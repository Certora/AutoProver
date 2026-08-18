"""Tests for a Rust backend's findings.

A check marked ``expect_check_failure`` must report as a finding even if this run
did not reproduce it. Those rows become ``Finding``s with no second write-up —
the crash and the declaration are the write-up. Reproduced and unreproduced
findings must stay distinguishable.

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

import pytest

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.pipeline.ptypes import ComponentOutcome
from composer.rustapp import adapter
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.findings import compose_findings
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import summarize_verdicts
from composer.rustapp.wire import Verdict
from composer.spec.source.report.collect import ReportComponentInput, collect
from composer.spec.source.report.schema import Outcome, RuleVerdict
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


def _verdict(outcome: Outcome, detail: str | None = None) -> Verdict:
    return Verdict(outcome=outcome, line=None, duration_seconds=None, unit_file=None, detail=detail)


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
# Report rows -> findings
# ---------------------------------------------------------------------------

def _outcome(result: RustFormalResult, unit_file: str = "main.rs") -> ComponentOutcome:
    return ComponentOutcome(
        cast(Any, _Feat("Oracle-Driven Refresh")), [], Delivered(result, pathlib.Path(unit_file))
    )


def _rows(result: RustFormalResult, unit_file: str = "main.rs") -> list[RuleVerdict]:
    """The report rows ``collect`` would build from one component's reported verdicts."""
    return [
        RuleVerdict(name=name, spec_file=v.unit_file or unit_file, outcome=v.outcome,
                    message=v.detail)
        for name, v in result.reported_verdicts().items()
    ]


def test_a_reproduced_declared_finding_carries_its_counterexample_and_its_reason():
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON},
                     checks=[("Stored prices are never in the future", ["c_ts"])])
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])

    assert len(findings) == 1
    f = findings[0]
    assert f.title == "Stored prices are never in the future"
    assert f.content.proof_of_concept == COUNTEREXAMPLE
    assert REASON in f.content.description
    assert f.provenance is not None and f.provenance.risk_reasoning == REASON
    assert f.provenance.rule_name == "c_ts" and f.provenance.outcome is Outcome.BAD


def test_an_unreproduced_declared_finding_claims_no_counterexample():
    result = _result({"c_kill": _verdict(Outcome.GOOD)}, {"c_kill": REASON},
                     checks=[("The kill switch stops price usage", ["c_kill"])])
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])

    assert len(findings) == 1
    f = findings[0]
    assert "NOT REPRODUCED" in f.content.description
    assert f.content.proof_of_concept is None
    assert f.provenance is not None and f.provenance.risk_reasoning == REASON


