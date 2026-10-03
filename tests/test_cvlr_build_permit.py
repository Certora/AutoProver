"""A submission keeps the build permit until the Prover has uploaded its ``.so``.

Every SBF build of the crate writes the same ``.so`` into the shared target directory, and the
Prover's local phase rebuilds it and uploads it. A sibling unit's build in between, for its own
feature, would replace the file this unit's job uploads. So the permit covers the submission's
local phase and is released when the job's link arrives, not when the cloud job finishes.

No cargo, no Prover: staging, the gate build and the submission are stand-ins.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.spec.cvlr import verify as verify_mod
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.prover import BuildRejected, Prepared, Submission
from composer.spec.cvlr.tree import SharedTree
from composer.spec.cvlr.verify import BuildPermit, HarnessTarget, _RunAccounting

pytestmark = pytest.mark.asyncio

_LINK = "https://prover.certora.com/jobStatus/1/abc"


def _deps(tmp_path: Path, sem: asyncio.Semaphore) -> SimpleNamespace:
    target = HarnessTarget(
        session=SimpleNamespace(workdir=tmp_path),  # type: ignore[arg-type]
        module_path=tmp_path / "m.rs",
        package="p",
        package_root=tmp_path,
        tuning=SimpleNamespace(),  # type: ignore[arg-type]
        unit=HarnessModule("a"),
        tree=SharedTree(pristine=tmp_path, root=tmp_path),
        build_sem=sem,
    )

    async def stage(*_args):
        return None

    return SimpleNamespace(
        target=SimpleNamespace(
            session=target.session, stage=stage, build_slot=target.build_slot
        ),
        submissions=tmp_path / "submissions",
        prover_opts=None,
    )


def _submit(deps: SimpleNamespace):
    return verify_mod._stage_and_submit(
        deps,  # type: ignore[arg-type]
        {"summaries": [], "munges": []},  # type: ignore[arg-type]
        "draft",
        Submission(manifest_path=Path("/w/Cargo.toml"), stem="unit", msg="m"),
        callbacks=_RunAccounting(),
        cex=None,  # type: ignore[arg-type]
        tool_call_id="tc",
    )


@pytest.fixture(autouse=True)
def _built(monkeypatch):
    install_run_summary(RunSummary())

    async def prepared(*_args, **_kwargs):
        return Prepared(build=None, conf_path=Path("/w/unit.conf"))  # type: ignore[arg-type]

    monkeypatch.setattr(verify_mod, "prepare_submission", prepared)


async def test_the_permit_is_held_until_the_upload_and_not_through_the_cloud_job(
    tmp_path, monkeypatch
):
    sem = asyncio.Semaphore(1)
    uploaded, cloud_done = asyncio.Event(), asyncio.Event()
    held_during_local_phase: list[bool] = []

    async def run_submission(_session, _prepared, *, callbacks, **_kwargs):
        held_during_local_phase.append(sem.locked())
        await callbacks.on_prover_link(_LINK)
        uploaded.set()
        await cloud_done.wait()
        return "outcome"

    monkeypatch.setattr(verify_mod, "run_submission", run_submission)

    first = asyncio.create_task(_submit(_deps(tmp_path, sem)))
    await uploaded.wait()

    assert held_during_local_phase == [True], "a sibling could replace the .so being uploaded"
    # The first unit's job is still in the cloud, and a sibling can build now.
    sibling = _deps(tmp_path, sem)
    async with asyncio.timeout(1):
        async with sibling.target.build_slot():
            pass

    cloud_done.set()
    assert await first == (None, "outcome")
    assert not sem.locked()


async def test_a_cli_failure_before_the_upload_frees_the_permit(tmp_path, monkeypatch):
    sem = asyncio.Semaphore(1)

    async def run_submission(*_args, **_kwargs):
        assert sem.locked()
        return "no link"

    monkeypatch.setattr(verify_mod, "run_submission", run_submission)

    assert await _submit(_deps(tmp_path, sem)) == (None, "no link")
    assert not sem.locked()


async def test_a_rejected_build_frees_the_permit_without_submitting(tmp_path, monkeypatch):
    sem = asyncio.Semaphore(1)
    rejected = BuildRejected(build=None)  # type: ignore[arg-type]

    async def prepared(*_args, **_kwargs):
        return rejected

    async def run_submission(*_args, **_kwargs):
        pytest.fail("a rejected build must not be submitted")

    monkeypatch.setattr(verify_mod, "prepare_submission", prepared)
    monkeypatch.setattr(verify_mod, "run_submission", run_submission)

    assert await _submit(_deps(tmp_path, sem)) == (None, rejected)
    assert not sem.locked()


async def test_releasing_a_permit_twice_releases_it_once():
    """The link releases it, and so does the end of the slot. A second release must not hand a
    second unit the permit while a third already holds it."""
    sem = asyncio.Semaphore(1)
    permit = BuildPermit(sem)
    await permit.acquire()
    permit.release()
    permit.release()
    await sem.acquire()
    assert sem.locked(), "the semaphore was over-released"
