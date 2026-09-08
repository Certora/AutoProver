"""
Spec-side prover tools: wrap composer/prover/core.py into LangGraph tools.

Provides get_prover_tool(), whose submit_buffer / collect_results tools:
- Materialize each run-target buffer to a temporary .spec file
- Run the Certora prover via run_prover() as background per-buffer jobs
- Stream output/polling events via a custom stream writer
- Report results as jobs finish, without blocking on a whole batch
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from contextlib import contextmanager, asynccontextmanager, ExitStack, nullcontext
from pathlib import Path
from typing import (
    Annotated, AsyncIterator, Callable, Container, Iterable, Iterator, Mapping, override,
    AsyncContextManager, Sequence, Literal
)
from typing_extensions import TypedDict, NotRequired

from graphcore.tools.vfs import VFSAccessor, VFSState

from composer.spec.source.live_explorer import VersionedHistory

from langchain_core.tools import InjectedToolCallId, tool, BaseTool
from langchain_core.messages import AIMessage
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, Field, Discriminator, create_model

from langgraph.config import get_stream_writer
from langgraph.types import Command
from composer.prover.ptypes import RuleResult, RulePath
from graphcore.graph import LLM

from composer.prover.core import (
    ProverOptions, SpecCompilationError, declared_rules_list, run_prover,
    DefaultCexHandler, ProverReport
)
from composer.prover.callbacks import ProverEventCallbacks
from composer.prover.ptypes import StatusCodes
from composer.ui.tool_display import tool_display
from composer.diagnostics.stream import (
    ProverOutputEvent, CloudPollingEvent, RuleAnalysisResult,
    CEXAnalysisStart, ProverRun, ProverLink, ProverResult
)
from composer.authoring.state import make_validation_stamper, spec_digest
from composer.spec.cvl_generation import CVLGenerationState
from composer.diagnostics.timing import RunSummary, get_run_summary
from graphcore.graph import tool_state_update
from composer.spec.util import temp_certora_file
from composer.spec.gen_types import CERTORA_DIR, SPECS_DIR
from composer.spec.util import string_hash
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.spec_buffers import (
    NamedBuffer, SpecBuffersExtra, buffer_state_digest, duplicated_declarations, run_targets,
)


_logger = logging.getLogger("composer.prover")


OVERLAY_OWNED_KEYS: frozenset[str] = frozenset({
    # forced by prover_config_overlay
    "verify", "parametric_contracts", "optimistic_loop", "rule_sanity",
    # set per prover run
    "rule", "msg",
})
"""Config keys the run pipeline forces onto the base config after spreading it: a
base-config entry under one of these is silently overridden at run and dump time.
The author's editable-flag registry (``author.EDITABLE_FLAGS``) must stay disjoint
from this set, or an "accepted" flag edit would never reach the prover."""


def prover_config_overlay(base_config: dict, *, main_contract: str, verify_target: str) -> dict:
    """The fixed prover settings the source pipeline layers on top of the base config.

    Shared by the live prover run and the persisted ``certora/confs`` dump so the
    two can't drift. ``verify_target`` is the ``<contract>:<spec path>`` the run verifies.
    """
    return {
        **base_config,
        "verify": verify_target,
        "parametric_contracts": main_contract,
        "optimistic_loop": True,
        "rule_sanity": "basic",
    }




DELETE_SKIP = "__delete_skip"

VALIDATION_KEY = "prover"

def _merge_rule_skips(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    to_ret = left.copy()
    for (k,v) in right.items():
        if v == DELETE_SKIP:
            if k in to_ret:
                del to_ret[k]
            continue
        to_ret[k] = v
    return to_ret

class RuleSelection(TypedDict):
    sort: Literal["exclude", "include"]
    selector: list[str]

class ProverRunLog(TypedDict):
    tool_call_id: str
    prover_results: list[tuple[RulePath, StatusCodes]]
    spec_digest: str
    rules: RuleSelection | None
    sort: Literal["run"]
    declared_rules: list[str]
    state_digest: str
    # The spec buffer this run belongs to; absent for a single-curr_spec run. Each buffer has its own
    # spec/digest, so completion is evaluated per buffer over its own runs (see _history_for_buffer).
    buffer: NotRequired[str]

class NagMarker(TypedDict):
    nagged_rules: list[RulePath]
    sort: Literal["nag"]

type ProverHistoryItem = Annotated[ProverRunLog | NagMarker, Discriminator("sort")]

def _executed_rules(
    r: ProverRunLog
) -> list[str]:
    if r["rules"] is None:
        return r["declared_rules"]
    elif r["rules"]["sort"] == "include":
        return r["rules"]["selector"]
    else:
        to_filt = set(r["rules"]["selector"])
        return [ r_id for r_id in r["declared_rules"] if r_id not in to_filt ]

#: How many consecutive runs must end in the identical failure before the author is nagged
#: about a rule. Counts the run being processed, so 3 means "this run plus the two before it".
STUCK_RULE_NAG_THRESHOLD = 3


def stuck_rule_warnings(
    # Values are compared for equality only, so the looser ``str`` keeps callers free of
    # the narrowing dance ``StatusCodes`` would otherwise force on a filtered comprehension.
    stuck_rules: Mapping["RulePath", str],
    prover_history: list[ProverHistoryItem],
    known_tool_call_ids: Container[str | None],
) -> tuple[set["RulePath"], bool]:
    """Decide which of the currently-stuck rules the author should be nagged about.

    Walks ``prover_history`` backwards counting, per stuck rule, how many *consecutive*
    recent runs ended in the identical failure — each rule starts at 1 for the run being
    processed. A rule leaves the tally as soon as a run breaks its streak (it either
    passed, failed differently, or the tally is exhausted); reaching
    :data:`STUCK_RULE_NAG_THRESHOLD` moves it to the returned warning set. A run that
    targeted an explicit rule subset is transparent to rules it never exercised, so a
    narrowly-scoped re-run neither extends nor breaks another rule's streak.

    A ``nag`` marker means those rules were warned about already, so their streaks restart
    there and the author isn't nagged twice for the same stretch of failures.

    Returns the rules to warn about, plus whether any inspected run predates the author's
    most recent history compaction (its tool call is no longer in ``known_tool_call_ids``)
    — the caller footnotes the warning with that, since those runs are no longer visible
    in the conversation.
    """
    stuck_count = {k: 1 for k in stuck_rules}
    to_warn: set[RulePath] = set()
    seen_post_compaction_history = False

    history_ind = len(prover_history) - 1
    while history_ind >= 0 and len(stuck_count) > 0:
        it = prover_history[history_ind]
        history_ind -= 1
        if it["sort"] == "nag":
            for r in it["nagged_rules"]:
                # A previously-nagged rule need not be stuck now — drop it if present.
                stuck_count.pop(r, None)
            continue
        assert it["sort"] == "run"
        if it["tool_call_id"] not in known_tool_call_ids:
            seen_post_compaction_history = True
        target_rules = _executed_rules(it)
        # Snapshot the keys: the body deletes from ``stuck_count`` as streaks end.
        for k in list(stuck_count.keys()):
            if k.rule not in target_rules:
                continue
            if not any(
                rp == k and stuck_rules[k] == stat for (rp, stat) in it["prover_results"]
            ):
                del stuck_count[k]
            else:
                stuck_count[k] += 1
                if stuck_count[k] == STUCK_RULE_NAG_THRESHOLD:
                    to_warn.add(k)
                    del stuck_count[k]
    return to_warn, seen_post_compaction_history


def last_prover_run(
    l: list[ProverHistoryItem]
) -> ProverRunLog | None:
    for i in range(len(l) - 1, -1, -1):
        it = l[i]
        if it["sort"] != "run":
            continue
        return it
    return None

def _iterate_history(
    l: list[ProverHistoryItem],
    curr_digest: str,
    curr_status: list[tuple[RulePath, StatusCodes]],
) -> Iterable[list[tuple[RulePath, StatusCodes]]]:
    """Newest-first walk of the prover results produced against the current authoring
    state: the current run's results, then each prior run whose ``state_digest`` matches,
    stopping at the first run recorded against a different state (nag markers are
    transparent)."""
    yield curr_status
    for elem in reversed(l):
        if elem["sort"] != "run":
            continue
        if elem["state_digest"] != curr_digest:
            return
        yield elem["prover_results"]

def _is_completion_history(
    l: list[ProverHistoryItem],
    curr_digest: str,
    expected_to_fail: set[str],
    curr_status: list[tuple[RulePath, StatusCodes]],
    all_rules: list[str]
) -> bool:
    """Whether the runs against the current authoring state collectively verify every
    declared rule (rules expected to fail are forgiven their failures but still count
    as covered)."""
    remaining_rules = set(all_rules)
    for history in _iterate_history(
        l, curr_digest, curr_status
    ):
        for (k, stat) in history:
            if stat != "VERIFIED" and k.rule not in expected_to_fail:
                return False
            # discard, not remove: results can name rules outside the declared list
            # (envfreeFuncsStaticCheck, parametric instantiations sharing one rule),
            # and overlapping run selections re-verify already-covered rules.
            remaining_rules.discard(k.rule)
        if not remaining_rules:
            return True
    return False

def _history_for_buffer(l: list[ProverHistoryItem], buffer: str) -> list[ProverHistoryItem]:
    """The prover history restricted to one buffer's runs (nag markers pass through). Each buffer has
    its own spec, hence its own ``state_digest``; filtering first keeps :func:`_iterate_history`'s
    digest streak from being truncated by an interleaved run of a different buffer."""
    return [it for it in l if it["sort"] != "run" or it.get("buffer") == buffer]


def buffer_is_complete(
    l: list[ProverHistoryItem],
    *,
    buffer: str,
    curr_digest: str,
    expected_to_fail: set[str],
    curr_status: list[tuple[RulePath, StatusCodes]],
    all_rules: list[str],
) -> bool:
    """Whether one buffer's rules are all verified against its current digest, evaluated over that
    buffer's own runs. Overall completion is the AND of this across every run-target buffer."""
    return _is_completion_history(
        l=_history_for_buffer(l, buffer),
        curr_digest=curr_digest,
        expected_to_fail=expected_to_fail,
        curr_status=curr_status,
        all_rules=all_rules,
    )


