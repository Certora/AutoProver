"""A draft stamps when every rule the build listed verified.

No cargo and no Prover. The report is a stand-in, and the stamp is the digest
of the draft.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.authoring.state import make_validation_stamper, spec_digest
from composer.cargo.sbf import BuildManifest, Built, SbfRun
from composer.cargo.session import CompileFailed
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath
from composer.prover.conf import SelectRules
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.manifest import rule_names_in_elf
from composer.spec.cvlr.prover import BuildRejected, Checked, Submission, SubmissionFailed
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY
from composer.spec.cvlr.tree import ProjectSource, SharedTree
from composer.spec.cvlr.verify import HarnessTarget, VerifyRules, _RunAccounting, _stage_and_submit, _submission_for
from tests.test_cvlr_manifest import elf_with_cvlr_rules, record

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
async def test_no_draft_is_not_submitted():
    out = await _tool(None).run()

    assert out == "No harness written yet — put a draft first."


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


@pytest.mark.asyncio
async def test_the_conf_names_the_rules_the_section_listed(tmp_path, monkeypatch):
    """The draft names a rule the section does not. The conf follows the section."""
    so = tmp_path / "prog.so"
    so.write_bytes(
        elf_with_cvlr_rules(
            record(1, b"rule_balance_conserved") + record(1, b"rule_only_authority_withdraws")
        )
    )
    written: list[Submission] = []

    async def build(*_args, **_kwargs):
        return SbfRun(
            duration_ms=1,
            verdict=Built(
                manifest=BuildManifest(
                    success=True,
                    project_directory=tmp_path,
                    executables="prog.so",
                    sources=("src/lib.rs",),
                )
            ),
            confined=True,
        )

    async def write(_session, submission, *, into):
        written.append(submission)
        return tmp_path / "unit.conf"

    async def run_submission(*_args, **_kwargs):
        return "ran"

    monkeypatch.setattr("composer.spec.cvlr.verify.build_for_submission", build)
    monkeypatch.setattr("composer.spec.cvlr.verify.rule_names_in_elf", rule_names_in_elf)
    monkeypatch.setattr("composer.spec.cvlr.verify.write_submission", write)
    monkeypatch.setattr("composer.spec.cvlr.verify.run_submission", run_submission)

    target = HarnessTarget(
        session=SimpleNamespace(workdir=tmp_path),  # type: ignore[arg-type]
        package="p",
        package_root=tmp_path,
        unit=HarnessModule("a"),
        tree=SharedTree(
            pristine=tmp_path / "project", root=tmp_path / "build", source=ProjectSource(roots=())
        ),
        build_sem=asyncio.Semaphore(1),
    )

    async def stage(*_args):
        return None

    deps = SimpleNamespace(
        target=SimpleNamespace(session=target.session, stage=stage, build_slot=target.build_slot),
        submissions=tmp_path / "submissions",
        prover_opts=None,
    )
    outcome = await _stage_and_submit(
        deps,  # type: ignore[arg-type]
        {},  # type: ignore[arg-type]
        "#[rule]\nfn rule_decoy_not_in_the_elf() {}",
        Submission(manifest_path=Path("p/Cargo.toml"), stem="unit", msg="m"),
        callbacks=_RunAccounting(),
        cex=None,  # type: ignore[arg-type]
        tool_call_id="tc",
    )

    assert outcome == "ran"
    assert len(written) == 1
    assert written[0].rules == SelectRules(("rule_balance_conserved", "rule_only_authority_withdraws"))
