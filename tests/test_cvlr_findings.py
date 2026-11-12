"""What a violated CVLR rule becomes, and what it deliberately does not.

``docs/cvlr-backend-plan.md`` §7.7.3. Every run before this phase reported ``findings: []`` with
violated rules in it, for a reason that was pure plumbing: the formalizer built its prover deps
without an analysis handler, so nothing explained a counterexample, and it did not override
``findings_evidence``, whose base returns ``None`` — the documented way a backend opts out of
findings entirely.

The half that is not plumbing is the filter. A rule can come back VIOLATED with a counterexample
attached and still say nothing about the program, because the assertion it broke was one the prover
generated for itself when it ran out of loop bound. The distinction has to be made *before* the
evidence reaches a findings synthesizer, which is happy to write up the prover's own limits as a
bug in the code under verification.
"""

import json
from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from composer.prover.core import ProverReport, TrivialFanoutCexHandler, _incomplete
from composer.prover.ptypes import GeneratedKind, IncompleteCheck, RulePath, RuleResult, StatusCodes
from composer.prover.results import (
    SOLANA_TRACE,
    Hotspot,
    RuleStatistics,
    Solving,
    StaticAnalysis,
    VerificationPhase,
    counterexample,
    prover_message,
)
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.pipeline import CvlrFormalizer
from composer.spec.source.report.schema import ConfinedBuilds, UnconfinedBuilds
from composer.spec.cvlr.verify import (
    AuthorModel,
    BuildPermit,
    CexAnalysis,
    _CaptureCallbacks,
    DOMINANT_SHARE,
    TIMEOUT_STREAK_WARNING,
    _verdicts_note,
    symptoms,
    timeout_streak,
    unaccounted,
)
from composer.spec.cvlr.state import LastVerdicts
from composer.spec.source.cex_capture import CexAnalysisStore

FIXTURES = Path(__file__).parent / "data" / "solana_cex"


def _idle_permit() -> BuildPermit:
    """A permit nothing acquired. These callbacks never see a link, so it is never released."""
    return BuildPermit(SimpleNamespace(build_sem=asyncio.Semaphore(1)))  # type: ignore[arg-type]


def _store() -> CexAnalysisStore:
    return CexAnalysisStore(store=InMemoryStore(), namespace=("cvlr_cex", "t"))


def _violated(rule: str, fixture: Path) -> RuleResult:
    """A violated rule carrying a real Solana counterexample."""
    return RuleResult(
        path=RulePath(rule=rule),
        counterexample=counterexample(json.loads(fixture.read_text()), SOLANA_TRACE),
        status="VIOLATED",
    )


_REAL_VIOLATION = FIXTURES / "assertion_failed" / "rule_output.json"
_LOOP_BOUND = FIXTURES / "loop_unwinding" / "Reports" / "treeView" / "rule_output_2.json"


@pytest.mark.asyncio
async def test_a_property_violation_is_kept_as_evidence():
    store = _store()
    rule = _violated("rule_withdraw_fee_collector_vault_aliasing", _REAL_VIOLATION)
    await _CaptureCallbacks(store, _idle_permit()).on_analysis_complete(rule, "the collector aliases the vault")

    records = await store.for_rule("rule_withdraw_fee_collector_vault_aliasing")
    assert [r.analysis for r in records] == ["the collector aliases the vault"]
    kept = records[0].counterexample
    assert kept is not None
    # The rendered element, values and all: this is what the findings model reads.
    assert "vault_lamports_before - vault_lamports_after: '99'" in kept
    assert "<assert>assertion failed</assert>" in kept


@pytest.mark.asyncio
async def test_a_loop_bound_the_prover_could_not_discharge_is_not_kept_as_evidence():
    """The gate this phase needed. This rule is VIOLATED, it has a counterexample, and there is no
    bug: the prover stopped. Recorded as evidence it becomes a written-up finding against the
    program."""
    store = _store()
    rule = _violated("rule_deposit_credits_exactly_the_amount", _LOOP_BOUND)
    await _CaptureCallbacks(store, _idle_permit()).on_analysis_complete(rule, "the loop needs a higher bound")

    assert await store.for_rule("rule_deposit_credits_exactly_the_amount") == []


