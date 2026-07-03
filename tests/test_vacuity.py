"""
Unit tests for composer/prover/vacuity.py: vacuous-method detection over
synthetic RuleResult sets, alert rendering, and the filtered-block /
documented-repair spec-text checks backing the verify_spec guard.
"""

from composer.prover.ptypes import RulePath, RuleResult, StatusCodes
from composer.prover.vacuity import (
    detect_vacuous_methods,
    format_vacuity_alert,
    instantiated_methods,
    undocumented_filtered_vacuous,
)


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
