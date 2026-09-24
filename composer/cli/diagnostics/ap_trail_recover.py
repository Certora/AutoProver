"""``ap-trail recover-drafts`` — re-seed a run's per-unit CVLR drafts into the cache.

The author caches each unit's working draft under ``last_attempt`` in a ``finally``, so the next
run sharing its ``--cache-ns`` picks the draft up instead of authoring it again. A run that dies
without unwinding — SIGKILL, a machine going away — never reaches that ``finally``, and its
drafts then exist only in the checkpointer. This walks them back into the cache.

Thread ids and cache namespaces are two renderings of one tree of ``child()`` calls
(``WorkflowContext._child_pure``), so a unit's thread id is rebuilt from its namespace by the
rule the pipeline derived it with. Only that direction is sound: a namespace element may contain
the ``-`` that joins thread-id segments, so a thread id does not split back apart unambiguously.
"""

import argparse
import asyncio
import sys

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from composer.io.run_index import get_run, get_run_data
from composer.pipeline.run_tags import CACHE_ROOT_RECORD, AutoProveCacheTags
from composer.spec.context import CvlrGeneration, WorkflowContext
from composer.spec.cvlr.author import LAST_ATTEMPT_KEY, LastCvlrAttempt
from composer.workflow.services import checkpointer_context, store_context

from .uid_bind import bind_uid_args

#: ``RunMeta.tags`` entry naming the run's root thread; every unit's thread descends from it.
ROOT_THREAD_TAG = "root_thread_id"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id", help="Run to recover drafts from (as shown by `ap-trail ls`).")
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Report what would be recovered and write nothing.",
    )
    parser.add_argument(
        "--draft", choices=("final", "largest"), default="final",
        help=(
            "Which checkpoint of each unit to recover. 'final' (default) is where the unit "
            "stopped. 'largest' is its high-water mark, which is the better resume buffer when "
            "the run ended on a budget cut: the wrap-up order has a unit delete every rule whose "
            "verdict it never saw, so a cut unit's final draft can be a fraction of its work."
        ),
    )
    bind_uid_args(parser)


def _no_memory(namespace: str) -> BaseTool:
    """This command drives no agents, so nothing should ask it for a memory tool."""
    raise NotImplementedError(f"recover-drafts has no memory services (asked for {namespace!r})")


async def _draft(
    checkpointer: BaseCheckpointSaver[str], thread_id: str, *, largest: bool
) -> LastCvlrAttempt | None:
    """A thread's draft and the munges recorded alongside it, or ``None`` if it never held one.

    ``alist`` walks newest-first, so the first hit is where the unit stopped. ``largest`` keeps
    walking for its high-water mark instead; see the ``--draft`` help for when that is wanted.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    best: LastCvlrAttempt | None = None
    async for entry in checkpointer.alist(config):
        values = entry.checkpoint.get("channel_values") or {}
        draft = values.get("curr_spec")
        if not (isinstance(draft, str) and draft.strip()):
            continue
        if best is None or len(draft) > len(best.spec):
            best = LastCvlrAttempt(
                spec=draft,
                munges=[m.describe() for m in values.get("munges") or ()],
            )
        if not largest:
            break
    return best


async def _recover(
    store: BaseStore,
    checkpointer: BaseCheckpointSaver[str],
    *,
    root: tuple[str, ...],
    thread_root: str,
    dry_run: bool,
    largest: bool,
) -> int:
    """How many drafts were found; each is cached too, unless ``dry_run``."""
    found = 0
    for namespace in sorted(await store.alist_namespaces(prefix=root)):
        tail = namespace[len(root):]
        if not tail:
            continue
        thread_id = thread_root + "".join(f"-{name}" for name in tail)
        attempt = await _draft(checkpointer, thread_id, largest=largest)
        if attempt is None:
            continue
        found += 1
        lines = attempt.spec.count("\n") + 1
        munged = f", {len(attempt.munges)} munge(s)" if attempt.munges else ""
        print(f"  {lines:5d} lines{munged}  {'.'.join(tail)}")
        if not dry_run:
            ctx = WorkflowContext.create(
                services=_no_memory,
                thread_id=thread_id,
                store=store,
                recursion_limit=1,
                cache_namespace=namespace,
            ).abstract(CvlrGeneration)
            await ctx.child(LAST_ATTEMPT_KEY).cache_put(attempt)
    return found


async def _main(args: argparse.Namespace) -> int:
    async with store_context() as store, checkpointer_context() as checkpointer:
        run = await get_run(store, args.run_id, uid=args.uid)
        if run is None:
            print(f"No such run: {args.run_id}", file=sys.stderr)
            return 1
        thread_root = run["tags"].get(ROOT_THREAD_TAG)
        if not isinstance(thread_root, str):
            print(f"Run {args.run_id} recorded no {ROOT_THREAD_TAG}.", file=sys.stderr)
            return 1

        record = await get_run_data(store, args.run_id, CACHE_ROOT_RECORD, uid=args.uid)
        cache_root = AutoProveCacheTags.model_validate(record).cache_root if record else None
        if not cache_root:
            print(
                f"Run {args.run_id} ran without --cache-ns, so there is nowhere to recover to.",
                file=sys.stderr,
            )
            return 1

        found = await _recover(
            store, checkpointer,
            root=tuple(cache_root), thread_root=thread_root, dry_run=args.dry_run,
            largest=args.draft == "largest",
        )

    if not found:
        print(f"No drafts on any thread under {thread_root}.", file=sys.stderr)
        return 1
    verb = "would be recovered" if args.dry_run else "written to the cache"
    print(f"\n{found} draft(s) {verb} under {'.'.join(cache_root)}.")
    return 0


def main(args: argparse.Namespace) -> int:
    return asyncio.run(_main(args))