def _merge_prover_history(left: list[ProverHistoryItem], right: list[ProverHistoryItem]) -> list[ProverHistoryItem]:
    to_ret = left.copy()
    to_ret.extend(right)
    return to_ret

class ProverStateExtra(TypedDict):
    rule_skips: Annotated[dict[str, str], _merge_rule_skips]
    config: dict
    # Link of the last prover run this generation performed (URL or local results dir).
    # Last-write-wins; absent until the first prover run. Read at completion onto GeneratedCVL.
    prover_link: NotRequired[str | None]
    # Basename the spec is materialized/persisted under (e.g. "autospec_<slug>").
    # NotRequired so other ProverStateExtra injectors (e.g. config_edit) needn't set it.
    spec_stem: NotRequired[str]
    prover_history: Annotated[list[ProverHistoryItem], _merge_prover_history]
    reminders_channel: list[str]

    # The author's working copy of the source under verification; the prover runs
    # against its materialization when non-empty (see ProjectDirectory). Absent/empty
    # outside the editing-enabled pipeline. No merge op intentionally: the vfs is
    # only ever replaced wholesale (commit_edit / revert_to_edit).
    vfs: NotRequired[dict[str, str]]

type ProverEvents = CEXAnalysisStart | CloudPollingEvent | ProverOutputEvent | RuleAnalysisResult | ProverRun | ProverLink | ProverResult

