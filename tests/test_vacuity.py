"""
Unit tests for composer/prover/vacuity.py: vacuous-method detection over
synthetic RuleResult sets, alert rendering, the filtered-block /
documented-repair spec-text checks, and the verify_spec hard guard that
withholds the PROVER validation stamp for undocumented filters — or
undocumented expect_rule_failure skips — of detected-vacuous methods.
"""

import pytest

from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath, RuleResult, StatusCodes
from composer.prover.vacuity import (
    VacuityEvidence,
    detect_vacuous_methods,
    format_vacuity_alert,
    instantiated_methods,
    undocumented_filtered_vacuous,
    undocumented_skipped_vacuous,
)
from composer.spec.cvl_generation import check_completion
from composer.spec.source.author import ExpectRuleFailure
from composer.spec.source.prover import StateWithSkips, VALIDATION_KEY

from graphcore.testing import Scenario, tool_call_raw, ToolCallDict
from graphcore.tools.results import result_tool_generator

from .conftest import ProverMock, ProverToolResponse


def _res(rule: str, method: str | None, status: StatusCodes) -> RuleResult:
    return RuleResult(
        path=RulePath(rule=rule, contract="Bank", method=method),
        cex_dump=None,
        status=status,
    )


DEPOSIT = "Bank.deposit(uint256)"
WITHDRAW = "Bank.withdraw(uint256)"


# =========================================================================
# detect_vacuous_methods
# =========================================================================


class TestDetectVacuousMethods:
    def test_vacuous_in_all_rules(self):
        """Sanity-failed in 100% of the (3) instantiating rules -> flagged."""
        results = [
            _res(r, DEPOSIT, "SANITY_FAILED") for r in ("ruleA", "ruleB", "ruleC")
        ]
        flagged = detect_vacuous_methods(results)
        assert set(flagged) == {DEPOSIT}
        assert flagged[DEPOSIT].affected_rules == ["ruleA", "ruleB", "ruleC"]
        assert "3 of 3" in flagged[DEPOSIT].diagnosis

    def test_vacuous_in_single_rule_run(self):
        """One rule, one instantiation, sanity-failed: 100% -> flagged."""
        flagged = detect_vacuous_methods([_res("ruleA", DEPOSIT, "SANITY_FAILED")])
        assert set(flagged) == {DEPOSIT}

    def test_sanity_failed_in_one_of_many_not_flagged(self):
        """Failed in 1 of 3 rules: below the >=2 threshold and not 100%."""
        results = [
            _res("ruleA", DEPOSIT, "SANITY_FAILED"),
            _res("ruleB", DEPOSIT, "VERIFIED"),
            _res("ruleC", DEPOSIT, "VERIFIED"),
        ]
        assert detect_vacuous_methods(results) == {}

    def test_two_rules_flagged_even_when_others_pass(self):
        """>=2 sanity-failed rules flag the method even below 100%."""
        results = [
            _res("ruleA", DEPOSIT, "SANITY_FAILED"),
            _res("ruleB", DEPOSIT, "SANITY_FAILED"),
            _res("ruleC", DEPOSIT, "VERIFIED"),
        ]
        assert set(detect_vacuous_methods(results)) == {DEPOSIT}

    def test_mixed_methods(self):
        """Only the everywhere-failing method is flagged in a mixed result set."""
        results = [
            _res("ruleA", DEPOSIT, "SANITY_FAILED"),
            _res("ruleB", DEPOSIT, "SANITY_FAILED"),
            _res("ruleA", WITHDRAW, "VERIFIED"),
            _res("ruleB", WITHDRAW, "SANITY_FAILED"),
            _res("ruleC", WITHDRAW, "VIOLATED"),
        ]
        assert set(detect_vacuous_methods(results)) == {DEPOSIT}

    def test_non_parametric_sanity_failure_ignored(self):
        """SANITY_FAILED with no method is a rule problem, not method vacuity."""
        assert detect_vacuous_methods([_res("ruleA", None, "SANITY_FAILED")]) == {}

    def test_instantiated_methods(self):
        results = [
            _res("ruleA", DEPOSIT, "VERIFIED"),
            _res("ruleB", WITHDRAW, "SANITY_FAILED"),
            _res("static", None, "VERIFIED"),
        ]
        assert instantiated_methods(results) == {DEPOSIT, WITHDRAW}


# =========================================================================
# format_vacuity_alert
# =========================================================================


class TestFormatVacuityAlert:
    def test_empty_evidence_renders_nothing(self):
        assert format_vacuity_alert({}) == ""

    def test_alert_contains_ladder_and_methods(self):
        evidence = detect_vacuous_methods(
            [_res("ruleA", DEPOSIT, "SANITY_FAILED"), _res("ruleB", DEPOSIT, "SANITY_FAILED")]
        )
        alert = format_vacuity_alert(evidence)
        assert alert.startswith("<vacuity_alert>")
        assert alert.endswith("</vacuity_alert>")
        assert DEPOSIT in alert
        # Repair ladder ordering: summary fix -> mock -> optimistic_fallback -> filtered.
        assert (
            alert.index("Fix or replace the offending summary")
            < alert.index("write_mock")
            < alert.index("optimistic_fallback")
            < alert.index("filtered")
        )


