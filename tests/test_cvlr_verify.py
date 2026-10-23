"""``verify_rules``, driven through :func:`gate_tools`.

No cargo and no Prover: the build, the section read, the conf write, and the
submission are stand-ins. The stamp is the digest of the draft. A submission
holds the build permit until the job's link arrives, and not through the cloud
job.
"""

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from composer.authoring.state import make_validation_stamper, spec_digest
from composer.cargo.sbf import Built, SbfRun
from composer.cargo.session import CompileFailed
from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.prover.conf import SelectRules
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath
from composer.spec.cvlr import verify as verify_mod
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing
from composer.spec.cvlr.prover import Checked, Submission, SubmissionFailed
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY
from composer.spec.cvlr.tree import ProjectSource, SharedTree
from composer.spec.cvlr.verify import BuildPermit, HarnessTarget, VerifyDeps, gate_tools

pytestmark = pytest.mark.asyncio

_DRAFT = "#[rule]\nfn rule_a() {}\n#[rule]\nfn rule_b() {}\n"
_LINK = "https://prover.certora.com/jobStatus/1/abc"
_BUILT = SbfRun(duration_ms=1, verdict=Built(manifest=SimpleNamespace(artifact=Path("/w/prog.so"))), confined=True)  # type: ignore[arg-type]


def _checked(*pairs: tuple[str, str]) -> Checked:
    report = ProverReport(
        raw_rule_status={RulePath(name): status for name, status in pairs},  # type: ignore[misc]
        result_str="rules",
        link=_LINK,
        certora_run_stdout="",
    )
    return Checked(build=_BUILT, report=report)


@dataclasses.dataclass
class _Gate:
    """The gate's stand-ins. Each test replaces what it is about."""

    deps: VerifyDeps
    build: SbfRun = _BUILT
    names: set[str] | Exception = dataclasses.field(default_factory=lambda: {"rule_a", "rule_b"})
    outcome: Any = dataclasses.field(default_factory=lambda: _checked(("rule_a", "VERIFIED"), ("rule_b", "VERIFIED")))
    written: list[Submission] = dataclasses.field(default_factory=list)

    async def verify(self, draft: str | None = _DRAFT) -> Any:
        state = {
            "messages": [],
            "curr_spec": draft,
            "skipped": [],
            "validations": {},
            "required_validations": [PROVER_VALIDATION_KEY],
        }
        tool = gate_tools(self.deps)[1]
        return await tool.ainvoke(
            {"name": tool.name, "args": {"state": state}, "id": "tc", "type": "tool_call"}
        )

    @property
    def sem(self) -> asyncio.Semaphore:
        return self.deps.target.build_sem


def _target(tmp_path: Path) -> HarnessTarget:
    return HarnessTarget(
        session=SimpleNamespace(workdir=tmp_path),  # type: ignore[arg-type]
        package="p",
        package_root=tmp_path,
        unit=HarnessModule("deposits"),
        tree=SharedTree(pristine=tmp_path / "project", root=tmp_path / "build", source=ProjectSource(roots=())),
        build_sem=asyncio.Semaphore(1),
    )


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Gate:
    install_run_summary(RunSummary())
    g = _Gate(
        VerifyDeps(
            target=_target(tmp_path),
            submission=Submission(manifest_path=Path("p/Cargo.toml"), stem="unit", msg="m"),
            submissions=tmp_path / "submissions",
            prover_opts=None,  # type: ignore[arg-type]
            stamper=make_validation_stamper(PROVER_VALIDATION_KEY),
        )
    )

    async def stage(_self, _draft):
        assert g.sem.locked(), "staged outside the build permit"

    async def build(_session, _submission):
        return g.build

    def names(_so):
        if isinstance(g.names, Exception):
            raise g.names
        return g.names

    async def write(_session, submission, *, into):
        g.written.append(submission)
        return into / "unit.conf"

    async def run_submission(*_args, **_kwargs):
        return g.outcome

    monkeypatch.setattr(BuildPermit, "stage", stage)
    monkeypatch.setattr(verify_mod, "build_for_submission", build)
    monkeypatch.setattr(verify_mod, "rule_names_in_elf", names)
    monkeypatch.setattr(verify_mod, "write_submission", write)
    monkeypatch.setattr(verify_mod, "run_submission", run_submission)
    return g


def _text(out: Any) -> str:
    return out.content if isinstance(out, ToolMessage) else out.update["messages"][0].content


