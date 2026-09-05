"""
Spec-side prover tool: wraps composer/prover/core.py into a LangGraph tool.

Provides get_prover_tool() which creates a verify_spec tool that:
- Reads curr_spec from injected state
- Writes a temporary .spec file
- Runs the Certora prover via run_prover()
- Streams output/polling events via custom stream writer
"""

import asyncio
import json
import logging
import time
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
from pydantic import BaseModel, Field, Discriminator

from langgraph.config import get_stream_writer
from langgraph.types import Command
from composer.prover.ptypes import RuleResult, RulePath
from graphcore.graph import LLM

from composer.prover.core import (
    ProverOptions, SpecCompilationError, declared_rules_list, run_prover,
    DefaultCexHandler
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
    BufferRun, NamedBuffer, SpecBuffersExtra, buffer_digest, plan_buffer_runs, run_targets,
)


_logger = logging.getLogger("composer.prover")


OVERLAY_OWNED_KEYS: frozenset[str] = frozenset({
    # forced by prover_config_overlay
    "verify", "parametric_contracts", "optimistic_loop", "rule_sanity",
    # set per-run by verify_spec
    "rule", "msg",
})
"""Config keys the run pipeline forces onto the base config after spreading it: a
base-config entry under one of these is silently overridden at run and dump time.
The author's editable-flag registry (``author.EDITABLE_FLAGS``) must stay disjoint
from this set, or an "accepted" flag edit would never reach the prover."""