def test_a_crash_the_run_found_needs_no_declaration():
    result = _result({"c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == COUNTEREXAMPLE
    assert findings[0].provenance is not None
    assert findings[0].provenance.risk_reasoning is None


def test_findings_assess_no_risk():
    """Severity and impact stay blank: nothing here has judged what a crash is worth."""
    result = _result({"c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {})
    f = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])[0]
    assert f.severity == "informational"
    assert f.content.impact == ""


def test_a_check_verifying_several_properties_is_named_after_itself():
    result = _result({"c_multi": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {},
                     checks=[("Prices are fresh", ["c_multi"]), ("Prices are signed", ["c_multi"])])
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])
    # Several properties: the check name is the only unambiguous title.
    assert findings[0].title == "c_multi"


def test_only_violations_become_findings():
    """ERROR and TIMEOUT stay verdict rows, not findings."""
    result = _result(
        {
            "c_good": _verdict(Outcome.GOOD),
            "c_err": _verdict(Outcome.ERROR, "linker died"),
            "c_slow": _verdict(Outcome.TIMEOUT, "gave up after 30m"),
            "c_bad": _verdict(Outcome.BAD, COUNTEREXAMPLE),
        },
        {},
    )
    findings = compose_findings(rules=_rows(result), outcomes=[_outcome(result)])
    assert [f.provenance.rule_name for f in findings if f.provenance] == ["c_bad"]


def test_a_clean_campaign_produces_no_findings():
    """An empty list is what makes the report omit the Findings section."""
    result = _result({"c_good": _verdict(Outcome.GOOD)}, {})
    assert compose_findings(rules=_rows(result), outcomes=[_outcome(result)]) == []


def test_two_components_sharing_one_artifact_keep_their_own_evidence():
    """Two components sharing one artifact file must still keep their own evidence."""
    left = _result({"c_a": _verdict(Outcome.BAD, "crash A")}, {})
    right = _result({"c_b": _verdict(Outcome.GOOD)}, {"c_b": REASON})
    findings = compose_findings(
        rules=_rows(left) + _rows(right), outcomes=[_outcome(left), _outcome(right)],
    )
    by_rule = {f.provenance.rule_name: f for f in findings if f.provenance}
    assert by_rule["c_a"].content.proof_of_concept == "crash A"
    assert by_rule["c_a"].provenance is not None and by_rule["c_a"].provenance.risk_reasoning is None
    assert by_rule["c_b"].content.proof_of_concept is None
    assert by_rule["c_b"].provenance is not None
    assert by_rule["c_b"].provenance.risk_reasoning == REASON


@pytest.mark.asyncio
async def test_the_formalizer_submits_them_through_the_report_hook():
    """The formalizer returns ready findings through the report hook."""
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    result = _result({"c_ts": _verdict(Outcome.BAD, COUNTEREXAMPLE)}, {"c_ts": REASON},
                     checks=[("Stored prices are never in the future", ["c_ts"])])
    findings = await formalizer.findings(
        contract_name="klend", rules=_rows(result), properties=[], groups=[],
        outcomes=[_outcome(result)], run=cast(Any, object()),
    )

    assert [f.title for f in findings] == ["Stored prices are never in the future"]
    assert findings[0].content.proof_of_concept == COUNTEREXAMPLE


# ---------------------------------------------------------------------------
# Crucible's one-crate deliverable
# ---------------------------------------------------------------------------

async def _collected(*components: tuple[str, RustFormalResult]) -> list[RuleVerdict]:
    """The rows the real collector builds — the same list ``build_report`` hands ``findings``."""
    formalizer = adapter.RustFormalizer(
        cast(Any, object()), AppDescriptor.model_validate(wire_descriptor())
    )
    _, rules, *_ = await collect(
        [
            ReportComponentInput(
                name=cast(Any, name),
                props=[
                    PropertyFormulation(title=title, sort="invariant", description="d")
                    for title, _ in res.checks
                ],
                formalized=cast(Any, _Formalized(res)),
            )
            for name, res in components
        ],
        fetch_verdicts=formalizer.fetch_verdicts,
    )
    return rules


@pytest.mark.asyncio
async def test_two_crucible_sections_naming_one_check_keep_their_own_crash():
    """Crucible delivers one crate, so `validate` files each verdict under its section's file.

    Two authors given the same property title write the same check name, and the report
    keys a row by ``(file, name)`` — the section file is the only thing that keeps the two
    rows apart. The findings mapper keys observations the same way, so a section's finding
    must carry that section's crash and not the other's.
    """
    left = _result({"c_auth_immutable": _verdict(Outcome.BAD, "crash A")}, {},
                   checks=[("Authority is immutable", ["c_auth_immutable"])])
    right = _result({"c_auth_immutable": _verdict(Outcome.BAD, "crash B")}, {},
                    checks=[("Authority is immutable", ["c_auth_immutable"])])
    for res, section in ((left, "c_vault_initialization.rs"), (right, "c_lamport_custody.rs")):
        res.verdicts["c_auth_immutable"].unit_file = section

    rules = await _collected(("Vault Initialization", left), ("Lamport Custody", right))
    findings = compose_findings(
        rules=rules, outcomes=[_outcome(left), _outcome(right)],
    )

    assert len(findings) == 2, "one row per section, so one finding per section"
    by_section = {f.provenance.spec_file: f for f in findings if f.provenance}
    assert by_section["c_vault_initialization.rs"].content.proof_of_concept == "crash A"
    assert by_section["c_lamport_custody.rs"].content.proof_of_concept == "crash B"


@pytest.mark.asyncio
async def test_a_finding_on_a_collapsed_row_reads_the_run_that_row_came_from():
    """Without a section file the two rows do collapse — and the finding must follow the row.

    ``collect`` keeps the first run naming a ``(file, name)``; the mapper has to keep the same
    one, or the row's message and the finding's proof of concept describe different runs.
    """
    first = _result({"c_auth_immutable": _verdict(Outcome.BAD, "crash A")}, {},
                    checks=[("Authority is immutable", ["c_auth_immutable"])])
    second = _result({"c_auth_immutable": _verdict(Outcome.BAD, "crash B")}, {},
                     checks=[("Authority is immutable", ["c_auth_immutable"])])

    rules = await _collected(("Vault Initialization", first), ("Lamport Custody", second))
    findings = compose_findings(
        rules=rules, outcomes=[_outcome(first), _outcome(second)],
    )

    assert len(findings) == 1
    assert findings[0].content.proof_of_concept == "crash A"
    assert "crash A" in (rules[0].message or "")