@pytest.mark.asyncio
async def test_the_author_is_still_told_about_a_bound_it_could_raise():
    """The filter is on the *evidence* path only, and that split is deliberate. An unwound loop
    bound is worth an explanation — the author can raise ``loop_iter`` or constrain the loop — so
    nothing is filtered out of the handler's account to the author. Pinned by the fact that the
    callback never touches the handler's own analysis, which is what
    :class:`composer.prover.core.TrivialFanoutCexHandler` renders back into the tool result."""
    handled: list[str] = []

    class _Recording(_CaptureCallbacks):
        async def on_analysis_complete(self, rule, explanation):
            handled.append(explanation)
            await super().on_analysis_complete(rule, explanation)

    store = _store()
    rule = _violated("rule_deposit_credits_exactly_the_amount", _LOOP_BOUND)
    await _Recording(store, _idle_permit()).on_analysis_complete(rule, "raise loop_iter to 3")
    assert handled == ["raise loop_iter to 3"]
    assert await store.for_rule("rule_deposit_credits_exactly_the_amount") == []


@pytest.mark.asyncio
async def test_a_fresh_run_supersedes_what_an_earlier_iteration_captured():
    """A rule that failed in iteration 1 and passes in iteration 2 must not survive into the report
    as a current failure. The clear fires on ``on_prover_result``, which the runner calls before the
    handler analyzes anything."""
    store = _store()
    callbacks = _CaptureCallbacks(store, _idle_permit())
    rule = _violated("rule_withdraw_fee_collector_vault_aliasing", _REAL_VIOLATION)
    await callbacks.on_analysis_complete(rule, "first pass")
    assert await store.for_rule("rule_withdraw_fee_collector_vault_aliasing")

    passing = RuleResult(
        path=RulePath(rule="rule_withdraw_fee_collector_vault_aliasing"),
        counterexample=None,
        status="VERIFIED",
    )
    await callbacks.on_prover_result({passing.name: passing})
    assert await store.for_rule("rule_withdraw_fee_collector_vault_aliasing") == []


@pytest.mark.asyncio
async def test_the_backend_opts_into_findings_and_reads_back_what_it_captured():
    """The other half of ``findings: []``: a backend that captures evidence and never offers it is
    indistinguishable, from the report's side, from one that captured nothing."""
    store = _store()
    rule = _violated("rule_withdraw_fee_collector_vault_aliasing", _REAL_VIOLATION)
    await _CaptureCallbacks(store, _idle_permit()).on_analysis_complete(rule, "the collector aliases the vault")

    formalizer = CvlrFormalizer(
        object, "prover", SimpleNamespace(cex_analysis=store), SimpleNamespace(), ()
    )
    fetch = formalizer.findings_evidence()
    assert fetch is not None

    evidence = await fetch("rule_withdraw_fee_collector_vault_aliasing")
    assert [e.analysis for e in evidence] == ["the collector aliases the vault"]
    assert await fetch("a_rule_that_never_failed") == []


@pytest.mark.parametrize(
    ("sandbox", "expected"),
    [
        (SandboxConfig(provider="launcher"), ConfinedBuilds(provider="launcher")),
        (SandboxConfig(provider="none"), UnconfinedBuilds()),
    ],
)
def test_the_report_records_how_the_builds_that_earned_its_verdicts_ran(sandbox, expected):
    """``docs/cvlr-backend-plan.md`` §7.8.4: an unconfined run was marked on stderr and
    nowhere in the deliverable, so a report read a week later could not be told from a production
    one. The backend answers this unconditionally — the base's ``None`` means "compiles nothing",
    and a reader who cannot distinguish that from "nobody recorded it" has no use for the field."""
    formalizer = CvlrFormalizer(object, "prover", SimpleNamespace(sandbox=sandbox), SimpleNamespace(), ())

    assert formalizer.build_environment() == expected


# ---------------------------------------------------------------------------------------------
# What the author is told
#
# The filter above decides what the *report* claims. It also creates a way for the deliverable to
# contradict itself: an author whose gate is stuck on an unwinding violation can mark it with
# `expect_rule_failure` and publish a rule declared to expose a defect that contributes no evidence
# for one. These are what close that.


def test_the_gate_learns_which_rules_never_reached_their_property():
    """Classification happens while the results still carry counterexamples.

    The report keeps which rules stopped on an assertion the prover generated,
    and drops the counterexample.
    """
    results = {
        "loop": _violated("loop", _LOOP_BOUND),
        "real": _violated("real", _REAL_VIOLATION),
    }
    incomplete = _incomplete(results)

    assert set(incomplete) == {RulePath(rule="loop")}
    assert incomplete[RulePath(rule="loop")].kind == GeneratedKind.LOOP_UNWINDING