# =========================================================================
# undocumented_filtered_vacuous
# =========================================================================


_UNDOCUMENTED_SPEC = """\
rule solvency(method f) filtered { f -> f.selector != sig:deposit(uint256).selector } {
    assert true;
}
"""

_DOCUMENTED_SPEC = """\
// deposit is excluded: attempted to fix the NONDET summary on the payable receiver (typecheck
// rejected the replacement), then a mock under certora/mocks (compilation failed on the
// constructor), then optimistic_fallback (the revert persisted). Filtering as a last resort.
rule solvency(method f) filtered { f -> f.selector != sig:deposit(uint256).selector } {
    assert true;
}
"""

_NO_FILTER_SPEC = """\
rule solvency(method f) {
    assert true;
}
"""


class TestUndocumentedFilteredVacuous:
    def test_undocumented_filter_blocked(self):
        assert undocumented_filtered_vacuous(_UNDOCUMENTED_SPEC, [DEPOSIT]) == [DEPOSIT]

    def test_documented_filter_passes(self):
        assert undocumented_filtered_vacuous(_DOCUMENTED_SPEC, [DEPOSIT]) == []

    def test_method_not_filtered_passes(self):
        assert undocumented_filtered_vacuous(_NO_FILTER_SPEC, [DEPOSIT]) == []

    def test_other_method_in_filter_passes(self):
        assert undocumented_filtered_vacuous(_UNDOCUMENTED_SPEC, [WITHDRAW]) == []

    def test_one_documented_occurrence_suffices(self):
        """A method filtered in two rules passes if either filter is documented."""
        spec = _UNDOCUMENTED_SPEC + "\n" + _DOCUMENTED_SPEC.replace("solvency", "shares")
        assert undocumented_filtered_vacuous(spec, [DEPOSIT]) == []

    def test_multiple_methods_sorted(self):
        spec = """\
rule a(method f) filtered { f -> f.selector != sig:deposit(uint256).selector
                              && f.selector != sig:withdraw(uint256).selector } {
    assert true;
}
"""
        assert undocumented_filtered_vacuous(spec, [WITHDRAW, DEPOSIT]) == [DEPOSIT, WITHDRAW]

    def test_prefix_collision_not_matched(self):
        """Token-boundary matching: vacuous `deposit` must NOT count as contained
        in a filter that only excludes `depositAll` (raw-substring prefix hit)."""
        spec = """\
rule a(method f) filtered { f -> f.selector != sig:depositAll().selector } {
    assert true;
}
"""
        assert undocumented_filtered_vacuous(spec, [DEPOSIT]) == []


# =========================================================================
# undocumented_skipped_vacuous
# =========================================================================


_DOCUMENTED_SKIP_REASON = (
    "solvency sanity-fails because deposit is vacuous; attempted to fix the NONDET "
    "summary on the payable receiver, then a mock under certora/mocks, then "
    "optimistic_fallback — the revert persisted in all three."
)


class TestUndocumentedSkippedVacuous:
    def test_undocumented_skip_blocked(self):
        evidence = detect_vacuous_methods(
            [_res("ruleA", DEPOSIT, "SANITY_FAILED"), _res("ruleB", DEPOSIT, "SANITY_FAILED")]
        )
        skips = {"ruleA": "flaky rule", "ruleB": "known to fail"}
        assert undocumented_skipped_vacuous(evidence, skips) == [DEPOSIT]

    def test_documented_skip_passes(self):
        evidence = detect_vacuous_methods(
            [_res("ruleA", DEPOSIT, "SANITY_FAILED"), _res("ruleB", DEPOSIT, "SANITY_FAILED")]
        )
        skips = {"ruleA": _DOCUMENTED_SKIP_REASON, "ruleB": "known to fail"}
        assert undocumented_skipped_vacuous(evidence, skips) == []

    def test_unskipped_rules_pass(self):
        """A vacuous verdict with no affected rule in rule_skips never blocks here
        (the still-failing rules block all_verified instead)."""
        evidence = detect_vacuous_methods(
            [_res("ruleA", DEPOSIT, "SANITY_FAILED"), _res("ruleB", DEPOSIT, "SANITY_FAILED")]
        )
        assert undocumented_skipped_vacuous(evidence, {"unrelated": "expected CEX"}) == []

    def test_empty_evidence_passes(self):
        assert undocumented_skipped_vacuous({}, {"ruleA": "flaky"}) == []


# =========================================================================
# verify_spec filtered-vacuous hard guard (mocked prover)
# =========================================================================

_PROVER = "verify_spec"
_RESULT = "result"

_result_tool = result_tool_generator(
    "result",
    (str, "Commentary"),
    "Signal completion",
    validator=(StateWithSkips, lambda st, *_: check_completion(st)),
)