# The source pipeline's state always seeds
# ``version_history`` — permanently empty in phases without the edit tools
# (structural invariants, never-edited authors), in which case it contributes
# nothing to the digest. The prover's validation stamp is bound to it so a
# post-run edit invalidates the stamp.
class StateWithSkips(CVLGenerationState, ProverStateExtra, VersionedHistory, SpecBuffersExtra):
    pass

class _SpecCallbacks(ProverEventCallbacks):
    def __init__(
        self,
        writer: Callable[[ProverEvents], None],
        tool_call_id: str,
        summary: RunSummary,
        config: dict,
        analysis_store: CexAnalysisStore | None = None,
    ) -> None:
        super().__init__(writer, tool_call_id)
        self._writer = writer
        self._tool_call_id = tool_call_id
        self._summary = summary
        self._config = config
        self._analysis_store = analysis_store
        self._started_mono: float | None = None

    @override
    async def on_cloud_poll(self, status: str, message: str) -> None:
        elapsed = (time.perf_counter() - self._started_mono) if self._started_mono else 0.0
        _logger.info(
            f"cloud poll tool_call={self._tool_call_id} status={status} "
            f"elapsed={elapsed:.1f}s msg={message}"
        )
        await super().on_cloud_poll(
            status, message
        )

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        self._started_mono = time.perf_counter()
        _logger.info(f"prover start tool_call={self._tool_call_id} args={args}")
        self._writer({
            "type": "prover_run",
            "tool_call_id": self._tool_call_id,
            "args": args,
            "config": self._config,
        })

    @override
    async def on_prover_link(self, link: str) -> None:
        _logger.info(f"prover link tool_call={self._tool_call_id} link={link}")
        self._summary.record_prover_link(link)
        self._writer({
            "type": "prover_link",
            "tool_call_id": self._tool_call_id,
            "link": link,
        })

    @override
    async def on_prover_runtime(self, ms: int) -> None:
        # Queue-free prover run time (cloud job startTime->finishTime, or local subprocess wall-clock).
        # Attributed to the active task; folded into the phase / run "prover_usage" totals.
        self._summary.record_prover_runtime(ms)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        elapsed = (time.perf_counter() - self._started_mono) if self._started_mono else 0.0
        status_summary = { k: v.status for (k,v) in results.items() }
        _logger.info(
            f"prover done tool_call={self._tool_call_id} "
            f"elapsed={elapsed:.1f}s status={status_summary}"
        )
        self._summary.add_prover_call(elapsed)
        # This run supersedes what was captured for the rules it covers, so drop their old analyses
        # before the handler records fresh ones: an instantiation that failed in an earlier iteration
        # and passes now must not survive into the report as a current failure. Fires before the CEX
        # handler runs, so the analyses recorded below are always the current run's.
        if self._analysis_store is not None:
            for rule_name in {r.path.rule for r in results.values()}:
                try:
                    await self._analysis_store.forget_rule(rule_name)
                except Exception:
                    _logger.exception("failed to clear stale cex analyses for %s", rule_name)
        await super().on_prover_result(
            results
        )

    @override
    async def on_analysis_complete(self, rule: RuleResult, explanation: str) -> None:
        # Capture this violated instantiation's counterexample analysis so the report phase can
        # reshape it into a finding without re-running the analysis. Keyed per instantiation, so a
        # parametric rule keeps every binding's analysis instead of only the last one written.
        # Never let a capture error disturb the run.
        if self._analysis_store is not None:
            try:
                await self._analysis_store.record(rule.path, explanation, rule.cex_dump)
            except Exception:
                _logger.exception("failed to capture cex analysis for %s", rule.name)
        await super().on_analysis_complete(rule, explanation)


