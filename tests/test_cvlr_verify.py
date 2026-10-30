"""``verify_rules``, driven through :func:`gate_tools`.

No cargo and no Prover: the build, the section read, the conf write, and the
submission are stand-ins. The stamp is the digest of the draft and its markings. A submission
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

from composer.authoring.state import checked_digest
from composer.cargo.sbf import Built, SbfRun
from composer.cargo.session import CompileFailed
from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.prover.conf import SelectRules
from composer.prover.core import ProverReport
from composer.prover.ptypes import (
    Counterexample,
    GeneratedKind,
    IncompleteCheck,
    RulePath,
    RuleResult,
    UnknownKind,
    UserAssertion,
)
from composer.prover.results import AlertReport, UnreadableAlertReport
from composer.spec.cvlr import verify as verify_mod
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.env_paths import PathDialect
from composer.spec.cvlr.externals import ExternalWarnings
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing
from composer.spec.cvlr.prover import Checked, Submission, SubmissionFailed
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY, tuning_history
from composer.spec.cvlr.tree import ProjectSource, SharedTree
from composer.spec.cvlr.tuning import SummaryDirective, TuningFiles
from composer.spec.cvlr.verify import (
    BuildPermit,
    HarnessTarget,
    RuleStatusReport,
    StagedUnit,
    VerifyDeps,
    gate_tools,
)

pytestmark = pytest.mark.asyncio

_DRAFT = "#[rule]\nfn rule_a() {}\n#[rule]\nfn rule_b() {}\n"
_LINK = "https://prover.certora.com/jobStatus/1/abc"
_LOOP = IncompleteCheck(
    GeneratedKind.LOOP_UNWINDING, "Unwinding condition in a loop. Try increasing the unwinding bound."
)
_BUILT = SbfRun(duration_ms=1, verdict=Built(manifest=SimpleNamespace(artifact=Path("/w/prog.so"))), confined=True)  # type: ignore[arg-type]


def _checked(
    *pairs: tuple[str, str],
    incomplete: dict[str, IncompleteCheck] | None = None,
    alerts: AlertReport | UnreadableAlertReport | None = None,
) -> Checked:
    report = ProverReport(
        raw_rule_status={RulePath(name): status for name, status in pairs},  # type: ignore[misc]
        result_str="rules",
        link=_LINK,
        certora_run_stdout="",
        incomplete={RulePath(name): check for name, check in (incomplete or {}).items()},
        **({} if alerts is None else {"alerts": alerts}),
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

    state: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "messages": [],
        "skipped": [],
        "expected_failures": {},
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": TunableConf(),
        "validations": {},
        "required_validations": [PROVER_VALIDATION_KEY],
    })

    async def verify(self, draft: str | None = _DRAFT) -> Any:
        state = {**self.state, "curr_spec": draft}
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
        tuning=TuningFiles(envs_dir=tmp_path / "envs", dialect=PathDialect(), unit="deposits"),
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
            submission_dir=tmp_path / "submissions",
            prover_opts=None,  # type: ignore[arg-type]
        )
    )

    async def stage(self, _draft, _summaries, _overlay):
        assert g.sem.locked(), "staged outside the build permit"
        return StagedUnit(self, g.deps.target)

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

    state = {**gate.state, "curr_spec": _DRAFT}
    assert out.update["validations"] == {
        PROVER_VALIDATION_KEY: checked_digest(state, tuning_history(state))  # type: ignore[arg-type]
    }
    assert out.update["prover_link"] == _LINK
    assert "stamped" in _text(out)


async def test_the_conf_names_the_section_and_selects_the_units_feature_and_settings(gate: _Gate):
    """The section, not the draft, names the rules. The submission default is only `certora`,
    which compiles the unit's module out. The settings are the unit's current ones."""
    gate.state["prover_settings"] = TunableConf(loop_iter=4)
    gate.names = {"rule_balance_conserved", "rule_only_authority_withdraws"}
    gate.outcome = _checked()

    await gate.verify()

    [submission] = gate.written
    assert submission.rules == SelectRules(("rule_balance_conserved", "rule_only_authority_withdraws"))
    assert submission.features == ("certora", "certora_unit_deposits")
    assert submission.settings == TunableConf(loop_iter=4)


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        pytest.param(
            _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED")),
            "are not marked as expected failures: rule_b.",
            id="a rule failed",
        ),
        pytest.param(_checked(("rule_a", "VERIFIED")), "No verdict came back for: rule_b.", id="a rule has no verdict"),
    ],
)
async def test_a_listed_rule_that_did_not_verify_does_not_stamp(gate: _Gate, outcome: Checked, said: str):
    gate.outcome = outcome

    out = await gate.verify()

    assert "validations" not in out.update
    assert out.update["prover_link"] == _LINK
    assert said in _text(out)


async def test_a_rule_stopped_by_the_provers_own_assertion_is_reported_apart(gate: _Gate):
    """A loop bound is the prover's limit, not a finding about the program."""
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"), incomplete={"rule_b": _LOOP})

    out = await gate.verify()

    assert "validations" not in out.update
    assert f"rule_b: {_LOOP.assertion}" in _text(out)
    assert "A loop needed more iterations" in _text(out)


async def test_a_kind_this_version_does_not_know_does_not_stamp_when_marked(gate: _Gate):
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}
    new = IncompleteCheck(UnknownKind("someKindAddedLater"), "a bound the Prover added later")
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"), incomplete={"rule_b": new})

    out = await gate.verify()

    assert "validations" not in out.update
    assert "rule_b: a bound the Prover added later" in _text(out)


