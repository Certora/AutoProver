"""What a Rust backend reports for a check its author declared broken.

An ``expect_check_failure`` check reports BAD either way: reproduced findings keep their
counterexample; unreproduced ones have no proof of concept. Stamps on ``Verdict.finding_key``
collapse those rows to one write-up. ``findings=None`` produces no findings.

The publish gate accepts an expected-to-fail check without requiring a repro, so this fold
is what keeps a documented finding off a green report row. Report and console both read
``reported_verdicts``, so they cannot disagree.
"""

import pathlib
from dataclasses import dataclass
from typing import Any, cast

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.pipeline.ptypes import ComponentOutcome
from composer.rustapp import adapter
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import summarize_verdicts
from composer.rustapp.wire import Verdict
from composer.spec.source.report.collect import ReportComponentInput, RuleEvidence, collect
from composer.spec.source.report.findings import FindingDraft, build_findings
from composer.templates.loader import load_jinja_template
from composer.spec.source.report.schema import (
    Finding, ImpactLevel, LikelihoodLevel, Outcome,
    ReproducedExpectedFailure, UnreproducedExpectedFailure,
)
from composer.spec.types import FindingKey, PropertyFormulation
from tests.conftest import wire_descriptor

#: Sample counterexample text a wheel might put on ``Verdict.detail``.
COUNTEREXAMPLE = (
    "crash crash_93d45d29a130d36f: [stored_price_timestamp_not_in_future] reserve 8J5W stored "
    "market_price_last_updated_ts=2 which is ahead of the current unix timestamp 0\n"
    "reproducing sequence (iteration 11, 3 action(s)):\n  1. refresh_reserves_batch -> OK"
)

#: Author's reason for a check this run did not hit.
REASON = "UpdateBlockPriceUsage only mark_stale()s, so PRICE_USAGE_ALLOWED survives the kill switch"


def _verdict(outcome: Outcome, detail: str | None = None, accounting: str | None = None,
             finding_key: FindingKey | None = None) -> Verdict:
    return Verdict(outcome=outcome, line=None, duration_seconds=None, unit_file=None,
                   detail=detail, accounting=accounting, finding_key=finding_key)


def _result(
    verdicts: dict[str, Verdict],
    expected_failures: dict[str, str],
    checks: list[tuple[str, list[str]]] | None = None,
) -> RustFormalResult:
    return RustFormalResult(
        checks=checks if checks is not None else [(f"{name}_prop", [name]) for name in verdicts],
        verdicts=verdicts,
        expected_failures=expected_failures,
    )


# ---------------------------------------------------------------------------
# The declaration fold
# ---------------------------------------------------------------------------