def _verify() -> ToolCallDict:
    return tool_call_raw(_PROVER, rules=None)


def _result(commentary: str) -> ToolCallDict:
    return tool_call_raw(_RESULT, value=commentary)


def _vacuous_report(*, rule_status: dict[str, bool], vacuous: dict[str, VacuityEvidence] | None = None,
                    instantiated: set[str] | None = None) -> ProverReport:
    return ProverReport(
        rule_status=rule_status,
        result_str="Prover report output",
        link="local://test-run",
        vacuous_methods=vacuous or {},
        instantiated_methods=instantiated or set(),
    )


_DEPOSIT_EVIDENCE = {
    DEPOSIT: VacuityEvidence(
        method=DEPOSIT, affected_rules=["solvency"], diagnosis="sanity-failed in 1 of 1 rule(s)",
    )
}


def _guard_scenario(certora_prover: ProverMock, *responses: ProverToolResponse, curr_spec: str):
    prover_tool = certora_prover(responses)
    return Scenario(StateWithSkips, prover_tool, ExpectRuleFailure.as_tool("expect_rule_failure"), _result_tool).init(
        curr_spec=curr_spec,
        skipped=[],
        property_rules=[],
        validations={},
        required_validations=[VALIDATION_KEY],
        rule_skips={},
        vacuous_methods={},
        config={"files": ["src/Foo.sol"]},
    )


def _result_accepted(st: StateWithSkips) -> bool:
    return "result" in st


class TestVerifySpecVacuityGuard:
    """Run 1 detects the vacuous method; run 2 passes because the spec filters it.
    The stamp must be withheld unless the filter documents a repair attempt."""

    @pytest.mark.asyncio
    async def test_undocumented_filter_withholds_stamp(self, certora_prover: ProverMock):
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
            curr_spec=_UNDOCUMENTED_SPEC,
        )
        accepted = await scenario.turns(
            _verify(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert not accepted

    @pytest.mark.asyncio
    async def test_guard_message_names_method_and_ladder(self, certora_prover: ProverMock):
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
            curr_spec=_UNDOCUMENTED_SPEC,
        )
        msg = await scenario.turn(_verify()).turn(_verify()).run_last_single_tool(_PROVER)
        assert "vacuity_filter_guard" in msg
        assert "WITHHELD" in msg
        assert DEPOSIT in msg

    @pytest.mark.asyncio
    async def test_documented_filter_grants_stamp(self, certora_prover: ProverMock):
        """Escape hatch: a repair-ladder attempt documented near the filter."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
            curr_spec=_DOCUMENTED_SPEC,
        )
        accepted = await scenario.turns(
            _verify(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted

    @pytest.mark.asyncio
    async def test_no_filter_no_guard(self, certora_prover: ProverMock):
        """A vacuous verdict alone never blocks — only filtering it does."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
            curr_spec=_NO_FILTER_SPEC,
        )
        accepted = await scenario.turns(
            _verify(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted

    @pytest.mark.asyncio
    async def test_undocumented_rule_skip_withholds_stamp(self, certora_prover: ProverMock):
        """The expect_rule_failure bypass: instead of filtering the vacuous method,
        the agent marks its sanity-failing rule "expected to fail". The rule drops
        out of the all-verified check, but the stamp must still be withheld."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            curr_spec=_NO_FILTER_SPEC,
        )
        accepted = await scenario.turns(
            _verify(),
            tool_call_raw("expect_rule_failure", rule_name="solvency", reason="flaky rule"),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert not accepted

    @pytest.mark.asyncio
    async def test_skip_guard_message_names_method_and_tools(self, certora_prover: ProverMock):
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            curr_spec=_NO_FILTER_SPEC,
        )
        msg = await (
            scenario.turn(_verify())
            .turn(tool_call_raw("expect_rule_failure", rule_name="solvency", reason="flaky rule"))
            .turn(_verify())
            .run_last_single_tool(_PROVER)
        )
        assert "vacuity_skip_guard" in msg
        assert "WITHHELD" in msg
        assert DEPOSIT in msg
        assert "expect_rule_passage" in msg

    @pytest.mark.asyncio
    async def test_documented_rule_skip_grants_stamp(self, certora_prover: ProverMock):
        """Escape hatch: the skip reason documents the attempted repair-ladder steps."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            curr_spec=_NO_FILTER_SPEC,
        )
        accepted = await scenario.turns(
            _verify(),
            tool_call_raw(
                "expect_rule_failure", rule_name="solvency", reason=_DOCUMENTED_SKIP_REASON
            ),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted

    @pytest.mark.asyncio
    async def test_healthy_reinstantiation_clears_verdict(self, certora_prover: ProverMock):
        """Run 2 instantiates the method without a sanity failure (the setup was
        repaired) — the vacuity verdict clears and the filter no longer blocks."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}, instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
            curr_spec=_UNDOCUMENTED_SPEC,
        )
        accepted = await scenario.turns(
            _verify(),
            _verify(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted
