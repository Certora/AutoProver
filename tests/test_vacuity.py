"""
Unit tests for composer/prover/vacuity.py and the verify_spec vacuity gate:
vacuous-method detection over synthetic RuleResult sets, alert rendering, the
acknowledge_vacuous_method ledger tool, and the purely structural hard guard
that withholds the PROVER validation stamp while any known-vacuous method is
neither shown healthy by a later run nor acknowledged in the ledger.
"""

import json

import pytest

from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath, RuleResult, StatusCodes
from composer.prover.vacuity import (
    VacuityEvidence,
    detect_vacuous_methods,
    format_vacuity_alert,
    instantiated_methods,
)
from composer.spec.cvl_generation import check_completion
from composer.spec.source.author import AcknowledgeVacuousMethod, ExpectRuleFailure
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

    def test_two_rules_with_healthy_instantiation_not_flagged(self):
        """>=2 sanity failures alongside a VERIFIED instantiation are a
        rule-precondition problem, not method vacuity: a passing rule reached
        the method's code, disproving all-paths reversion."""
        results = [
            _res("ruleA", DEPOSIT, "SANITY_FAILED"),
            _res("ruleB", DEPOSIT, "SANITY_FAILED"),
            _res("ruleC", DEPOSIT, "VERIFIED"),
        ]
        assert detect_vacuous_methods(results) == {}

    def test_two_rules_with_violated_instantiation_not_flagged(self):
        """VIOLATED also requires reaching the method's code, so it clears
        the >=2 arm just like VERIFIED."""
        results = [
            _res("ruleA", DEPOSIT, "SANITY_FAILED"),
            _res("ruleB", DEPOSIT, "SANITY_FAILED"),
            _res("ruleC", DEPOSIT, "VIOLATED"),
        ]
        assert detect_vacuous_methods(results) == {}

    def test_two_rules_flagged_when_others_timeout(self):
        """TIMEOUT/ERROR can mask an all-paths-reverting method — the >=2 arm
        still fires when no instantiation reached a healthy verdict."""
        results = [
            _res("ruleA", DEPOSIT, "SANITY_FAILED"),
            _res("ruleB", DEPOSIT, "SANITY_FAILED"),
            _res("ruleC", DEPOSIT, "TIMEOUT"),
            _res("ruleD", DEPOSIT, "ERROR"),
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
# verify_spec vacuity hard guard (mocked prover)
# =========================================================================

# The gate is purely structural (persisted verdicts minus the acknowledgment
# ledger) — the spec text is irrelevant to it, so one spec serves every case:
# it "hides" the vacuous method simply by not exercising it in run 2.
_SPEC = """\
rule solvency(method f) filtered { f -> f.selector != sig:deposit(uint256).selector } {
    assert true;
}
"""

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


_ACK = "acknowledge_vacuous_method"


def _acknowledge(
    method: str = DEPOSIT,
    steps: list[str] | None = None,
    justification: str = "summary fix rejected by typechecker; mock failed to compile",
) -> ToolCallDict:
    return tool_call_raw(
        _ACK,
        method=method,
        steps_attempted=steps if steps is not None else ["summary_fix", "mock"],
        justification=justification,
    )


def _guard_scenario(certora_prover: ProverMock, *responses: ProverToolResponse):
    prover_tool = certora_prover(responses)
    return Scenario(
        StateWithSkips,
        prover_tool,
        ExpectRuleFailure.as_tool("expect_rule_failure"),
        AcknowledgeVacuousMethod.as_tool(_ACK),
        _result_tool,
    ).init(
        curr_spec=_SPEC,
        skipped=[],
        property_rules=[],
        validations={},
        required_validations=[VALIDATION_KEY],
        rule_skips={},
        vacuous_methods={},
        acknowledged_vacuous={},
        config={"files": ["src/Foo.sol"]},
    )


def _result_accepted(st: StateWithSkips) -> bool:
    return "result" in st


class TestAcknowledgeVacuousMethod:
    """The ledger tool's own validation, exercised against pre-seeded state."""

    def _scenario(self, vacuous: dict[str, str] | None = None):
        return Scenario(StateWithSkips, AcknowledgeVacuousMethod.as_tool(_ACK)).init(
            curr_spec=_SPEC,
            skipped=[],
            property_rules=[],
            validations={},
            required_validations=[VALIDATION_KEY],
            rule_skips={},
            vacuous_methods=vacuous if vacuous is not None else {DEPOSIT: "diagnosis"},
            acknowledged_vacuous={},
            config={},
        )

    @pytest.mark.asyncio
    async def test_acknowledgment_recorded_as_structured_ledger_entry(self):
        acks = await self._scenario().turn(_acknowledge()).map_run(
            lambda st: st["acknowledged_vacuous"]
        )
        record = json.loads(acks[DEPOSIT])
        assert record["steps_attempted"] == ["mock", "summary_fix"]
        assert "typechecker" in record["justification"]

    @pytest.mark.asyncio
    async def test_unknown_method_rejected(self):
        scenario = self._scenario().turn(_acknowledge(method=WITHDRAW))
        msg = await scenario.run_last_single_tool(_ACK)
        assert "not currently flagged" in msg
        assert DEPOSIT in msg  # the response lists what IS flagged

    @pytest.mark.asyncio
    async def test_empty_steps_rejected(self):
        acks = await self._scenario().turn(_acknowledge(steps=[])).map_run(
            lambda st: st["acknowledged_vacuous"]
        )
        assert acks == {}

    @pytest.mark.asyncio
    async def test_blank_justification_rejected(self):
        acks = await self._scenario().turn(_acknowledge(justification="  ")).map_run(
            lambda st: st["acknowledged_vacuous"]
        )
        assert acks == {}


class TestVerifySpecVacuityGuard:
    """Run 1 detects the vacuous method; run 2 passes because the spec hides it
    (filter / skip / rule removal — the gate cannot tell and must not care).
    The stamp must be withheld until the method is acknowledged or shown healthy."""

    @pytest.mark.asyncio
    async def test_unacknowledged_hidden_method_withholds_stamp(self, certora_prover: ProverMock):
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
        )
        accepted = await scenario.turns(
            _verify(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert not accepted

    @pytest.mark.asyncio
    async def test_guard_message_names_method_ladder_and_tool(self, certora_prover: ProverMock):
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
        )
        msg = await scenario.turn(_verify()).turn(_verify()).run_last_single_tool(_PROVER)
        assert "vacuity_guard" in msg
        assert "WITHHELD" in msg
        assert DEPOSIT in msg
        assert "acknowledge_vacuous_method" in msg
        assert "write_mock" in msg  # the repair ladder is restated

    @pytest.mark.asyncio
    async def test_acknowledged_method_grants_stamp(self, certora_prover: ProverMock):
        """The structured escape hatch: a ledger entry written by the tool."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
        )
        accepted = await scenario.turns(
            _verify(),
            _acknowledge(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted

    @pytest.mark.asyncio
    async def test_rule_skip_bypass_still_blocked(self, certora_prover: ProverMock):
        """The expect_rule_failure bypass: the sanity-failing rule drops out of
        the all-verified check, but the persisted verdict still withholds the
        stamp — no matter how persuasive the free-text skip reason sounds
        (the old lexical hatch is gone; only the ledger clears the gate)."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
        )
        accepted = await scenario.turns(
            _verify(),
            tool_call_raw(
                "expect_rule_failure", rule_name="solvency",
                reason="attempted summary fix, mock, and optimistic_fallback; all failed",
            ),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert not accepted

    @pytest.mark.asyncio
    async def test_acknowledged_rule_skip_grants_stamp(self, certora_prover: ProverMock):
        """Skipping the sanity-failing rules is fine once the method itself is
        acknowledged in the ledger."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
        )
        accepted = await scenario.turns(
            _verify(),
            tool_call_raw("expect_rule_failure", rule_name="solvency", reason="vacuous, acknowledged"),
            _acknowledge(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted

    @pytest.mark.asyncio
    async def test_healthy_reinstantiation_clears_verdict(self, certora_prover: ProverMock):
        """Run 2 instantiates the method without a sanity failure (the setup was
        repaired) — the vacuity verdict clears and nothing blocks the stamp."""
        scenario = _guard_scenario(
            certora_prover,
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}, instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
        )
        accepted = await scenario.turns(
            _verify(),
            _verify(),
            _verify(),
            _result("done"),
        ).map_run(_result_accepted)
        assert accepted

    @pytest.mark.asyncio
    async def test_healthy_reinstantiation_clears_acknowledgment(self, certora_prover: ProverMock):
        """When the verdict clears, its ledger entry clears with it: a method
        that turns vacuous AGAIN later needs a fresh acknowledgment, so a stale
        acknowledgment can never pre-clear a future verdict."""
        scenario = _guard_scenario(
            certora_prover,
            # Run 1: vacuous. Acknowledged. Run 2: healthy (verdict + ack clear).
            # Run 3: vacuous again. Run 4: hidden again -> guard must re-fire.
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}, instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": False}, vacuous=_DEPOSIT_EVIDENCE,
                            instantiated={DEPOSIT}),
            _vacuous_report(rule_status={"solvency": True}),
        )
        final = await scenario.turns(
            _verify(),
            _acknowledge(),
            _verify(),
            _verify(),
            _verify(),
        ).map_run(lambda st: (st["acknowledged_vacuous"], Scenario.last_single_tool(_PROVER, st)))
        acks, last_prover_msg = final
        assert acks == {}  # cleared by the healthy run, not re-established
        assert "vacuity_guard" in last_prover_msg
        assert "WITHHELD" in last_prover_msg
