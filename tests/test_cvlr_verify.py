"""A draft stamps when every rule it declares verified.

No cargo and no Prover. The report is a stand-in, and the stamp is the digest
of the draft.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.authoring.state import make_validation_stamper, spec_digest
from composer.cargo.session import CompileFailed
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath
from composer.prover.conf import SelectRules
from composer.spec.cvlr.prover import BuildRejected, Checked, Submission, SubmissionFailed
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY
from composer.spec.cvlr.verify import VerifyRules, _submission_for

_DRAFT = "#[rule]\nfn rule_a() {}\n#[rule]\nfn rule_b() {}\n"
_LINK = "https://prover.certora.com/jobStatus/1/abc"


def _state(draft: str | None = _DRAFT) -> dict:
    return {
        "messages": [],
        "curr_spec": draft,
        "skipped": [],
        "validations": {},
        "required_validations": [PROVER_VALIDATION_KEY],
    }


def _tool(draft: str | None = _DRAFT) -> VerifyRules:
    return VerifyRules(state=_state(draft), tool_call_id="tc")  # type: ignore[arg-type]


def _deps():
    return SimpleNamespace(stamper=make_validation_stamper(PROVER_VALIDATION_KEY))


def _report(*pairs: tuple[str, str]) -> ProverReport:
    return ProverReport(
        raw_rule_status={RulePath(name): status for name, status in pairs},  # type: ignore[misc]
        result_str="rules",
        link=_LINK,
        certora_run_stdout="",
    )


def test_every_declared_rule_verified_stamps_the_draft():
    out = _tool()._report(
        Checked(build=None, report=_report(("rule_a", "VERIFIED"), ("rule_b", "VERIFIED"))),  # type: ignore[arg-type]
        _deps(),  # type: ignore[arg-type]
        declared=("rule_a", "rule_b"),
    )

    assert out.update["validations"] == {PROVER_VALIDATION_KEY: spec_digest(_DRAFT, [])}
    assert out.update["prover_link"] == _LINK
    assert "stamped" in out.update["messages"][0].content


def test_a_failing_rule_does_not_stamp():
    out = _tool()._report(
        Checked(build=None, report=_report(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"))),  # type: ignore[arg-type]
        _deps(),  # type: ignore[arg-type]
        declared=("rule_a", "rule_b"),
    )

    assert "validations" not in out.update
    assert out.update["prover_link"] == _LINK
    assert "rule_b" in out.update["messages"][0].content


def test_a_missing_verdict_does_not_stamp():
    out = _tool()._report(
        Checked(build=None, report=_report(("rule_a", "VERIFIED"))),  # type: ignore[arg-type]
        _deps(),  # type: ignore[arg-type]
        declared=("rule_a", "rule_b"),
    )

    assert "validations" not in out.update
    assert "rule_b" in out.update["messages"][0].content


def test_a_rejected_build_reports_the_compiler_and_does_not_stamp():
    rejected = BuildRejected(
        build=SimpleNamespace(verdict=CompileFailed(diagnostics="error[E0425]: cannot find", exit_code=1))  # type: ignore[arg-type]
    )

    out = _tool()._report(rejected, _deps(), declared=("rule_a",))  # type: ignore[arg-type]

    assert "E0425" in out.update["messages"][0].content
    assert "validations" not in out.update


def test_a_submission_without_results_does_not_stamp():
    out = _tool()._report(
        SubmissionFailed(build=None, reason="the cloud job failed"),  # type: ignore[arg-type]
        _deps(),  # type: ignore[arg-type]
        declared=("rule_a",),
    )

    assert "cloud job failed" in out.update["messages"][0].content
    assert "validations" not in out.update


@pytest.mark.asyncio
async def test_a_draft_with_no_rules_is_not_submitted():
    out = await _tool("fn helper() {}").run()

    assert isinstance(out, str)
    assert "nothing to submit" in out


@pytest.mark.asyncio
async def test_no_draft_is_not_submitted():
    out = await _tool(None).run()

    assert out == "No harness written yet — put a draft first."


@pytest.mark.asyncio
async def test_an_unreadable_declaration_is_not_submitted():
    """`rule_real` is a real rule. A stamp over only the names that parsed would accept it unverified."""
    out = await _tool("#[rule]\nstruct NotARule {}\n#[rule]\nfn rule_real() {}\n").run()

    assert isinstance(out, str)
    assert "Nothing was submitted" in out


def test_the_submission_selects_the_units_feature():
    """`cargo_check` builds with this set. The submission default is only `certora`, which compiles
    the unit's module out."""
    submission = Submission(manifest_path=Path("p/Cargo.toml"), stem="u", msg="m")
    got = _submission_for(
        SimpleNamespace(  # type: ignore[arg-type]
            submission=submission,
            target=SimpleNamespace(features=("certora", "unit_deposits")),
        ),
        ("rule_a",),
    )

    assert got.features == ("certora", "unit_deposits")
    assert got.rules == SelectRules(("rule_a",))