def test_a_run_where_nothing_stalled_reports_nothing_stalled():
    incomplete = _incomplete({"real": _violated("real", _REAL_VIOLATION)})
    assert incomplete == {}


def test_an_incomplete_check_cannot_be_declared_an_expected_failure():
    """``expect_rule_failure`` says "this failure is a real defect". A rule that stopped on the
    prover's own assertion has shown no defect, so the marking must not excuse it from the gate —
    otherwise the run publishes a claim of a finding for which nothing was captured."""
    status: dict[str, StatusCodes] = {"loop": "VIOLATED", "real": "VIOLATED"}
    expected = {"loop": "I think this is a bug", "real": "the fee collector aliases the vault"}

    assert unaccounted(status, expected, ()) == []
    assert unaccounted(status, expected, {"loop"}) == ["loop"]


def test_a_genuine_expected_failure_still_satisfies_the_gate():
    """The other direction, which is the whole reason the marking exists: a run can be complete with
    a real violation in it."""
    status: dict[str, StatusCodes] = {"real": "VIOLATED"}
    expected = {"real": "the fee collector aliases the vault"}
    assert unaccounted(status, expected, ()) == []


def _report_note(report: ProverReport) -> str:
    """The note ``verify_rules`` writes for ``report`` when nothing is marked expected-to-fail."""
    status = report.rule_outcomes()
    incomplete = {path.rule: check for path, check in report.incomplete.items()}
    return _verdicts_note(
        status, incomplete, sorted(status), [], symptoms=symptoms(report), stamped=False
    )


def _report(status: dict[str, StatusCodes], **fields) -> ProverReport:
    return ProverReport(
        raw_rule_status={RulePath(rule=name): code for name, code in status.items()},
        result_str="",
        link="L",
        certora_run_stdout="",
        **fields,
    )


def _note(**incomplete: IncompleteCheck) -> str:
    return _report_note(
        _report(
            {name: "VIOLATED" for name in incomplete},
            incomplete={RulePath(rule=name): check for name, check in incomplete.items()},
        )
    )


def _errored(**messages: str) -> str:
    return _report_note(
        _report(
            {name: "ERROR" for name in messages},
            errors={RulePath(rule=name): [prover_message(m)] for name, m in messages.items()},
        )
    )


def _overflow(assertion: str) -> IncompleteCheck:
    return IncompleteCheck(GeneratedKind.CANNOT_OVERFLOW, assertion)


def test_the_gate_tells_an_overflow_stop_how_to_get_past_it_and_not_a_loop_remedy():
    note = _note(r=_overflow("Cannot overflow: inferred pointer"))
    assert "Cannot overflow: inferred pointer" in note
    assert "cvlr_assume!(x < 1 << 63)" in note
    assert "trip count" not in note


def test_the_gate_bounds_an_overflow_stop_once_and_then_moves_on():
    """The bound is not always the lever: the stop follows the code's layout, so a rule that still
    stops with it in place is restated below the handler or skipped, not reworded again."""
    note = " ".join(_note(r=_overflow("Cannot overflow: inferred pointer")).split())
    assert "once" in note
    assert "comes back with the bound in place" in note
    assert "takes no `AccountInfo`" in note
    assert "skip the property" in note


def test_the_gate_gives_each_kind_its_remedy_once():
    note = _note(
        a=IncompleteCheck(GeneratedKind.LOOP_UNWINDING, "Unwinding condition in a loop"),
        b=_overflow("Cannot overflow: computing TAC map index"),
        c=_overflow("Cannot overflow: inferred pointer"),
    )
    assert note.count("### K4.") == 1
    assert note.count("### K5.") == 1
    assert "expect_rule_failure" in note


def test_the_gate_pushes_the_pointer_analysis_recipe_on_an_error_code():
    """An ERROR carries no counterexample, so its code is the only thing a recipe can be keyed on."""
    note = _errored(
        r="[3308] illegal dereference of an absolute address",
        s="In s: [3006] illegal store of a stack pointer",
    )
    assert note.count("### K10.") == 1
    assert "### K11." not in note


def test_the_gate_pushes_the_sizing_recipe_on_a_dynamic_memcpy():
    note = _errored(r="[3005] memcpy with dynamically sized length")
    assert "### K11." in note and "### K10." not in note


