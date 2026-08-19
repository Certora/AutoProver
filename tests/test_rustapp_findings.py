"""The declaration fold: what a Rust backend reports for a check its author declared broken.

A check marked ``expect_check_failure`` must report as a finding even if this run
did not reproduce it. Reproduced and unreproduced findings must stay distinguishable.

``expect_check_failure`` is how an author says "the failure here IS the finding". The
publish gate accepts such a check as clean and nothing downstream ever asked the run to
reproduce it, so on the klend run of 2026-08-10 four declared findings reached
``report.html``: one as a violation (its campaign happened to hit it first) and three as
**"No counterexample"** — including ``block_price_usage_kill_switch_effective``, which the
author had reproduced at iteration 11619 and whose commentary documents the reproducing
sequence.

The declaration is the author's and the outcome is the wheel's; they meet on
``RustFormalResult``, and the first group below pins that meeting down for both consumers
of it — the HTML report and the console rollup, which must not disagree about whether a
run found something.
"""

import pathlib
from dataclasses import dataclass
from typing import Any, cast
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
from composer.spec.source.report.collect import ReportComponentInput, collect
from composer.spec.source.report.findings import FindingDraft, RuleEvidence, build_findings
from composer.templates.loader import load_jinja_template
from composer.spec.source.report.schema import Finding, Outcome
from composer.spec.types import PropertyFormulation
from tests.conftest import wire_descriptor

#: Crash text from a real Crucible hit, trimmed.
COUNTEREXAMPLE = (
    "crash crash_93d45d29a130d36f: [stored_price_timestamp_not_in_future] reserve 8J5W stored "
    "market_price_last_updated_ts=2 which is ahead of the current unix timestamp 0\n"
    "reproducing sequence (iteration 11, 3 action(s)):\n  1. refresh_reserves_batch -> OK"
)

#: Author's reason for a check this run did not hit.
REASON = "UpdateBlockPriceUsage only mark_stale()s, so PRICE_USAGE_ALLOWED survives the kill switch"


def _verdict(outcome: Outcome, detail: str | None = None, accounting: str | None = None,
             finding: str | None = None) -> Verdict:
    return Verdict(outcome=outcome, line=None, duration_seconds=None, unit_file=None,
                   detail=detail, accounting=accounting, finding=finding)


def _result(
    verdicts: dict[str, Verdict],
    declared: dict[str, str],
    checks: list[tuple[str, list[str]]] | None = None,
) -> RustFormalResult:
    return RustFormalResult(
        checks=checks if checks is not None else [(f"{name}_prop", [name]) for name in verdicts],
        verdicts=verdicts,
        expected_failures=declared,
    )


# ---------------------------------------------------------------------------
# The declaration fold
# ---------------------------------------------------------------------------