async def test_a_limit_without_advice_of_its_own_is_reported_by_its_message(gate: _Gate):
    cpi = IncompleteCheck(GeneratedKind.UNRESOLVED_CPI, "CPI call could not be resolved")
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"), incomplete={"rule_b": cpi})

    out = await gate.verify()

    assert "validations" not in out.update
    assert "rule_b: CPI call could not be resolved" in _text(out)
    assert "A loop needed more iterations" not in _text(out)


async def test_a_rule_marked_before_its_first_run_is_stamped_by_that_run(gate: _Gate):
    """A defect found by tracing the program needs no run before the marking."""
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"))

    out = await gate.verify()

    assert "validations" in out.update
    assert "not marked as expected failures" not in _text(out)


async def test_a_marked_rule_stopped_by_the_provers_own_assertion_does_not_stamp(gate: _Gate):
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}
    gate.outcome = _checked(
        ("rule_a", "VERIFIED"),
        ("rule_b", "VIOLATED"),
        incomplete={"rule_b": _LOOP},
    )

    out = await gate.verify()

    assert "validations" not in out.update
    assert "expect_rule_failure does not account for" in _text(out)


async def test_a_timeout_is_not_excused_by_a_marking(gate: _Gate):
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "TIMEOUT"))

    out = await gate.verify()

    assert "validations" not in out.update
    assert "rule_b" in _text(out)
    assert "not marked as expected failures" not in _text(out)


async def test_unreadable_symbols_are_said_rather_than_left_as_a_match(gate: _Gate):
    gate.state["summaries"] = [SummaryDirective(pattern="^foo$", why="formatting")]

    out = await gate.verify()

    assert "could not be read" in _text(out)
    assert "stamped" in _text(out)


async def test_only_the_external_function_alerts_are_reported(gate: _Gate):
    warning = "The following functions are neither inlined nor summarized. [solana_program::program::invoke]"
    gate.outcome = _checked(
        ("rule_a", "VERIFIED"),
        ("rule_b", "VERIFIED"),
        alerts=AlertReport((warning, "an unrelated alert", warning)),
    )

    out = await gate.verify()

    assert "solana_program::program::invoke" in _text(out)
    assert "an unrelated alert" not in _text(out)
    assert out.update["external_functions"] == ExternalWarnings((warning,))


async def test_an_unreadable_alert_report_is_not_reported_as_no_externals(gate: _Gate):
    gate.outcome = _checked(
        ("rule_a", "VERIFIED"),
        ("rule_b", "VERIFIED"),
        alerts=UnreadableAlertReport(),
    )

    out = await gate.verify()

    assert "could not be read" in _text(out)
    assert out.update["external_functions"] == UnreadableAlertReport()


async def test_a_marked_rule_that_verified_does_not_stamp(gate: _Gate):
    """The marking claims a defect this run did not show, so the draft is not ready to publish."""
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}

    out = await gate.verify()

    assert "validations" not in out.update
    assert "marked as expected failures but VERIFIED: rule_b" in _text(out)
    assert "did not reach a property verdict" not in _text(out)


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

    async def run_submission(_prepared, *, callbacks, **_kwargs):
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
            await permit.stage("// draft\n", (), {})


async def test_the_status_report_leaves_out_the_counterexample(tmp_path: Path):
    """A dump per violated rule would flood the author's context."""
    violated = RuleResult(
        path=RulePath(rule="rule_b"),
        counterexample=Counterexample(trace="<the-whole-trace/>", origin=UserAssertion()),
        status="VIOLATED",
    )
    text = await RuleStatusReport().analyze([violated], "tc", None, tmp_path)  # type: ignore[arg-type]
    assert text == "rule_b: VIOLATED"


async def test_a_released_permit_does_not_build(tmp_path: Path):
    """What staging returned keeps no build alive past the permit."""
    target = _target(tmp_path)
    async with target.build_slot() as permit:
        staged = StagedUnit(permit, target)
        permit.release()
        with pytest.raises(RuntimeError):
            await staged.check()


async def test_a_marking_after_the_run_stamps_without_another_submission(gate: _Gate):
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"))
    first = await gate.verify()
    gate.state["last_verdicts"] = first.update["last_verdicts"]
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}

    out = await gate.verify()

    state = {**gate.state, "curr_spec": _DRAFT}
    assert out.update["validations"] == {
        PROVER_VALIDATION_KEY: checked_digest(state, tuning_history(state))  # type: ignore[arg-type]
    }
    assert "nothing was submitted" in _text(out)
    assert len(gate.written) == 1


async def test_a_draft_edited_since_the_run_is_submitted_again(gate: _Gate):
    gate.outcome = _checked(("rule_a", "VERIFIED"), ("rule_b", "VIOLATED"))
    first = await gate.verify()
    gate.state["last_verdicts"] = first.update["last_verdicts"]
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}

    await gate.verify(_DRAFT + "// edited\n")

    assert len(gate.written) == 2


async def test_a_marked_incomplete_check_is_submitted_again(gate: _Gate):
    gate.outcome = _checked(
        ("rule_a", "VERIFIED"),
        ("rule_b", "VIOLATED"),
        incomplete={"rule_b": _LOOP},
    )
    first = await gate.verify()
    gate.state["last_verdicts"] = first.update["last_verdicts"]
    gate.state["expected_failures"] = {"rule_b": "the program underflows"}

    await gate.verify()

    assert len(gate.written) == 2