@contextmanager
def tmp_spec(
    *,
    root: str,
    content: str,
    name: str | None = None,
) -> Iterator[str]:
    # Materialize under the canonical specs dir -- the same directory the spec is
    # ultimately persisted to -- so the prover resolves the spec's CVL imports
    # (e.g. ``summaries/X.spec``) identically at verify-time and after dumping.
    with temp_certora_file(
        root=root,
        ext="spec",
        content=content,
        name=name,
        dest_dir=SPECS_DIR,
    ) as tmp:
        yield tmp

def _prover_sem(cloud: bool) -> AsyncContextManager[None]:
    if not cloud:
        return asyncio.Semaphore(1)
    else:
        return nullcontext()


type ProjectDirectory = Callable[[dict[str, str]], AsyncContextManager[str]]
"""Per-run choice of the directory the prover executes in, given the author's
current VFS overlay. Yields the directory path; its lifetime is the run."""


def in_situ_project(project_root: str) -> ProjectDirectory:
    """The no-editing strategy: every run executes directly in the project
    directory."""
    @asynccontextmanager
    async def provide(vfs: dict[str, str]) -> AsyncIterator[str]:
        yield project_root
    return provide


def materializing_project(
    project_root: str, accessor: VFSAccessor[VFSState]
) -> ProjectDirectory:
    """The editing strategy: an empty VFS runs in-situ; a non-empty VFS is
    materialized over the project into a temporary directory that lives for
    the duration of the run. The copy (and the teardown) run in a worker
    thread — materializing a whole project is blocking IO that would
    otherwise stall every concurrently-streaming batch."""
    @asynccontextmanager
    async def provide(vfs: dict[str, str]) -> AsyncIterator[str]:
        if not vfs:
            yield project_root
            return
        stack = ExitStack()
        tmp = await asyncio.to_thread(
            stack.enter_context, accessor.materialize({"vfs": vfs})
        )
        try:
            yield tmp
        finally:
            await asyncio.to_thread(stack.close)
    return provide

@contextmanager
def setup_prover_config_in(
    *,
    working_dir: str,
    config: dict,
    spec_contents: str,
    spec_stem: str | None = None,
    main_contract: str,
    rule: list[str] | None,
    exclude_rule: list[str] | None,
    conf_dir: Path = CERTORA_DIR,
    **config_extra
):
    with tmp_spec(
        root=working_dir,
        content=spec_contents,
        name=spec_stem
    ) as generated_path:
        config = prover_config_overlay(
            config, main_contract=main_contract, verify_target=f"{main_contract}:{generated_path}"
        )
        config.update(config_extra)
        if rule is not None:
            config["rule"] = rule
        if exclude_rule is not None:
            config["exclude_rule"] = exclude_rule
        with temp_certora_file(
            root=working_dir,
            content=json.dumps(config, indent=2),
            ext="conf",
            name=spec_stem,
            prefix="verify",
            dest_dir=conf_dir,
        ) as conf_path:
            yield (conf_path, config)