def test_an_error_with_no_listed_code_pushes_nothing():
    assert "### K" not in _errored(r="Rule r timed-out")


def test_the_gate_pushes_the_vacuity_recipe_on_a_sanity_failure():
    assert "### K12." in _report_note(_report({"r": "SANITY_FAILED", "s": "VERIFIED"}))


def test_the_gate_pushes_the_timeout_recipe_on_a_rule_that_ran_out_of_its_own_time():
    assert "### K8." in _report_note(_report({"r": "TIMEOUT", "s": "VERIFIED"}))
    assert "### K8." not in _report_note(_report({"r": "VERIFIED"}))


def test_a_halted_jobs_cut_off_rules_push_only_the_halted_job_recipe():
    note = _report_note(_report({"r": "TIMEOUT", "s": "VERIFIED"}, halted=True))
    assert "### K9." in note and "### K8." not in note


def test_the_gate_pushes_the_stand_in_recipe_on_a_rule_that_never_reached_the_solver():
    """Even in a halted job, where a TIMEOUT alone says only that the job ran out."""
    def stopped(phase: VerificationPhase) -> ProverReport:
        stats = RuleStatistics(phase=phase, path_count=None, branching=(), nonlinear=())
        return _report({"r": "TIMEOUT"}, statistics={RulePath(rule="r"): stats}, halted=True)

    assert "### K8." in _report_note(stopped(StaticAnalysis()))
    assert "### K8." not in _report_note(stopped(Solving(4, 50, 2, 10)))


def test_the_gate_pushes_the_stand_in_recipe_when_one_function_holds_the_time():
    def stopped(branching: int | None, nonlinear: int | None) -> ProverReport:
        stats = RuleStatistics(
            phase=Solving(4, 50, 2, 10),
            path_count=None,
            branching=() if branching is None else (Hotspot("f", branching),),
            nonlinear=() if nonlinear is None else (Hotspot("g", nonlinear),),
        )
        return _report({"r": "TIMEOUT"}, statistics={RulePath(rule="r"): stats}, halted=True)

    assert "### K8." in _report_note(stopped(DOMINANT_SHARE, None))
    assert "### K8." in _report_note(stopped(10, DOMINANT_SHARE + 30))
    assert "### K8." not in _report_note(stopped(DOMINANT_SHARE - 1, DOMINANT_SHARE - 1))
    assert "### K8." not in _report_note(stopped(None, None))


def test_the_gate_pushes_the_halted_job_recipe_when_the_job_hit_its_limit():
    assert "### K9." in _report_note(_report({"r": "TIMEOUT", "s": "VERIFIED"}, halted=True))
    assert "### K9." not in _report_note(_report({"r": "TIMEOUT", "s": "VERIFIED"}))


def test_the_cvlr_author_reads_the_prover_report_whole() -> None:
    """An error's ``[NNNN]`` text and its ``source:`` chain reach the author only through the
    report, and a summarized report rewrites every violation as something to fix."""
    model = AuthorModel()
    model.bind(SimpleNamespace())  # type: ignore[arg-type]
    handler = CexAnalysis(store=_store(), model=model).handler({"messages": []})  # type: ignore[arg-type]
    assert isinstance(handler, TrivialFanoutCexHandler)
    assert handler.summarization_threshold is None


def _last(streak: int) -> LastVerdicts:
    return LastVerdicts(status={}, incomplete=[], unverdicted=[], draft="d", timeout_streak=streak)


def test_the_timeout_streak_counts_runs_in_a_row_with_a_timed_out_rule():
    timed_out: dict[str, StatusCodes] = {"a": "VERIFIED", "b": "TIMEOUT"}
    assert timeout_streak(timed_out, None) == 1
    assert timeout_streak(timed_out, _last(2)) == 3


def test_a_run_with_no_timed_out_rule_ends_the_streak():
    assert timeout_streak({"a": "VERIFIED", "b": "VIOLATED"}, _last(5)) == 0


def test_the_note_tells_the_author_to_stop_restating_only_once_the_streak_is_long():
    status: dict[str, StatusCodes] = {"b": "TIMEOUT"}

    def note(streak: int) -> str:
        return _verdicts_note(status, {}, ["b"], [], stamped=False, timeout_streak=streak)

    assert "Do not restate" not in note(TIMEOUT_STREAK_WARNING - 1)
    assert f"{TIMEOUT_STREAK_WARNING} runs in a row" in note(TIMEOUT_STREAK_WARNING)
