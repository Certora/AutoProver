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

import pytest

from composer.pipeline.core import CorePipelineResult, Delivered
from composer.pipeline.ptypes import ComponentOutcome
from composer.rustapp import adapter
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.result import RustFormalResult
from composer.rustapp.results import summarize_verdicts
from composer.rustapp.wire import Verdict
from composer.spec.source.report.schema import Outcome
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