def stuck_rule_nag(
    status_pairs: list[tuple[RulePath, StatusCodes]],
    prover_update: list[ProverHistoryItem],
    state: StateWithSkips,
) -> list[str]:
    """Warn when a rule has repeated the identical failure across recent runs: append a NagMarker to
    ``prover_update`` and return the reminder lines (empty when nothing is stuck). Shared by the
    single-spec and per-buffer verify paths."""
    stuck_rules = {
        k: v for (k, v) in status_pairs
        if v in ("TIMEOUT", "ERROR", "SANITY_FAILED") and k.rule not in state["rule_skips"]
    }
    known_tc_ids = {
        l["id"] for msg in state["messages"] if isinstance(msg, AIMessage)
        for l in msg.tool_calls if l["name"] == "collect_results"
    }
    to_warn, seen_post_compaction_history = stuck_rule_warnings(
        stuck_rules, state["prover_history"], known_tc_ids
    )
    if not to_warn:
        return []
    prover_update.append(NagMarker(sort="nag", nagged_rules=list(to_warn)))
    reminders = [
        "The following rule(s) have had identical failures on the last 3 runs of the prover:",
        *(f"- {it.pprint()}" for it in to_warn),
        "You may need to significantly change your approach, or skip the property if this is a persistent issue (you may need to use rebuttals to communicate"
        " these failures to the feedback judge).",
    ]
    if seen_post_compaction_history:
        reminders.append(
            "(NB: Some of these prover calls happened before your most recent task history summarization)"
        )
    return reminders


def _retarget_buffer_imports(cvl: str, names: Iterable[str], tag: str) -> str:
    """Rewrite each sibling-buffer import ``"<name>.spec"`` to its tagged filename ``"<name>__<tag>.spec"``
    so a tagged materialization stays self-consistent. Resource imports (e.g. ``"summaries/foo.spec"``)
    are different quoted strings and are left untouched, so they still resolve within the specs dir."""
    out = cvl
    for n in names:
        out = out.replace(f'"{n}.spec"', f'"{n}__{tag}.spec"')
    return out


@contextmanager
def materialize_buffers(
    working_dir: str, buffers: Mapping[str, NamedBuffer], tag: str
) -> Iterator[dict[str, str]]:
    """Write every buffer as ``{name}__{tag}.spec`` into the specs dir (all at once, so any buffer's
    ``import "<sibling>.spec"`` — retargeted to the tagged name — resolves), and yield ``name -> on-disk
    spec path``; every file is removed on exit. The ``tag`` (unique per submission) isolates concurrent
    jobs that share one run-root: on the in-situ path the run-root is the shared project directory, so
    two jobs writing the deterministic ``{name}.spec`` would clobber each other and race on cleanup."""
    names = list(buffers)
    with ExitStack() as stack:
        yield {
            name: stack.enter_context(tmp_spec(
                root=working_dir,
                content=_retarget_buffer_imports(buf.cvl, names, tag),
                name=f"{name}__{tag}",
            ))
            for name, buf in buffers.items()
        }


@contextmanager
def buffer_conf(
    *,
    working_dir: str,
    config: dict,
    main_contract: str,
    spec_path: str,
    buffer_name: str,
    conf_dir: Path,
    msg: str,
) -> Iterator[tuple[str, dict]]:
    """Build a conf verifying an already-materialized buffer spec at ``spec_path`` (its imports resolve
    to the sibling ``.spec`` files written by :func:`materialize_buffers`). Yields (conf_path, config)."""
    cfg = prover_config_overlay(
        config, main_contract=main_contract, verify_target=f"{main_contract}:{spec_path}"
    )
    cfg["msg"] = msg
    with temp_certora_file(
        root=working_dir,
        content=json.dumps(cfg, indent=2),
        ext="conf",
        name=f"verify_{buffer_name}",
        prefix="verify",
        dest_dir=conf_dir,
    ) as conf_path:
        yield (conf_path, cfg)


_SUBMIT_BUFFER_DESCRIPTION = """
Submit one run-target buffer for verification as an independent background prover job, and return
immediately — the job proves while you keep working. Submit each buffer as soon as it is ready; buffers
prove in parallel. Re-submitting a buffer relaunches it (superseding any in-flight job for it), which is
how you re-verify a buffer after editing it, or after editing a shared buffer it imports. A buffer
already verified at its current content, or already running, is not re-launched. Retrieve outcomes with
collect_results.
"""

_COLLECT_RESULTS_DESCRIPTION = """
Retrieve the results of finished buffer jobs (submitted with submit_buffer). Returns each finished
buffer's prover outcome plus a status board: which buffers are complete, still running, or need
(re)submission. By default it does NOT block — it returns whatever has finished so far (possibly
nothing), so you can go author or submit other buffers instead of waiting. Pass wait=true ONLY when you
have no other work: every buffer submitted and running, with no finished result left to process; it then
sleeps until the next job finishes.
"""


@dataclass
class ProverToolset:
    """The prover-side agent tools: ``buffer_tools`` (``submit_buffer`` / ``collect_results``), which
    submit per-buffer jobs asynchronously and consume results as they finish."""

    buffer_tools: list[BaseTool]