def test_a_declared_finding_the_run_reproduced_reports_bad_with_its_counterexample():
    reported = _result(
        {"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON}
    ).reported_verdicts()

    assert reported["c_ts"].outcome is Outcome.BAD
    detail = reported["c_ts"].detail or ""
    # Declaration and counterexample both have to survive the fold.
    assert REASON in detail
    assert COUNTEREXAMPLE in detail


def test_a_declared_finding_the_run_did_not_reproduce_still_reports_as_a_finding():
    # Wheel said GOOD; the declaration still makes it a finding.
    reported = _result(
        {"c_kill": _verdict(Outcome.GOOD)}, {"c_kill": REASON}
    ).reported_verdicts()

    assert reported["c_kill"].outcome is Outcome.BAD, "a documented finding must not read as a pass"
    detail = reported["c_kill"].detail or ""
    assert REASON in detail
    # Weaker claim: say so, and name the outcome the run actually reached.
    assert "NOT REPRODUCED" in detail
    assert "GOOD" in detail, "the run's own outcome is what makes the claim weaker; name it"


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
    reported = _result(
        {"c_kill": _verdict(Outcome.ERROR, "harness build timed out")}, {"c_kill": REASON}
    ).reported_verdicts()

    assert reported["c_kill"].outcome is Outcome.BAD
    detail = reported["c_kill"].detail or ""
    assert "NOT REPRODUCED" in detail and "ERROR" in detail
    assert "harness build timed out" in detail


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
    assert REASON in (verdicts["c_kill"].message or "")
    # Only the declared check changes.
    assert verdicts["c_plain"].outcome is Outcome.GOOD


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
    """A verdict that names its own file keeps it; otherwise use the component artifact.

    The report keys a row by ``(file, name)``. A callout-mode wheel delivers one
    artifact, so every component would share that fallback — two same-named checks
    would collapse. The wheel's ``unit_file`` is what keeps them apart.
    """
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


def _draft() -> FindingDraft:
    return FindingDraft(
        title="Initialization is accepted without the authority's signature",
        summary="s", description="d", impact="the authority guarantee is lost",
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
    _, rules, *_ = await collect(
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
        contract_name="klend", rules=rules, properties=[], groups=[],
        synthesis=fz.findings_synthesis(list(outcomes)), llm=_StubModel(output=_draft()),
    )


@pytest.mark.asyncio
async def test_a_reproduced_crash_is_the_proof_of_concept():
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    findings = await _written(_outcome(result))

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == COUNTEREXAMPLE
    # The model wrote the prose; the campaign text is evidence, not the description.
    assert findings[0].content.description == "d"


@pytest.mark.asyncio
async def test_an_unreproduced_declared_finding_claims_no_counterexample():
    """Its only ground is the author's reading, and the finding has to say so rather than show one."""
    result = _result({"c_kill": _verdict(Outcome.GOOD, "campaign spent 41231 executions")},
                     {"c_kill": REASON})
    findings = await _written(_outcome(result))

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept is None, "no crash means no proof of concept"
    prov = findings[0].provenance
    assert prov is not None and prov.risk_reasoning == REASON


@pytest.mark.asyncio
async def test_a_fuzz_finding_carries_no_risk_rating():
    """Severity is fixed and the axes stay empty: nothing here has assessed exploitability."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    f = (await _written(_outcome(result)))[0]

    assert f.severity == "informational"
    prov = f.provenance
    assert prov is not None and prov.impact is None and prov.likelihood is None


@pytest.mark.asyncio
async def test_two_sections_naming_one_check_keep_their_own_crash():
    """Crucible delivers one crate, so `validate` files each verdict under its section's file.

    Two authors given the same property title write the same check name, and the report keys a row
    by ``(file, name)`` — the section file is the only thing keeping the two rows apart. The
    evidence is keyed the same way, so a section's finding must carry that section's crash.
    """
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
    """Without a section file the two rows do collapse — the finding must follow the row.

    ``collect`` keeps the first run naming a ``(file, name)``; the evidence has to keep the same
    one, or the row's message and its finding's proof of concept describe different runs.
    """
    first = _result({"c_auth": _verdict(Outcome.BAD, "crash A")}, {})
    second = _result({"c_auth": _verdict(Outcome.BAD, "crash B")}, {})

    findings = await _written(_outcome(first), _outcome(second, component="Lamport Custody"))

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == "crash A"


def test_the_prompt_offers_no_counterexample_for_a_finding_the_run_did_not_reproduce():
    """The prompt is where a model could be led to invent one, so the distinction lives there too."""
    user = load_jinja_template(
        "autoprove_report_findings_rust_prompt.j2",
        contract_name="klend", rule_name="c_kill", properties=[], groups=[], also_covers=[],
        evidence=[RuleEvidence(label="Oracle", ran=Outcome.GOOD,
                               accounting="campaign spent 41231 executions", declared=REASON)],
    )

    assert "did NOT reproduce" in user and REASON in user
    assert "Do not describe a counterexample" in user


# ---------------------------------------------------------------------------
# Evidence about the program vs evidence about the run
# ---------------------------------------------------------------------------

#: What `campaign.rs` puts on every verdict, green ones included.
ACCOUNTING = (
    "[Vault Initialization] campaign spent 67798 executions in 597s of a 600s budget; reached "
    "6.1% of edges and 10.7% of branches, and got 44/92 of the harness's actions to succeed"
)


@pytest.mark.asyncio
async def test_a_proof_of_concept_is_the_crash_and_not_what_the_campaign_spent():
    """Run accounting inside a proof of concept leaves a reader unable to see where evidence ends."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE, ACCOUNTING)}, {})
    f = (await _written(_outcome(result)))[0]

    assert f.content.proof_of_concept == COUNTEREXAMPLE
    assert "campaign spent" not in (f.content.proof_of_concept or "")


@pytest.mark.asyncio
async def test_a_green_row_still_says_what_the_campaign_cost():
    """The split must not cost the report what `campaign.rs` exists to put on a passing row.

    The report has one ``message`` per row, so `fetch_verdicts` rejoins the halves — evidence
    first, because a BAD row's first line is what a reader is looking for.
    """
    result = _result({"c_ok": _verdict(Outcome.GOOD, None, ACCOUNTING),
                      "c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE, ACCOUNTING)}, {})
    fz = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    rows = await fz.fetch_verdicts(cast(Any, _Formalized(result)))

    assert rows["c_ok"].message == ACCOUNTING, "a green row's whole worth is its accounting"
    bad = rows["c_ts"].message or ""
    assert bad.startswith(COUNTEREXAMPLE) and bad.endswith(ACCOUNTING)


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
# One crash the campaign could not place — which the WHEEL says, not this host
# ---------------------------------------------------------------------------

class _CountingModel(_StubModel):
    """Counts the write-ups actually asked for, and keeps the prompt of each."""
    calls: list[str] = []

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        out, calls = self.output, self.calls

        def _invoke(messages):
            calls.append(messages[-1].content)
            return out
        return RunnableLambda(_invoke)


UNPLACEABLE = (
    "crash crash_7f2a: [ledger_total_conserved] sum drifted by 3\n"
    "reproducing sequence (iteration 88, 1 action(s)):\n  1. sweep_fees -> OK"
)


async def _written_by(model: _CountingModel, *outcomes: ComponentOutcome) -> list[Finding]:
    fz = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    _, rules, *_ = await collect(
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
        contract_name="klend", rules=rules, properties=[], groups=[],
        synthesis=fz.findings_synthesis(list(outcomes)), llm=model,
    )


@pytest.mark.asyncio
async def test_a_crash_the_campaign_could_not_place_is_written_up_once():
    """`attribute_findings` condemns every check a campaign covered when it cannot place a crash,
    and stamps the fan-out as one finding (`Verdict.finding`).

    Condemning them all is right for the verdict table — the counterexample is real and hiding it
    would be worse — but it is one finding, not one per row. Writing it up per row spends a heavy
    model on each and publishes N accounts of the same crash, each guessing a different check it
    might have been.

    The key is the wheel's, and nothing here infers it: rows fanned out from one conclusion are
    otherwise indistinguishable from several checks that failed identically, which is a different
    fact about the program.
    """
    checks = [f"c_{i}" for i in range(6)]
    result = _result(
        {c: _verdict(Outcome.BAD, UNPLACEABLE, ACCOUNTING, finding="c_vault") for c in checks}, {})
    model = _CountingModel(output=_draft(), calls=[])

    findings = await _written_by(model, _outcome(result))

    assert len(model.calls) == 1, f"one crash, one write-up; asked for {len(model.calls)}"
    assert len(findings) == 1
    # The write-up is told it covers the others, so it cannot pick one and pin the crash on it.
    prompt = model.calls[0]
    assert "COULD NOT BE ATTRIBUTED" in prompt
    for c in checks[1:]:
        assert c in prompt, f"{c} is one of the rows this finding answers for"


@pytest.mark.asyncio
async def test_the_loop_binds_this_runs_evidence_into_the_write_up_prompt():
    """The host binds the prompt now, not the backend, so nothing backend-side would catch it
    binding the wrong thing — and a write-up rendered against absent evidence still produces a
    plausible finding rather than an error."""
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE, ACCOUNTING)}, {})
    model = _CountingModel(output=_draft(), calls=[])

    await _written_by(model, _outcome(result))

    prompt = model.calls[0]
    assert COUNTEREXAMPLE in prompt, "the run's own evidence has to reach the model"
    assert "c_ts" in prompt and "Vault Initialization" in prompt


@pytest.mark.asyncio
async def test_distinct_crashes_stay_distinct_findings():
    """Two crashes the wheel *could* place carry no key, so they stay two findings."""
    result = _result({"c_a": _verdict(Outcome.BAD, "crash A", ACCOUNTING),
                      "c_b": _verdict(Outcome.BAD, "crash B", ACCOUNTING)}, {})
    model = _CountingModel(output=_draft(), calls=[])

    findings = await _written_by(model, _outcome(result))

    assert len(model.calls) == 2 and len(findings) == 2


@pytest.mark.asyncio
async def test_two_unreproduced_declarations_are_two_findings():
    """An unstamped row stands on its own, so each declaration is its own claim.

    Worth pinning because these two rows are otherwise as alike as rows get: no counterexample, and
    the same accounting, since it is the same campaign. Nothing about that similarity may merge two
    findings the author made separately.
    """
    result = _result({"c_kill": _verdict(Outcome.GOOD, None, ACCOUNTING),
                      "c_stale": _verdict(Outcome.GOOD, None, ACCOUNTING)},
                     {"c_kill": REASON, "c_stale": "a different documented bug"})
    model = _CountingModel(output=_draft(), calls=[])

    findings = await _written_by(model, _outcome(result))

    assert len(findings) == 2, "two declarations are two findings"
    reasons = {f.provenance.risk_reasoning for f in findings if f.provenance}
    assert reasons == {REASON, "a different documented bug"}


# ---------------------------------------------------------------------------
# What the wheel declares, and what a wheel that declares nothing gets
# ---------------------------------------------------------------------------

def test_a_wheel_that_declares_no_findings_policy_produces_none():
    """No policy, no findings — not findings written from a default the host made up.

    A write-up asserts what the evidence behind it is, and the host cannot know: it has verdicts,
    not a claim about what produced them. A wheel that reports outcomes without saying what they
    establish is coherent, and its report carries the verdict rows and nothing else."""
    fz = adapter.RustFormalizer(
        cast(Any, object()),
        AppDescriptor.model_validate(wire_descriptor(findings=None)),
    )
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})

    assert fz.findings_synthesis([_outcome(result)]) is None