def test_a_declared_finding_the_run_reproduced_reports_bad_with_its_counterexample():
    result = _result(
        {"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON}
    )
    reported = result.reported_verdicts()

    assert reported["c_ts"].outcome is Outcome.BAD
    assert reported["c_ts"].detail == COUNTEREXAMPLE
    assert result.expected_failure("c_ts") == ReproducedExpectedFailure(reason=REASON)


def test_a_declared_finding_the_run_did_not_reproduce_still_reports_as_a_finding():
    # Wheel said GOOD; the declaration still makes it a finding.
    result = _result(
        {"c_kill": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    )
    reported = result.reported_verdicts()

    assert reported["c_kill"].outcome is Outcome.BAD, "a documented finding must not read as a pass"
    assert reported["c_kill"].detail is None
    assert result.expected_failure("c_kill") == UnreproducedExpectedFailure(reason=REASON, ran=Outcome.GOOD)


def test_an_undeclared_check_is_left_exactly_as_the_wheel_reported_it():
    # No declaration: leave the wheel's verdicts untouched.
    verdicts = {
        "c_good": _verdict(Outcome.GOOD),
        "c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE),
        "c_err": _verdict(Outcome.ERROR, "linker died"),
    }
    assert _result(verdicts, {}).reported_verdicts() == verdicts


def test_a_declared_check_whose_run_errored_keeps_the_error_text():
    # Keep the error text so the row still explains what happened.
    result = _result(
        {"c_kill": _verdict(Outcome.ERROR, "harness build timed out")}, {"c_kill": REASON}
    )
    reported = result.reported_verdicts()

    assert reported["c_kill"].outcome is Outcome.BAD
    assert reported["c_kill"].detail == "harness build timed out"
    assert result.expected_failure("c_kill") == UnreproducedExpectedFailure(
        reason=REASON, ran=Outcome.ERROR,
    )


def test_a_declaration_naming_a_check_that_did_not_run_adds_no_row():
    # A declaration for a check that never ran must not invent a row.
    reported = _result({"c_good": _verdict(Outcome.GOOD)}, {"c_gone": REASON}).reported_verdicts()

    assert list(reported) == ["c_good"]
    assert reported["c_good"].outcome is Outcome.GOOD


# ---------------------------------------------------------------------------
# Both consumers of the fold
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Formalized:
    """The ``Formalized`` protocol the report's collector reads."""

    result: RustFormalResult
    unit_file: str = "main.rs"
    run_link: str | None = None


@pytest.mark.asyncio
async def test_the_report_gets_the_declared_finding():
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    result = _result(
        {"c_kill": _verdict(Outcome.GOOD), "c_plain": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    )
    verdicts = await formalizer.fetch_verdicts(cast(Any, _Formalized(result)))

    assert verdicts["c_kill"].outcome is Outcome.BAD
    assert verdicts["c_kill"].message is None
    assert verdicts["c_kill"].expected_failure == UnreproducedExpectedFailure(
        reason=REASON, ran=Outcome.GOOD,
    )
    # Only the expected-to-fail check changes.
    assert verdicts["c_plain"].outcome is Outcome.GOOD
    assert verdicts["c_plain"].expected_failure is None


@dataclass
class _Feat:
    display_name: str
    slug: str = "oracle"


def test_the_console_rollup_agrees_with_the_report():
    # Report and console must agree on whether the run found something.
    result = _result(
        {"c_kill": _verdict(Outcome.GOOD), "c_plain": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    )
    summary = summarize_verdicts(
        CorePipelineResult(
            n_components=1, n_properties=0, failures=[],
            outcomes=[
                ComponentOutcome(
                    cast(Any, _Feat("Oracle-Driven Refresh")), [],
                    Delivered(result, pathlib.Path("main.rs")),
                )
            ],
        ),
        "prover",
    )

    assert [(v.name, v.outcome) for v in summary.verdicts] == [
        ("c_kill_prop", Outcome.BAD),
        ("c_plain_prop", Outcome.GOOD),
    ]


@pytest.mark.asyncio
async def test_a_wheels_own_file_beats_the_components_fallback():
    """A verdict that names its own file keeps it; otherwise use the component artifact."""
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    located = _verdict(Outcome.BAD)
    located.unit_file = "c_lamport_custody.rs"
    result = _result({"c_authority_immutable": located, "c_unplaced": _verdict(Outcome.GOOD)}, {})
    verdicts = await formalizer.fetch_verdicts(cast(Any, _Formalized(result)))
    assert verdicts["c_authority_immutable"].unit_file == "c_lamport_custody.rs"
    assert verdicts["c_unplaced"].unit_file == "main.rs"


# ---------------------------------------------------------------------------
# Report rows -> written findings
# ---------------------------------------------------------------------------

class _StubModel(BaseChatModel):
    """Structured output is preset, so the real path (prompt render, assess, compose) still runs."""
    output: Any

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        out = self.output
        return RunnableLambda(lambda _messages: out)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("stub is structured-output only")

    @property
    def _llm_type(self) -> str:
        return "stub"


def _draft(impact: ImpactLevel = "medium", likelihood: LikelihoodLevel = "low") -> FindingDraft:
    return FindingDraft(
        title="Initialization is accepted without the authority's signature",
        summary="s", description="d", impact="the authority guarantee is lost",
        impact_level=impact, likelihood_level=likelihood,
        risk_reasoning="the run reached it from a state the harness set up",
    )


def _outcome(result: RustFormalResult, unit_file: str = "harness.rs",
             component: str = "Vault Initialization") -> ComponentOutcome:
    return ComponentOutcome(
        cast(Any, _Feat(component)), [], Delivered(result, pathlib.Path(unit_file))
    )


async def _written(*outcomes: ComponentOutcome) -> list[Finding]:
    """The findings the report would carry, through the real collector and the real loop."""
    fz = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    properties, rules, *_ = await collect(
        [
            ReportComponentInput(
                name=cast(Any, o.feat.display_name),
                props=[
                    PropertyFormulation(title=t, sort="invariant", description="d")
                    for t, _ in cast(Delivered, o.result).result.checks
                ],
                formalized=cast(Any, _Formalized(cast(Delivered, o.result).result,
                                                 cast(Delivered, o.result).deliverable.name)),
            )
            for o in outcomes
        ],
        fetch_verdicts=fz.fetch_verdicts,
    )
    return await build_findings(
        contract_name="klend", rules=rules, properties=properties, groups=[],
        policy=fz.findings_policy(list(outcomes)), llm=_StubModel(output=_draft()),
    )


@pytest.mark.asyncio
async def test_a_reproduced_crash_is_the_proof_of_concept():
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    findings = await _written(_outcome(result))

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == COUNTEREXAMPLE
    # The model wrote the prose; the run's text is evidence, not the description.
    assert findings[0].content.description == "d"


@pytest.mark.asyncio
async def test_an_unreproduced_declared_finding_claims_no_counterexample():
    """An unreproduced declared finding has no proof of concept."""
    result = _result({"c_kill": _verdict(Outcome.GOOD, "campaign spent 41231 executions")},
                     {"c_kill": REASON})
    findings = await _written(_outcome(result))

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept is None, "no crash means no proof of concept"


@pytest.mark.asyncio
async def test_a_fuzz_finding_is_rated_like_any_other():
    """Severity comes from the model's axes through the matrix, not from the backend."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    f = (await _written(_outcome(result)))[0]

    prov = f.provenance
    assert prov is not None
    assert (prov.impact, prov.likelihood) == ("medium", "low")
    assert f.severity == "low", "the matrix maps medium x low, and nothing else assigns a tier"


@pytest.mark.asyncio
async def test_two_sections_naming_one_check_keep_their_own_crash():
    """Same check name in two files is two rows; each finding carries that file's reproducer."""
    left = _result({"c_auth": _verdict(Outcome.BAD, "crash A")}, {})
    right = _result({"c_auth": _verdict(Outcome.BAD, "crash B")}, {})
    for res, section in ((left, "c_vault_initialization.rs"), (right, "c_lamport_custody.rs")):
        res.verdicts["c_auth"].unit_file = section

    findings = await _written(_outcome(left, component="Vault Initialization"),
                              _outcome(right, component="Lamport Custody"))

    assert len(findings) == 2, "one row per section, so one finding per section"
    by_section = {f.provenance.spec_file: f for f in findings if f.provenance}
    assert by_section["c_vault_initialization.rs"].content.proof_of_concept == "crash A"
    assert by_section["c_lamport_custody.rs"].content.proof_of_concept == "crash B"


@pytest.mark.asyncio
async def test_a_finding_on_a_collapsed_row_reads_the_run_that_row_came_from():
    """Without a section file the two rows collapse; the finding must follow the surviving row."""
    first = _result({"c_auth": _verdict(Outcome.BAD, "crash A")}, {})
    second = _result({"c_auth": _verdict(Outcome.BAD, "crash B")}, {})

    findings = await _written(_outcome(first), _outcome(second, component="Lamport Custody"))

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == "crash A"


def test_the_prompt_offers_no_counterexample_for_a_finding_the_run_did_not_reproduce():
    """An unreproduced finding's prompt must not invite a counterexample."""
    user = load_jinja_template(
        "autoprove_report_findings_rust_prompt.j2",
        contract_name="klend", rule_name="c_kill", properties=[], groups=[], also_covers=[],
        evidence=[RuleEvidence(label="Oracle", ran=Outcome.GOOD,
                               accounting="campaign spent 41231 executions",
                               expected_failure_reason=REASON)],
    )

    assert "did NOT reproduce" in user and REASON in user
    assert "Do not describe a counterexample" in user


# ---------------------------------------------------------------------------
# Evidence about the program vs evidence about the run
# ---------------------------------------------------------------------------

#: Sample run accounting a wheel might put on every verdict, green ones included.
ACCOUNTING = (
    "[Vault Initialization] campaign spent 67798 executions in 597s of a 600s budget; reached "
    "6.1% of edges and 10.7% of branches, and got 44/92 of the harness's actions to succeed"
)


@pytest.mark.asyncio
async def test_a_proof_of_concept_is_the_crash_and_not_what_the_campaign_spent():
    """A proof of concept is the reproducer, not the run accounting."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE, ACCOUNTING)}, {})
    f = (await _written(_outcome(result)))[0]

    assert f.content.proof_of_concept == COUNTEREXAMPLE
    assert "campaign spent" not in (f.content.proof_of_concept or "")


@pytest.mark.asyncio
async def test_a_green_row_still_says_what_the_campaign_cost():
    """A green row still carries its accounting; a BAD row keeps evidence and accounting apart."""
    result = _result({"c_ok": _verdict(Outcome.GOOD, None, ACCOUNTING),
                      "c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE, ACCOUNTING)}, {})
    fz = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    rows = await fz.fetch_verdicts(cast(Any, _Formalized(result)))

    assert rows["c_ok"].message is None and rows["c_ok"].accounting == ACCOUNTING
    assert rows["c_ts"].message == COUNTEREXAMPLE
    assert rows["c_ts"].accounting == ACCOUNTING


def test_the_prompt_keeps_the_accounting_out_of_the_evidence():
    """The model is shown both, told which is which, and told what each is for."""
    user = load_jinja_template(
        "autoprove_report_findings_rust_prompt.j2",
        contract_name="klend", rule_name="c_ts", properties=[], groups=[], also_covers=[],
        evidence=[RuleEvidence(label="Vault Initialization", ran=Outcome.BAD,
                               counterexample=COUNTEREXAMPLE, accounting=ACCOUNTING)],
    )

    crash_at, spent_at = user.index(COUNTEREXAMPLE), user.index(ACCOUNTING)
    assert crash_at < spent_at, "the evidence leads"
    # The accounting is introduced as what it is, not appended to the crash.
    assert "with whatever reproduces it:" in user
    assert "spent and covered" in user


# ---------------------------------------------------------------------------
# One conclusion the wheel could not attribute to one check — which the WHEEL says, not this host
# ---------------------------------------------------------------------------

class _CountingModel(_StubModel):
    """Counts the write-ups actually asked for, and keeps both messages of each."""
    calls: list[str] = []
    systems: list[str] = []

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        out, calls, systems = self.output, self.calls, self.systems

        def _invoke(messages):
            systems.append(messages[0].content)
            calls.append(messages[-1].content)
            return out
        return RunnableLambda(_invoke)


UNPLACEABLE = (
    "crash crash_7f2a: [ledger_total_conserved] sum drifted by 3\n"
    "reproducing sequence (iteration 88, 1 action(s)):\n  1. sweep_fees -> OK"
)


async def _written_by(model: _CountingModel, *outcomes: ComponentOutcome,
                      descriptor: dict[str, Any] | None = None) -> list[Finding]:
    fz = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(descriptor or wire_descriptor())
    )
    properties, rules, *_ = await collect(
        [
            ReportComponentInput(
                name=cast(Any, o.feat.display_name),
                props=[
                    PropertyFormulation(title=t, sort="invariant", description="d")
                    for t, _ in cast(Delivered, o.result).result.checks
                ],
                formalized=cast(Any, _Formalized(cast(Delivered, o.result).result,
                                                 cast(Delivered, o.result).deliverable.name)),
            )
            for o in outcomes
        ],
        fetch_verdicts=fz.fetch_verdicts,
    )
    return await build_findings(
        contract_name="klend", rules=rules, properties=properties, groups=[],
        policy=fz.findings_policy(list(outcomes)), llm=model,
    )


def test_an_unattributed_write_up_lists_every_covered_check_and_none_as_the_subject():
    """The first row is one of the set, not the failing check the others hang off."""
    user = load_jinja_template(
        "autoprove_report_findings_rust_prompt.j2",
        contract_name="klend", rule_name="c_0", properties=[], groups=[],
        also_covers=["c_0", "c_1", "c_2"],
        evidence=[RuleEvidence(label="Vault", ran=Outcome.BAD, counterexample=UNPLACEABLE)],
    )
    assert "Failing check:" not in user
    for c in ("c_0", "c_1", "c_2"):
        assert f"- {c}" in user
    assert "do not pick one of them" in user


@pytest.mark.asyncio
async def test_a_crash_the_campaign_could_not_place_is_written_up_once():
    """A stamp on ``Verdict.finding_key`` writes several BAD rows up once.

    The key is the wheel's. Matching evidence is not enough to merge: several checks that
    failed the same way are a different fact.
    """
    checks = [f"c_{i}" for i in range(6)]
    result = _result(
        {c: _verdict(Outcome.BAD, UNPLACEABLE, ACCOUNTING, finding_key="c_vault") for c in checks}, {})
    model = _CountingModel(output=_draft(), calls=[], systems=[])

    findings = await _written_by(model, _outcome(result))

    assert len(model.calls) == 1, f"one crash, one write-up; asked for {len(model.calls)}"
    assert len(findings) == 1
    prompt = model.calls[0]
    assert "COULD NOT BE ATTRIBUTED" in prompt
    assert "Failing check:" not in prompt
    for c in checks:
        assert f"- {c}" in prompt, f"{c} is one of the rows this finding answers for"
    prov = findings[0].provenance
    assert prov is not None
    assert prov.covers == [("harness.rs", c) for c in checks]


@pytest.mark.asyncio
async def test_the_loop_binds_this_runs_evidence_into_the_write_up_prompt():
    """The host binds the write-up prompt, so the run's own evidence has to reach the model."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE, ACCOUNTING)}, {})
    model = _CountingModel(output=_draft(), calls=[], systems=[])

    await _written_by(model, _outcome(result))

    prompt = model.calls[0]
    assert COUNTEREXAMPLE in prompt, "the run's own evidence has to reach the model"
    assert "Failing check: c_ts" in prompt and "Vault Initialization" in prompt


@pytest.mark.asyncio
async def test_distinct_crashes_stay_distinct_findings():
    """Two crashes the wheel *could* place carry no key, so they stay two findings."""
    result = _result({"c_a": _verdict(Outcome.BAD, "crash A", ACCOUNTING),
                      "c_b": _verdict(Outcome.BAD, "crash B", ACCOUNTING)}, {})
    model = _CountingModel(output=_draft(), calls=[], systems=[])

    findings = await _written_by(model, _outcome(result))

    assert len(model.calls) == 2 and len(findings) == 2


@pytest.mark.asyncio
async def test_two_unreproduced_declarations_are_two_findings():
    """Unstamped rows stay separate, even when they look alike (no counterexample, same accounting)."""
    result = _result({"c_kill": _verdict(Outcome.GOOD, None, ACCOUNTING),
                      "c_stale": _verdict(Outcome.GOOD, None, ACCOUNTING)},
                     {"c_kill": REASON, "c_stale": "a different documented bug"})
    model = _CountingModel(output=_draft(), calls=[], systems=[])

    findings = await _written_by(model, _outcome(result))

    assert len(findings) == 2, "two declarations are two findings"
    # Each is written up against its own declaration — the reason is what makes them two claims.
    assert len(model.calls) == 2
    assert {REASON in c for c in model.calls} == {True, False}
    assert {"a different documented bug" in c for c in model.calls} == {True, False}


# ---------------------------------------------------------------------------
# What the wheel declares, and what a wheel that declares nothing gets
# ---------------------------------------------------------------------------

def test_a_wheel_that_declares_no_findings_policy_produces_none():
    """No FindingsDeclaration means no findings, not a host-invented write-up."""
    fz = adapter.RustFormalizer(
        cast(Any, object()),
        AppDescriptor.model_validate(wire_descriptor(findings=None)),
    )
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})

    assert fz.findings_policy([_outcome(result)]) is None


@pytest.mark.asyncio
async def test_the_write_up_is_told_what_this_wheels_evidence_is():
    """The wheel's domain leads the system prompt; the host's severity contract follows it."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    model = _CountingModel(output=_draft(), calls=[], systems=[])

    await _written_by(model, _outcome(result), descriptor=wire_descriptor(findings={
        "domain": "MARKER: this backend reads tea leaves.",
    }))

    system = model.systems[0]
    assert system.startswith("MARKER: this backend reads tea leaves.")
    assert system.index("MARKER") < system.index("HOW SEVERITY IS REACHED") < system.index(
        "WHAT TO PRODUCE"), "the wheel's claim leads; the host's contract follows it"