async def test_every_listed_rule_verified_stamps_the_draft(gate: _Gate):
    out = await gate.verify()

    assert out.update["validations"] == {PROVER_VALIDATION_KEY: spec_digest(_DRAFT, [])}
    assert out.update["prover_link"] == _LINK
    assert "stamped" in _text(out)


async def test_the_conf_names_the_section_and_selects_the_units_feature(gate: _Gate):
    """The section, not the draft, names the rules. The submission default is only `certora`,
    which compiles the unit's module out."""
    gate.names = {"rule_balance_conserved", "rule_only_authority_withdraws"}
    gate.outcome = _checked()

    await gate.verify()

    [submission] = gate.written
    assert submission.rules == SelectRules(("rule_balance_conserved", "rule_only_authority_withdraws"))
    assert submission.features == ("certora", "certora_unit_deposits")


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED")), id="a rule failed"),
        pytest.param(_checked(("rule_a", "VERIFIED")), id="a rule has no verdict"),
    ],
)
async def test_a_listed_rule_that_did_not_verify_does_not_stamp(gate: _Gate, outcome: Checked):
    gate.outcome = outcome

    out = await gate.verify()

    assert "validations" not in out.update
    assert out.update["prover_link"] == _LINK
    assert "Not verified: rule_b." in _text(out)


async def test_a_submission_without_results_does_not_stamp(gate: _Gate):
    gate.outcome = SubmissionFailed(build=_BUILT, reason="the cloud job failed")

    out = await gate.verify()

    assert "cloud job failed" in _text(out)
    assert "validations" not in out.update
    assert not gate.sem.locked()


@pytest.mark.parametrize(
    ("build", "names", "said"),
    [
        pytest.param(
            SbfRun(duration_ms=1, verdict=CompileFailed(diagnostics="error[E0425]: cannot find", exit_code=1), confined=True),
            set(),
            "E0425",
            id="rejected build",
        ),
        pytest.param(_BUILT, ManifestMissing("no section"), "no `.cvlr_rules` section", id="missing section"),
        pytest.param(_BUILT, ManifestCorrupt("bad record", offset=12), "offset 12", id="corrupt section"),
        pytest.param(_BUILT, set(), "listed no rules", id="empty section"),
    ],
)
async def test_nothing_is_written_or_stamped_and_the_permit_is_freed(
    gate: _Gate, build: SbfRun, names: set[str] | Exception, said: str
):
    gate.build, gate.names = build, names

    out = await gate.verify()

    assert said in _text(out)
    assert "nothing was submitted" in _text(out).lower()
    assert gate.written == []
    assert not gate.sem.locked()


async def test_no_draft_is_not_submitted(gate: _Gate):
    out = await gate.verify(None)

    assert _text(out) == "No harness written yet — put a draft first."


async def test_the_permit_is_held_until_the_upload_and_not_through_the_cloud_job(
    gate: _Gate, monkeypatch: pytest.MonkeyPatch
):
    uploaded, cloud_done = asyncio.Event(), asyncio.Event()
    held_during_local_phase: list[bool] = []

    async def run_submission(_session, _prepared, *, callbacks, **_kwargs):
        held_during_local_phase.append(gate.sem.locked())
        await callbacks.on_prover_link(_LINK)
        uploaded.set()
        await cloud_done.wait()
        return gate.outcome

    monkeypatch.setattr(verify_mod, "run_submission", run_submission)

    first = asyncio.create_task(gate.verify())
    await uploaded.wait()

    assert held_during_local_phase == [True], "a sibling could replace the .so being uploaded"
    # The job is still in the cloud, and a sibling can build now.
    async with asyncio.timeout(1):
        async with gate.deps.target.build_slot():
            pass

    cloud_done.set()
    assert "stamped" in _text(await first)
    assert not gate.sem.locked()


async def test_releasing_a_permit_twice_releases_it_once(tmp_path: Path):
    """Releasing twice must not leave an extra permit for another unit to take."""
    target = _target(tmp_path)
    sem = target.build_sem
    permit = BuildPermit(target)
    await permit.acquire()
    permit.release()
    permit.release()
    await sem.acquire()
    assert sem.locked(), "the semaphore was over-released"


async def test_a_released_permit_does_not_stage(tmp_path: Path):
    """A submission releases its permit at upload. A sibling may be building by then."""
    async with _target(tmp_path).build_slot() as permit:
        permit.release()
        with pytest.raises(RuntimeError):
            await permit.stage("// draft\n")