def test_the_write_up_is_told_what_this_wheels_evidence_is():
    """The wheel's own prose leads the system prompt, and the host's output contract follows it.

    The two halves are separable for the same reason `judge` splits them: what the evidence *is* is
    the backend's claim, and which sections come back is the host's, so neither restates the other.
    """
    fz = adapter.RustFormalizer(
        cast(Any, object()),
        AppDescriptor.model_validate(wire_descriptor(findings={
            "system": "MARKER: this backend reads tea leaves.",
            "severity": {"policy": "fixed", "tier": "low"},
        })),
    )
    synthesis = fz.findings_synthesis([])
    assert synthesis is not None

    assert synthesis.system.startswith("MARKER: this backend reads tea leaves.")
    # A fixed tier is named, and the model is told not to rate — asking for a rating this evidence
    # cannot support is how a fabricated one gets into a finding a reader trusts.
    assert "fixed at low" in synthesis.system
    assert "Do not assign or imply a severity" in synthesis.system
    # …and the schema it answers in carries no axes to fill in either.
    assert "impact_level" not in synthesis.severity.draft.model_fields


def test_a_wheel_that_assesses_risk_is_asked_for_the_axes():
    """The other half of the policy: a backend whose evidence *can* carry a risk judgement gets the
    assessed draft and the matrix, not a constant."""
    fz = adapter.RustFormalizer(
        cast(Any, object()),
        AppDescriptor.model_validate(wire_descriptor(findings={
            "system": "This backend proves things.", "severity": {"policy": "assessed"},
        })),
    )
    synthesis = fz.findings_synthesis([])
    assert synthesis is not None

    assert "impact_level" in synthesis.severity.draft.model_fields
    assert "Rate impact and likelihood" in synthesis.system
    assert "Do not assign" not in synthesis.system