def prover_config_overlay(base_config: dict, *, main_contract: str, verify_target: str) -> dict:
    """The fixed prover settings the source pipeline layers on top of the base config.

    Shared by the live ``verify_spec`` run and the persisted ``certora/confs`` dump so the
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

    # The author's working copy of the source under verification; verify_spec runs
    # against its materialization when non-empty (see ProjectDirectory). Absent/empty
    # outside the editing-enabled pipeline. No merge op intentionally: the vfs is
    # only ever replaced wholesale (commit_edit / revert_to_edit).
    vfs: NotRequired[dict[str, str]]

type ProverEvents = CEXAnalysisStart | CloudPollingEvent | ProverOutputEvent | RuleAnalysisResult | ProverRun | ProverLink | ProverResult

# ``verify_spec`` only runs in the source pipeline, whose state always seeds
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


class VerifySpecSchema(BaseModel):
    """
    Run the Certora prover to verify the current spec against the source code.

    Returns verification results:
    - VERIFIED: Rule holds for all inputs
    - VIOLATED: Counterexample found (with CEX analysis)
    - TIMEOUT: Verification did not complete in time

    Use these results to refine your spec.
    """
    tool_call_id: Annotated[str, InjectedToolCallId]

    rules: list[str] | None = Field(
        default=None,
        description="Specific rules to verify. If None, verifies all rules. Mutually exclusive with the `exclude_rules` argument"
    )

    exclude_rules: list[str] | None = Field(
        default=None,
        description="Specific rules to SKIP verifying. If none validates all rules. Mutually exclusive with `rules` argument"
    )

    state: Annotated[StateWithSkips, InjectedState]


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

@contextmanager
def materialize_buffers(
    working_dir: str, buffers: Mapping[str, NamedBuffer]
) -> Iterator[dict[str, str]]:
    """Write every buffer as ``{name}.spec`` into the specs dir (all at once), so any buffer's
    ``import "X.spec"`` resolves to its sibling. Yields ``name -> on-disk spec path``; every file is
    removed on exit. Buffer names are unique, so there is no same-name write race across the set."""
    with ExitStack() as stack:
        yield {
            name: stack.enter_context(tmp_spec(root=working_dir, content=buf.cvl, name=name))
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
    rule: list[str] | None,
    msg: str,
) -> Iterator[tuple[str, dict]]:
    """Build a conf verifying an already-materialized buffer spec at ``spec_path`` (its imports resolve
    to the sibling ``.spec`` files written by :func:`materialize_buffers`). Yields (conf_path, config)."""
    cfg = prover_config_overlay(
        config, main_contract=main_contract, verify_target=f"{main_contract}:{spec_path}"
    )
    if rule is not None:
        cfg["rule"] = rule
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


def get_prover_tool(
    llm: LLM,
    main_contract: str,
    project_directory: ProjectDirectory,
    prover_opts: ProverOptions,
    analysis_store: CexAnalysisStore | None = None,
) -> BaseTool:
    sem = _prover_sem(prover_opts.cloud)
    stamper = make_validation_stamper(VALIDATION_KEY)
    # Serialize verify calls targeting the same spec name: the spec/conf are written
    # under a deterministic name and unlinked on exit, so two overlapping same-stem
    # calls (e.g. parallel verify_spec for one component) would race. Distinct stems
    # stay concurrent (notably on cloud, where ``sem`` is a no-op).
    # Not pruned: bounded by this run's stems (per-component + invariants) and dies with
    # the per-run tool; popping a held lock would let a later same-stem call mint a fresh,
    # non-excluding one.
    spec_locks: dict[str, asyncio.Lock] = {}

    @tool_display("Running prover", None)
    @tool(args_schema=VerifySpecSchema)
    async def verify_spec(
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[StateWithSkips, InjectedState],
        rules: list[str] | None = None,
        exclude_rules: list[str] | None = None
    ) -> str | Command:
        last_msg = state["messages"][-1]
        if isinstance(last_msg, AIMessage) and any(
            i["id"] != tool_call_id for i in last_msg.tool_calls
        ):
            return "Cannot call the verify_spec tool in parallel with other tool calls. verify_spec must be the only tool you call in a turn"

        if rules is not None and exclude_rules is not None:
            return "Cannot invoke the prover with both `rules` and `exclude_rules` set to non-none"

        # Multi-buffer mode when the agent has declared run-target buffers; otherwise the single
        # curr_spec path below (unchanged).
        buffers = state.get("buffers") or {}
        targets = run_targets(buffers)

        spec = state["curr_spec"]
        if not targets and spec is None:
            return "Specification not yet put on VFS"

        spec_hash = string_hash(spec) if spec is not None else ""

        if not targets and (last_run := last_prover_run(state["prover_history"])) is not None:
            if any(i == "TIMEOUT" for (_,i) in last_run["prover_results"]) and last_run["spec_digest"] == spec_hash:
                return "Refusing to re-run prover on identical spec with a known TIMEOUT result; timeouts are not transient " \
                    "errors and will not go away by re-running the tool."

        conf = state["config"]
        # With a seeded stem, name the spec/conf after it (so on-disk names match the
        # dump) under a lock; else fall back to unique uid names (no lock needed).
        spec_stem = state.get("spec_stem")
        summary = get_run_summary()
        component = (spec_stem or main_contract).removeprefix("autospec_")
        iteration = len(state["prover_history"]) + 1

        conf_dir = (CERTORA_DIR / "confs") if spec_stem is not None else CERTORA_DIR
        lock = spec_locks.setdefault(spec_stem, asyncio.Lock()) if spec_stem is not None else nullcontext()
        prover_msg = f"{component} iteration number {iteration}"

        summary = get_run_summary()

        component = (spec_stem or main_contract).removeprefix("autospec_")
        iteration = len(state["prover_history"]) + 1
        prover_msg = f"{component} iteration number {iteration}"


        async def run_in(run_root: str) -> str | Command:
            assert spec is not None  # single-spec path; buffers mode dispatches to run_buffers
            with setup_prover_config_in(
                working_dir=run_root,
                main_contract=main_contract,
                spec_stem=spec_stem,
                spec_contents=spec,
                conf_dir=conf_dir,
                config=conf,
                rule=None,
                exclude_rule=None,
                msg=""
            ) as (config_path, _ignored):
                try:
                    all_rules = await declared_rules_list(
                        folder=Path(run_root),
                        args=[config_path]
                    )
                except SpecCompilationError as exc:
                    return f"The spec failed to compile:\n{exc.output}"
            with setup_prover_config_in(
                working_dir=run_root,
                main_contract=main_contract,
                spec_stem=spec_stem,
                spec_contents=spec,
                conf_dir=conf_dir,
                config=conf,
                rule=rules,
                exclude_rule=exclude_rules,
                msg=prover_msg
            ) as (config_path, config):
                async with sem:
                    result = await run_prover(
                        Path(run_root),
                        [config_path],
                        tool_call_id,
                        prover_opts,
                        _SpecCallbacks(get_stream_writer(), tool_call_id, summary, config,
                                        analysis_store=analysis_store),
                        DefaultCexHandler(llm, state, summarization_threshold=10)
                    )

            if isinstance(result, str):
                return result

            stuck_rules = {
                k: v for (k,v) in result.raw_rule_status.items() if v in ("TIMEOUT", "ERROR", "SANITY_FAILED") and k.rule not in state["rule_skips"]
            }

            known_tc_ids = {
                l["id"]
                for msg in state["messages"] if isinstance(msg, AIMessage)
                for l in msg.tool_calls if l["name"] == "verify_spec"
            }

            to_warn, seen_post_compaction_history = stuck_rule_warnings(
                stuck_rules, state["prover_history"], known_tc_ids
            )

            curr_state_digest = spec_digest(
                spec, state["skipped"], state["version_history"]
            )

            prover_results : list[tuple[RulePath, StatusCodes]] = [(k, v) for (k,v) in result.raw_rule_status.items()]

            all_verified = _is_completion_history(
                l=state["prover_history"],
                curr_digest=curr_state_digest,
                expected_to_fail=set(state["rule_skips"].keys()),
                curr_status=prover_results,
                all_rules=all_rules
            )

            prover_update : list[ProverHistoryItem] = [
                ProverRunLog(
                    tool_call_id=tool_call_id,
                    prover_results=[(k, v) for (k,v) in result.raw_rule_status.items()],
                    rules={"sort": "exclude", "selector": exclude_rules } if exclude_rules is not None else \
                        {"sort": "include", "selector": rules} if rules is not None else None,
                    spec_digest=spec_hash,
                    sort="run",
                    declared_rules=all_rules,
                    state_digest=curr_state_digest
                )
            ]
            nag_channel = {

            }
            if len(to_warn) > 0:
                prover_update.append(NagMarker(
                    sort="nag",
                    nagged_rules=list(to_warn)
                ))
                nag_channel["reminders_channel"] = [
                    "The following rule(s) have had identical failures on the last 3 runs of the prover:",
                    *(f"- {it.pprint()}" for it in to_warn),
                    "You may need to significantly change your approach, or skip the property if this is a persistent issue (you may need to use rebuttals to communicate"
                    " these failures to the feedback judge)."
                ]
                if seen_post_compaction_history:
                    nag_channel["reminders_channel"].append(
                        "(NB: Some of these prover calls happened before your most recent task history summarization)"
                    )
            if all_verified:
                nag_channel.setdefault("reminders_channel", []).append(
                    "You have successfully verified over your prior prover run(s) that all rules verify. This task is completed."
                )
                # Completing the coverage stamps, however the completing run was scoped:
                # every declared rule was verified against exactly this authoring state
                # (the state_digest match), so a piecemeal completion is as good as a
                # full-run one.
                return tool_state_update(
                    tool_call_id=tool_call_id, content=result.result_str,
                    prover_link=result.link, validations=stamper(state, state["version_history"]),
                    prover_history=prover_update, **nag_channel
                )
            return tool_state_update(
                tool_call_id=tool_call_id, content=result.result_str, prover_link=result.link,
                prover_history=prover_update, **nag_channel
            )

        # The author's working copy decides where this run executes (in-situ for
        # an empty VFS, a temp materialization otherwise); the same-stem lock
        # guards the deterministic spec/conf names within it.
        async def run_buffers(run_root: str, targets: list[NamedBuffer]) -> str | Command:
            """Verify each run-target buffer as its own spec, concurrently; overall completion is the
            AND of per-buffer completion. A buffer already complete at its current digest is skipped."""
            conf = state["config"]
            summary = get_run_summary()
            conf_dir = CERTORA_DIR / "confs"
            expected = set(state["rule_skips"].keys())
            skipped = state["skipped"]
            vh = state["version_history"]

            def digest_of(b: NamedBuffer) -> str:
                # The buffer's content + import closure, plus this authoring state (skips / edit history),
                # so any of them changing forces a re-verify.
                return buffer_digest(
                    buffers, b.name,
                    extra_parts=[f"skip:{s}" for s in sorted(str(x) for x in skipped)] + [f"vh:{len(vh)}"],
                )

            plan = plan_buffer_runs(
                buffers,
                digest_of=digest_of,
                is_complete=lambda b, d: buffer_is_complete(
                    state["prover_history"], buffer=b.name, curr_digest=d,
                    expected_to_fail=expected, curr_status=[], all_rules=list(b.owned_rules),
                ),
            )
            pending = [r for r in plan if r.needs_run]
            if not pending:
                return tool_state_update(
                    tool_call_id=tool_call_id,
                    content="All buffers already verified at their current state; nothing to re-run.",
                )

            with materialize_buffers(run_root, buffers) as paths:
                async def run_one(r: BufferRun):
                    b = r.buffer
                    spec_path = paths[b.name]
                    with buffer_conf(
                        working_dir=run_root, config=conf, main_contract=main_contract,
                        spec_path=spec_path, buffer_name=b.name, conf_dir=conf_dir, rule=None, msg="",
                    ) as (cpath, _cfg):
                        try:
                            all_rules = await declared_rules_list(folder=Path(run_root), args=[cpath])
                        except SpecCompilationError as exc:
                            return (r, f"[buffer {b.name}] failed to compile:\n{exc.output}", [])
                    with buffer_conf(
                        working_dir=run_root, config=conf, main_contract=main_contract,
                        spec_path=spec_path, buffer_name=b.name, conf_dir=conf_dir, rule=None,
                        msg=f"{b.name} iteration {len(state['prover_history']) + 1}",
                    ) as (cpath, cfg):
                        async with sem:
                            res = await run_prover(
                                Path(run_root), [cpath], tool_call_id, prover_opts,
                                _SpecCallbacks(get_stream_writer(), tool_call_id, summary, cfg,
                                               analysis_store=analysis_store),
                                DefaultCexHandler(llm, state, summarization_threshold=10),
                            )
                    return (r, res, all_rules)

                outcomes = await asyncio.gather(*(run_one(r) for r in pending))

            for (_r, res, _rules) in outcomes:
                if isinstance(res, str):  # a hard compile/toolchain error in any buffer aborts the pass
                    return res

            prover_update: list[ProverHistoryItem] = []
            fresh: dict[str, list[tuple[RulePath, StatusCodes]]] = {}
            link: str | None = None
            parts: list[str] = []
            for (r, res, all_rules) in outcomes:
                assert not isinstance(res, str)
                results: list[tuple[RulePath, StatusCodes]] = [(k, v) for (k, v) in res.raw_rule_status.items()]
                fresh[r.buffer.name] = results
                link = res.link or link
                parts.append(f"=== buffer {r.buffer.name} ===\n{res.result_str}")
                prover_update.append(ProverRunLog(
                    tool_call_id=tool_call_id,
                    prover_results=results,
                    rules=None,
                    spec_digest=string_hash(r.buffer.cvl),
                    sort="run",
                    declared_rules=all_rules,
                    state_digest=r.digest,
                    buffer=r.buffer.name,
                ))

            # Overall completion is the AND across every run target: fresh results for the ones we ran,
            # history for those skipped as already complete.
            history_after = state["prover_history"] + prover_update
            all_verified = all(
                buffer_is_complete(
                    history_after, buffer=b.name, curr_digest=digest_of(b),
                    expected_to_fail=expected, curr_status=fresh.get(b.name, []),
                    all_rules=list(b.owned_rules),
                )
                for b in targets
            )

            content = "\n\n".join(parts)
            if all_verified:
                return tool_state_update(
                    tool_call_id=tool_call_id, content=content, prover_link=link,
                    validations=stamper(state, state["version_history"]),
                    prover_history=prover_update,
                )
            return tool_state_update(
                tool_call_id=tool_call_id, content=content, prover_link=link,
                prover_history=prover_update,
            )

        if targets:
            async with spec_locks.setdefault("__buffers__", asyncio.Lock()), \
                    project_directory(state.get("vfs") or {}) as run_root:
                return await run_buffers(run_root, targets)
        async with lock, project_directory(state.get("vfs") or {}) as run_root:
            return await run_in(run_root)

    return verify_spec