@dataclass
class _BufJob:
    """One in-flight (or just-finished) per-buffer prover job. At most one per buffer name at a time;
    re-submitting a buffer supersedes (cancels) a stale predecessor. Lives in the prover tool's closure,
    not in graph state — asyncio tasks span agent turns and are not serializable."""

    name: str
    #: The buffer's content digest at submit time; a completion is credited only at the current digest,
    #: so a job whose digest is now stale (its buffer or a shared import changed) can't mark it done.
    digest: str
    task: "asyncio.Task[None]"


@dataclass
class _BufDone:
    """A finished buffer job's payload, delivered through the completion queue to ``collect_results``."""

    name: str
    digest: str
    #: The prover report, or a compile/toolchain error message (str) that aborts only this buffer.
    result: ProverReport | str
    all_rules: list[str]


def get_prover_tool(
    llm: LLM,
    main_contract: str,
    project_directory: ProjectDirectory,
    prover_opts: ProverOptions,
    analysis_store: CexAnalysisStore | None = None,
) -> ProverToolset:
    sem = _prover_sem(prover_opts.cloud)
    stamper = make_validation_stamper(VALIDATION_KEY)

    # Multi-buffer async job state, held in the closure (not graph state) so it spans agent turns:
    # submit_buffer launches a background task per buffer and returns immediately; collect_results
    # drains finished jobs off the queue. At most one live job per buffer name — a re-submit supersedes
    # a stale predecessor. See submit_buffer / collect_results below.
    buffer_jobs: dict[str, _BufJob] = {}
    done_queue: asyncio.Queue[_BufDone] = asyncio.Queue()
    submit_counts: dict[str, int] = {}
    # Declarations already flagged as duplicated-across-buffers, so collect_results nags about each at
    # most once per run (advisory only — the agent may hoist them to a shared buffer or ignore).
    reported_dupes: set[str] = set()

    def component_of(state: StateWithSkips) -> str:
        """The label prefix for this generation's prover runs: its seeded spec stem, or the main
        contract, with the ``autospec_`` prefix stripped."""
        return (state.get("spec_stem") or main_contract).removeprefix("autospec_")

    # ---- Multi-buffer async submit / collect -------------------------------------------------
    # The agent submits each run-target buffer as an independent background job and consumes results
    # as they finish, so a fast group is reviewed while a slow group is still proving. A shared-buffer
    # edit re-verifying every importer rides the content digest.

    async def _run_buffer_job(
        *, name: str, digest: str, tag: str, label: str, buffers: Mapping[str, NamedBuffer],
        vfs: dict[str, str], conf: dict, cex_state: StateWithSkips, tool_call_id: str,
        writer: Callable[[ProverEvents], None], summary: RunSummary,
    ) -> None:
        """Verify one buffer end-to-end against a frozen snapshot (taken at submit time) of the source
        and all buffers, then push the outcome onto the completion queue. The per-submission ``tag``
        isolates this job's spec/conf files, so editing + re-submitting a shared buffer (or a concurrent
        sibling job) can never mutate the files this job is reading. Cancellation (a supersede) propagates
        as CancelledError and pushes nothing — the superseded result is simply dropped."""
        conf_dir = CERTORA_DIR / "confs"
        stem = f"{name}__{tag}"
        try:
            async with sem, project_directory(vfs) as run_root:
                with materialize_buffers(run_root, buffers, tag) as paths:
                    spec_path = paths[name]
                    with buffer_conf(
                        working_dir=run_root, config=conf, main_contract=main_contract,
                        spec_path=spec_path, buffer_name=stem, conf_dir=conf_dir, msg="",
                    ) as (cpath, _cfg):
                        try:
                            all_rules = await declared_rules_list(folder=Path(run_root), args=[cpath])
                        except SpecCompilationError as exc:
                            await done_queue.put(_BufDone(
                                name, digest, f"[buffer {name}] failed to compile:\n{exc.output}", [],
                            ))
                            return
                    with buffer_conf(
                        working_dir=run_root, config=conf, main_contract=main_contract,
                        spec_path=spec_path, buffer_name=stem, conf_dir=conf_dir, msg=label,
                    ) as (cpath, cfg):
                        res = await run_prover(
                            Path(run_root), [cpath], tool_call_id, prover_opts,
                            _SpecCallbacks(writer, tool_call_id, summary, cfg, analysis_store=analysis_store),
                            DefaultCexHandler(llm, cex_state, summarization_threshold=10),
                        )
            await done_queue.put(_BufDone(name, digest, res, all_rules))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a job crash must not sink silently — surface it on the queue
            _logger.exception("buffer job %s crashed", name)
            await done_queue.put(_BufDone(name, digest, f"[buffer {name}] job error: {exc}", []))

    def _cur_digest(state: StateWithSkips, buffers: Mapping[str, NamedBuffer], name: str) -> str:
        skipped_pairs = [(str(s.property_title), str(s.reason)) for s in state["skipped"]]
        return buffer_state_digest(
            buffers, name, skipped=skipped_pairs, version_history=state["version_history"],
        )

    def _buffer_complete_at(
        state: StateWithSkips, buffers: Mapping[str, NamedBuffer], name: str, digest: str,
        *, extra_history: Sequence[ProverHistoryItem] = (),
    ) -> bool:
        return buffer_is_complete(
            list(state["prover_history"]) + list(extra_history), buffer=name, curr_digest=digest,
            expected_to_fail=set(state["rule_skips"].keys()), curr_status=[],
            all_rules=list(buffers[name].owned_rules),
        )

    submit_schema = create_model(
        "SubmitBuffer", __doc__=_SUBMIT_BUFFER_DESCRIPTION,
        name=(str, Field(description="The run-target buffer to submit for verification.")),
        state=(Annotated[StateWithSkips, InjectedState], ...),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display("Submitting buffer", None)
    @tool(args_schema=submit_schema)
    async def submit_buffer(**args) -> str | Command:
        state: StateWithSkips = args["state"]
        name: str = args["name"]
        tool_call_id: str = args["tool_call_id"]
        buffers = state.get("buffers") or {}
        b = buffers.get(name)
        if b is None:
            return f"No buffer named {name!r}. Create it with put_buffer first."
        if not b.is_run_target:
            return f"Buffer {name!r} is a shared (imports-only) buffer; it runs no rules of its own."

        digest = _cur_digest(state, buffers, name)
        if _buffer_complete_at(state, buffers, name, digest):
            return f"Buffer {name!r} is already verified at its current content; nothing to submit."

        existing = buffer_jobs.get(name)
        if existing is not None and not existing.task.done():
            if existing.digest == digest:
                return f"Buffer {name!r} is already running. Use collect_results to retrieve its result."
            existing.task.cancel()  # buffer (or a shared import) changed: supersede the stale job

        n = submit_counts.get(name, 0) + 1
        submit_counts[name] = n
        component = component_of(state)
        task = asyncio.create_task(_run_buffer_job(
            name=name, digest=digest, tag=f"{digest[:8]}_{n}",
            label=f"{component}/{name} submission {n}",
            buffers=dict(buffers), vfs=dict(state.get("vfs") or {}), conf=state["config"],
            cex_state=state, tool_call_id=tool_call_id,
            # The stream writer is captured here and used by the detached task: its prover-progress
            # events carry this submit call's tool_call_id, which has already returned, so background-job
            # progress can render loosely in the UI (functional results are unaffected).
            writer=get_stream_writer(), summary=get_run_summary(),
        ))
        buffer_jobs[name] = _BufJob(name=name, digest=digest, task=task)
        running = sorted(nm for nm, j in buffer_jobs.items() if not j.task.done())
        return (
            f"Submitted buffer {name!r} (submission {n}); it is now proving in the background. "
            f"Running: {running}. Call collect_results to retrieve results as jobs finish."
        )

    collect_schema = create_model(
        "CollectResults", __doc__=_COLLECT_RESULTS_DESCRIPTION,
        wait=(bool, Field(
            default=False,
            description="Block until the next job finishes. Set true ONLY when you have no other work: "
            "every buffer is submitted and running and you have no finished result left to process. "
            "Leave false to take whatever has finished so far without waiting.",
        )),
        state=(Annotated[StateWithSkips, InjectedState], ...),
        tool_call_id=(Annotated[str, InjectedToolCallId], ...),
    )

    @tool_display("Collecting prover results", None)
    @tool(args_schema=collect_schema)
    async def collect_results(**args) -> str | Command:
        state: StateWithSkips = args["state"]
        tool_call_id: str = args["tool_call_id"]
        wait: bool = args["wait"]
        buffers = state.get("buffers") or {}
        targets = run_targets(buffers)
        if not targets:
            return "No run-target buffers to collect. Author buffers and submit_buffer them first."

        # Current digest per buffer is stable within this call (buffers/skips/edit-history are fixed);
        # memoize it — each is a content hash over the import closure, read at several points below.
        _digests: dict[str, str] = {}
        def cur_digest(nm: str) -> str:
            if nm not in _digests:
                _digests[nm] = _cur_digest(state, buffers, nm)
            return _digests[nm]

        drained: list[_BufDone] = []
        while not done_queue.empty():
            drained.append(done_queue.get_nowait())
        if not drained and wait and any(not j.task.done() for j in buffer_jobs.values()):
            # Idle wait: nothing else to do, sleep until one job finishes. Unbounded, but every job is
            # guaranteed to land on the queue — run_prover self-bounds its subprocess, and a crash is
            # caught and enqueued as an error — so this can't hang on a wedged job.
            drained.append(await done_queue.get())
            while not done_queue.empty():
                drained.append(done_queue.get_nowait())

        # Cancel jobs left running against a now-stale digest: a shared buffer they import was edited, so
        # their result would be discarded anyway — and on local runs a doomed job needlessly holds the
        # single prover slot. The agent re-submits them (they show under needs-(re)submission below).
        for nm, j in list(buffer_jobs.items()):
            if not j.task.done() and nm in buffers and j.digest != cur_digest(nm):
                j.task.cancel()
                buffer_jobs.pop(nm, None)

        # Retire finished jobs from the registry. A result that lands between the drain and here stays on
        # the queue, so its buffer is picked up on the next collect even though its job is already gone.
        for nm in [nm for nm, j in buffer_jobs.items() if j.task.done()]:
            buffer_jobs.pop(nm, None)

        prover_update: list[ProverHistoryItem] = []
        fresh: dict[str, list[tuple[RulePath, StatusCodes]]] = {}
        link: str | None = None
        parts: list[str] = []
        for d in drained:
            if isinstance(d.result, str):  # compile/toolchain error: surface it, record no run
                parts.append(f"=== buffer {d.name} ===\n{d.result}")
                continue
            results: list[tuple[RulePath, StatusCodes]] = list(d.result.raw_rule_status.items())
            fresh[d.name] = results
            link = d.result.link or link
            stale = d.name in buffers and d.digest != cur_digest(d.name)
            note = (" (NOTE: the spec changed since this was submitted — this result is STALE; re-submit "
                    "this buffer.)") if stale else ""
            parts.append(f"=== buffer {d.name} ==={note}\n{d.result.result_str}")
            prover_update.append(ProverRunLog(
                tool_call_id=tool_call_id, prover_results=results, rules=None,
                spec_digest=string_hash(buffers[d.name].cvl) if d.name in buffers else "",
                sort="run", declared_rules=d.all_rules, state_digest=d.digest, buffer=d.name,
            ))

        # Per-buffer completion is re-evaluated over history + this drain against the CURRENT digest, so
        # a stale run (state_digest mismatch) never credits completion. Overall completion is the AND of
        # these, checked at publish (check_buffer_completion).
        prover_stamps: dict[str, str] = {}
        for b in targets:
            d = cur_digest(b.name)
            if _buffer_complete_at(state, buffers, b.name, d, extra_history=prover_update):
                prover_stamps[f"prover:{b.name}"] = d

        # Status board — the agent's work-list. `running` counts only a live job at the CURRENT digest;
        # a job left running at a stale digest (its shared import changed) is doomed, so its buffer falls
        # under needs-(re)submission until the agent relaunches it.
        complete = {b.name for b in targets if f"prover:{b.name}" in prover_stamps}
        running = {
            nm for nm, j in buffer_jobs.items()
            if not j.task.done() and nm in buffers and j.digest == cur_digest(nm)
        }
        needs_submit = [b.name for b in targets if b.name not in complete and b.name not in running]
        board = [
            "",
            f"[buffers] complete: {sorted(complete)}",
            f"[buffers] running: {sorted(running)}",
            f"[buffers] needs (re)submission: {sorted(needs_submit)}",
        ]
        # Advisory: flag declarations duplicated verbatim across run-target buffers (once each) — likely
        # belong in a shared buffer the duplicating buffers import.
        fresh_dupes = {d: ns for d, ns in duplicated_declarations(buffers).items() if d not in reported_dupes}
        if fresh_dupes:
            reported_dupes.update(fresh_dupes)
            board.append("[buffers] NOTE: these declarations are duplicated across run-target buffers — "
                         "consider moving each to a shared buffer the duplicating buffers import:")
            board.extend(f"  {d}  (in {ns})" for d, ns in fresh_dupes.items())
        if not drained:
            parts.append("No finished jobs yet." if running else "No finished jobs and nothing running.")

        nag_channel: dict = {}
        all_status = [pair for results in fresh.values() for pair in results]
        if reminders := stuck_rule_nag(all_status, prover_update, state):
            nag_channel["reminders_channel"] = reminders
        if not needs_submit and not running:
            nag_channel.setdefault("reminders_channel", []).append(
                "Every run-target buffer is verified at its current content. Once each also has "
                "feedback, you can publish."
            )

        return tool_state_update(
            tool_call_id=tool_call_id, content="\n".join(parts + board), prover_link=link,
            validations=prover_stamps, prover_history=prover_update, **nag_channel,
        )

    return ProverToolset(buffer_tools=[submit_buffer, collect_results])
