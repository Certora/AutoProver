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
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from composer.prover.ptypes import RulePath, RuleResult
from composer.prover.results import SOLANA_TRACE, counterexample
from composer.spec.cvlr.pipeline import CvlrFormalizer
from composer.spec.cvlr.verify import _CaptureCallbacks, _unaccounted
from composer.spec.source.cex_capture import CexAnalysisStore

FIXTURES = Path(__file__).parent / "data" / "solana_cex"


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
    await _CaptureCallbacks(store).on_analysis_complete(rule, "the collector aliases the vault")

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
    await _CaptureCallbacks(store).on_analysis_complete(rule, "the loop needs a higher bound")

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
    await _Recording(store).on_analysis_complete(rule, "raise loop_iter to 3")
    assert handled == ["raise loop_iter to 3"]
    assert await store.for_rule("rule_deposit_credits_exactly_the_amount") == []


@pytest.mark.asyncio
async def test_a_fresh_run_supersedes_what_an_earlier_iteration_captured():
    """A rule that failed in iteration 1 and passes in iteration 2 must not survive into the report
    as a current failure. The clear fires on ``on_prover_result``, which the runner calls before the
    handler analyzes anything."""
    store = _store()
    callbacks = _CaptureCallbacks(store)
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
    await _CaptureCallbacks(store).on_analysis_complete(rule, "the collector aliases the vault")

    formalizer = CvlrFormalizer(object, "prover", SimpleNamespace(cex_analysis=store))
    fetch = formalizer.findings_evidence()
    assert fetch is not None

    evidence = await fetch("rule_withdraw_fee_collector_vault_aliasing")
    assert [e.analysis for e in evidence] == ["the collector aliases the vault"]
    assert await fetch("a_rule_that_never_failed") == []


# ---------------------------------------------------------------------------------------------
# What the author is told
#
# The filter above decides what the *report* claims. It also creates a way for the deliverable to
# contradict itself: an author whose gate is stuck on an unwinding violation can mark it with
# `expect_rule_failure` and publish a rule declared to expose a defect that contributes no evidence
# for one. These are what close that.


@pytest.mark.asyncio
async def test_the_gate_learns_which_rules_never_reached_their_property():
    """``ProverReport`` carries statuses and no counterexamples, so the classification has to be
    made where the results still have them — on the way past."""
    callbacks = _CaptureCallbacks(_store())
    results = {
        "loop": _violated("loop", _LOOP_BOUND),
        "real": _violated("real", _REAL_VIOLATION),
    }
    await callbacks.on_prover_result(results)

    assert set(callbacks.incomplete) == {"loop"}
    assert callbacks.incomplete["loop"].startswith("Unwinding condition in a loop")


@pytest.mark.asyncio
async def test_a_run_where_nothing_stalled_reports_nothing_stalled():
    callbacks = _CaptureCallbacks(_store())
    await callbacks.on_prover_result({"real": _violated("real", _REAL_VIOLATION)})
    assert callbacks.incomplete == {}


def test_an_incomplete_check_cannot_be_declared_an_expected_failure():
    """``expect_rule_failure`` says "this failure is a real defect". A rule that stopped on the
    prover's own assertion has shown no defect, so the marking must not excuse it from the gate —
    otherwise the run publishes a claim of a finding for which nothing was captured."""
    status = {"loop": False, "real": False}
    expected = {"loop": "I think this is a bug", "real": "the fee collector aliases the vault"}

    assert _unaccounted(status, expected) == []
    assert _unaccounted(status, expected, incomplete={"loop": "Unwinding condition in a loop"}) == [
        "loop"
    ]


def test_a_genuine_expected_failure_still_satisfies_the_gate():
    """The other direction, which is the whole reason the marking exists: a run can be complete with
    a real violation in it."""
    status = {"real": False}
    expected = {"real": "the fee collector aliases the vault"}
    assert _unaccounted(status, expected, incomplete={}) == []
