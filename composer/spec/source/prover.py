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
from pydantic import BaseModel, Field, Discriminator

from langgraph.config import get_stream_writer
from langgraph.types import Command
from composer.prover.ptypes import RuleResult, RulePath
from graphcore.graph import LLM

from composer.prover.core import (
    ProverOptions, SpecCompilationError, ProverReport, declared_rules_list, run_prover,
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
from composer.spec.source.verification_groups import (
    VerificationGroup,
    merge_group_results,
    prune_phantom_owned_rules,
    resolved_max_groups,
    single_group,
)
from composer.spec.source.agent_groups import VerificationGroupSpec, groups_from_specs


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
    # The verification group this run belongs to; absent (None) for an ungrouped run.
    group: NotRequired[str | None]

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

#: Statuses that make a rule "stuck" — a failure worth warning/nagging about (a genuine
#: VIOLATED is a real verdict, not stuck).
STUCK_STATUSES: frozenset[StatusCodes] = frozenset({"TIMEOUT", "ERROR", "SANITY_FAILED"})


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

def _history_for_group(
    l: list[ProverHistoryItem], group: str | None
) -> list[ProverHistoryItem]:
    """The history entries relevant to one verification group's completion: that
    group's own runs, plus every nag marker (nags are group-agnostic and are
    transparent to ``_iterate_history``). Runs belonging to a *different* group are
    dropped, so a group's contiguous same-digest streak is not truncated by another
    group's interleaved run at a different digest. An untagged run (``group`` absent)
    belongs to the default group ``None`` — the single-group / backward-compatible case."""
    return [it for it in l if it["sort"] != "run" or it.get("group") == group]


def group_is_complete(
    l: list[ProverHistoryItem],
    *,
    group: str | None,
    curr_digest: str,
    expected_to_fail: set[str],
    curr_status: list[tuple[RulePath, StatusCodes]],
    owned_rules: set[str],
) -> bool:
    """Whether one group's owned rules are all verified against its current spec state.

    Delegates to :func:`_is_completion_history` over the group's own filtered history,
    so a group with a distinct spec (distinct ``state_digest``) is evaluated in isolation
    from interleaved runs of other groups. Overall verification completeness is the AND of
    this over every group."""
    return _is_completion_history(
        l=_history_for_group(l, group),
        curr_digest=curr_digest,
        expected_to_fail=expected_to_fail,
        curr_status=curr_status,
        all_rules=list(owned_rules),
    )


def group_pending_rules(
    l: list[ProverHistoryItem],
    *,
    group: str | None,
    curr_digest: str,
    owned_rules: set[str],
    expected_to_fail: set[str],
    curr_status: list[tuple[RulePath, StatusCodes]] | None = None,
) -> set[str]:
    """The owned rules a re-run of this group still needs to cover: those whose *latest*
    verdict against the group's current spec state is not ``VERIFIED`` (and that are not
    expected to fail), plus any never yet run. This is the incremental re-run engine —
    verified rules are never re-submitted; a timed-out / spuriously-violated rule is, on
    its own, within its group's setup. Empty means the group is fully covered and its run
    can be skipped entirely."""
    latest: dict[str, StatusCodes] = {}
    for results in _iterate_history(_history_for_group(l, group), curr_digest, list(curr_status or [])):
        for (path, status) in results:  # newest-first: first seen is the latest verdict
            if path.rule in owned_rules and path.rule not in latest:
                latest[path.rule] = status
    return {
        rule for rule in owned_rules
        if rule not in expected_to_fail and latest.get(rule) != "VERIFIED"
    }


@dataclass(frozen=True)
class GroupRun:
    """One verification group's execution decision for a single verify pass."""
    group: VerificationGroup
    #: The group's current spec-state digest (keys its completion history).
    digest: str
    #: Owned rules to (re-)submit this pass. Empty means the group is already
    #: fully covered at this digest, so its prover run is skipped entirely.
    pending: frozenset[str]


def plan_group_execution(
    groups: Sequence[VerificationGroup],
    *,
    history: list[ProverHistoryItem],
    all_rules: list[str],
    agent_rules: list[str] | None,
    agent_exclude: list[str] | None,
    expected_to_fail: set[str],
    digest_of: Callable[[VerificationGroup], str],
) -> list[GroupRun]:
    """Decide, per group, which owned rules this verify pass (re-)submits.

    Each group's pending set is its owned rules not yet verified at its current
    digest (:func:`group_pending_rules`) intersected with any explicit agent rule
    selection. An empty pending set marks a group already fully covered — the
    executor skips its run. Pure and total (never runs the prover), so the
    parallel executor and this decision can be tested apart."""
    if agent_rules is not None:
        selection = set(agent_rules)
    elif agent_exclude is not None:
        selection = set(all_rules) - set(agent_exclude)
    else:
        selection = set(all_rules)
    plan: list[GroupRun] = []
    for group in groups:
        digest = digest_of(group)
        pending = group_pending_rules(
            history,
            group=group.name,
            curr_digest=digest,
            owned_rules=set(group.owned_rules),
            expected_to_fail=expected_to_fail,
        )
        plan.append(GroupRun(group=group, digest=digest, pending=frozenset(pending & selection)))
    return plan


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

    # The agent's declared verification-group partition (see agent_groups), set via the
    # group-declaration tool; absent => the single-spec/single-run default. Replaced wholesale
    # on redeclaration.
    verification_groups: NotRequired[list[VerificationGroupSpec]]

type ProverEvents = CEXAnalysisStart | CloudPollingEvent | ProverOutputEvent | RuleAnalysisResult | ProverRun | ProverLink | ProverResult

# ``verify_spec`` only runs in the source pipeline, whose state always seeds
# ``version_history`` — permanently empty in phases without the edit tools
# (structural invariants, never-edited authors), in which case it contributes
# nothing to the digest. The prover's validation stamp is bound to it so a
# post-run edit invalidates the stamp.
class StateWithSkips(CVLGenerationState, ProverStateExtra, VersionedHistory):
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

        spec = state["curr_spec"]
        if spec is None:
            return "Specification not yet put on VFS"

        spec_hash = string_hash(
            spec
        )

        if (last_run := last_prover_run(state["prover_history"])) is not None:
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


        def _finalize_run(
            *,
            status_map: Mapping[RulePath, StatusCodes],
            prover_update: list[ProverHistoryItem],
            all_verified: bool,
            content: str,
            link: str | None,
        ) -> Command:
            """Post-run bookkeeping shared by the grouped and single-run paths: nag on rules stuck on
            repeated identical failures, then build the tool_state_update. The caller supplies this
            pass's own status map, history items, completion verdict, and result text/link."""
            stuck_rules = {
                k: v for (k, v) in status_map.items()
                if v in STUCK_STATUSES and k.rule not in state["rule_skips"]
            }
            known_tc_ids = {
                l["id"] for msg in state["messages"] if isinstance(msg, AIMessage)
                for l in msg.tool_calls if l["name"] == "verify_spec"
            }
            to_warn, seen_post_compaction_history = stuck_rule_warnings(
                stuck_rules, state["prover_history"], known_tc_ids
            )
            nag_channel: dict = {}
            if len(to_warn) > 0:
                prover_update.append(NagMarker(sort="nag", nagged_rules=list(to_warn)))
                nag_channel["reminders_channel"] = [
                    "The following rule(s) have had identical failures on the last 3 runs of the prover:",
                    *(f"- {it.pprint()}" for it in to_warn),
                    "You may need to significantly change your approach, or skip the property if this is a persistent issue (you may need to use rebuttals to communicate"
                    " these failures to the feedback judge).",
                    "If these are TIMEOUTs, re-running the same spec will not help. Consider "
                    "`declare_verification_groups` to split the properties into parallel runs, each keeping only "
                    "what it needs precise and summarizing the rest — a monolithic run pays the intersection of every "
                    "rule's precision needs.",
                ]
                if seen_post_compaction_history:
                    nag_channel["reminders_channel"].append(
                        "(NB: Some of these prover calls happened before your most recent task history summarization)"
                    )
            if all_verified:
                nag_channel.setdefault("reminders_channel", []).append(
                    "You have successfully verified over your prior prover run(s) that all rules verify. This task is completed."
                )
                return tool_state_update(
                    tool_call_id=tool_call_id, content=content,
                    prover_link=link, validations=stamper(state, state["version_history"]),
                    prover_history=prover_update, **nag_channel
                )
            return tool_state_update(
                tool_call_id=tool_call_id, content=content, prover_link=link,
                prover_history=prover_update, **nag_channel
            )

        async def run_grouped(
            run_root: str, all_rules: list[str], groups: list[VerificationGroup]
        ) -> str | Command:
            """Verify a multi-group partition: each group runs its pending owned rules
            under its own spec/conf, concurrently, and the per-group verdicts recombine.

            Groups run in parallel via ``asyncio.gather`` — on cloud, ``sem`` is a no-op,
            so the submissions are genuinely concurrent; each group writes its spec/conf
            under a group-distinct name so the parallel same-stem writes don't race.
            Completion is the AND of per-group completeness (a fully-covered group is
            skipped and passes trivially); history entries are tagged with their group so
            :func:`group_is_complete` evaluates each group over its own digest streak."""
            expected = set(state["rule_skips"].keys())

            def digest_of(g: VerificationGroup) -> str:
                return spec_digest(
                    g.spec_contents if g.spec_contents is not None else spec,
                    state["skipped"], state["version_history"],
                )

            # Drop phantom owned rules — declared but absent from the compiled spec; warn once.
            groups, phantom = prune_phantom_owned_rules(groups, all_rules)
            if phantom:
                _logger.warning(
                    "verification groups: declared rule(s) absent from the compiled spec, ignored: %s",
                    sorted(phantom),
                )

            plan = plan_group_execution(
                groups, history=state["prover_history"], all_rules=all_rules,
                agent_rules=rules, agent_exclude=exclude_rules,
                expected_to_fail=expected, digest_of=digest_of,
            )

            async def run_one(gr: GroupRun) -> tuple[GroupRun, ProverReport | str | None]:
                if not gr.pending:
                    return (gr, None)  # already fully covered — skip its run
                g = gr.group
                gspec = g.spec_contents if g.spec_contents is not None else spec
                gstem = f"{spec_stem}__{g.name}" if spec_stem is not None else None
                with setup_prover_config_in(
                    working_dir=run_root,
                    main_contract=main_contract,
                    spec_stem=gstem,
                    spec_contents=gspec,
                    conf_dir=conf_dir,
                    config={**conf, **g.conf_overlay},
                    rule=sorted(gr.pending),
                    exclude_rule=None,
                    msg=f"{component} [{g.name}] iteration number {iteration}",
                ) as (config_path, cfg):
                    async with sem:
                        res = await run_prover(
                            Path(run_root), [config_path], tool_call_id, prover_opts,
                            _SpecCallbacks(get_stream_writer(), tool_call_id, summary, cfg,
                                           analysis_store=analysis_store),
                            DefaultCexHandler(llm, state, summarization_threshold=10),
                        )
                return (gr, res)

            runs = await asyncio.gather(*(run_one(gr) for gr in plan))

            # A hard toolchain error (str) aborts the pass; otherwise sort into the
            # groups that actually ran vs. those skipped as already-covered.
            outcomes: list[tuple[GroupRun, ProverReport | None]] = []
            for (gr, res) in runs:
                if isinstance(res, str):
                    return res
                outcomes.append((gr, res))
            executed = [(gr, res) for (gr, res) in outcomes if res is not None]
            merged = merge_group_results([(gr.group, res.raw_rule_status) for (gr, res) in executed])
            combined_str = "\n\n".join(
                f"=== group {gr.group.name} ===\n{res.result_str}" for (gr, res) in executed
            ) or "All verification groups already covered by prior runs; nothing to re-run."
            link = next((res.link for (_gr, res) in executed if res.link is not None), None)

            prover_update: list[ProverHistoryItem] = [
                ProverRunLog(
                    tool_call_id=tool_call_id,
                    prover_results=[(k, v) for (k, v) in res.raw_rule_status.items()],
                    rules={"sort": "include", "selector": sorted(gr.pending)},
                    spec_digest=string_hash(gr.group.spec_contents if gr.group.spec_contents is not None else spec),
                    sort="run",
                    declared_rules=all_rules,
                    state_digest=gr.digest,
                    group=gr.group.name,
                )
                for (gr, res) in executed
            ]
            all_verified = all(
                group_is_complete(
                    state["prover_history"], group=gr.group.name, curr_digest=gr.digest,
                    expected_to_fail=expected,
                    curr_status=[(k, v) for (k, v) in res.raw_rule_status.items()] if res is not None else [],
                    owned_rules=set(gr.group.owned_rules),
                )
                for (gr, res) in outcomes
            )
            return _finalize_run(
                status_map=merged, prover_update=prover_update,
                all_verified=all_verified, content=combined_str, link=link,
            )

        async def run_in(run_root: str) -> str | Command:
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
            # Groups from the agent's declaration, else one shared-spec group. Only a genuine
            # split — more than one group, or a group carrying its own spec/conf — takes
            # run_grouped; a trivial single group falls through to the single-run path below.
            declared = state.get("verification_groups")
            if declared:
                groups = groups_from_specs(
                    spec, list(declared),
                    cap=resolved_max_groups(),
                )
            else:
                groups = single_group(all_rules)
            if len(groups) > 1 or groups[0].spec_contents is not None or groups[0].conf_overlay:
                return await run_grouped(run_root, all_rules, groups)
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

            curr_state_digest = spec_digest(
                spec, state["skipped"], state["version_history"]
            )
            prover_results: list[tuple[RulePath, StatusCodes]] = [(k, v) for (k, v) in result.raw_rule_status.items()]
            all_verified = _is_completion_history(
                l=state["prover_history"],
                curr_digest=curr_state_digest,
                expected_to_fail=set(state["rule_skips"].keys()),
                curr_status=prover_results,
                all_rules=all_rules
            )
            prover_update: list[ProverHistoryItem] = [
                ProverRunLog(
                    tool_call_id=tool_call_id,
                    prover_results=prover_results,
                    rules={"sort": "exclude", "selector": exclude_rules } if exclude_rules is not None else \
                        {"sort": "include", "selector": rules} if rules is not None else None,
                    spec_digest=spec_hash,
                    sort="run",
                    declared_rules=all_rules,
                    state_digest=curr_state_digest
                )
            ]
            return _finalize_run(
                status_map=result.raw_rule_status, prover_update=prover_update,
                all_verified=all_verified, content=result.result_str, link=result.link,
            )

        # The author's working copy decides where this run executes (in-situ for
        # an empty VFS, a temp materialization otherwise); the same-stem lock
        # guards the deterministic spec/conf names within it.
        async with lock, project_directory(state.get("vfs") or {}) as run_root:
            return await run_in(run_root)

    return verify_spec
