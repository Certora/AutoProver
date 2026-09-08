"""Tests for the driver's two overlaps — the build-shaped steps run alongside the LLM steps that
don't depend on them.

``run_pipeline`` overlaps twice (``composer/pipeline/core.py``):

* ``backend.preflight`` (Crucible's program build + harness-skeleton build) with **system analysis**;
* ``prepared.prepare_formalization`` (the prover's autosetup) with **property extraction**.

Neither side of either pair needs the other. The preflight is additionally a *gate*: it shares a task
group with the analysis, so a failure on either side cancels the other rather than letting it spend
on a run that can no longer complete. Nothing else is cancelled — the second pair is awaited in turn.

Stubs throughout — no LLM, no DB, no backend wheel.
"""

import asyncio
from pathlib import Path

import pytest

from composer.pipeline.run_mode import RunMode
import composer.pipeline.core as core
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM
from composer.spec.util import fs_forbidden_read

# The driver needs *an* ecosystem to reach the overlap under test, but never exercises this one:
# the analysis and extraction it feeds are both monkeypatched below. EVM is the convenient real
# value — ``supports_greenfield=True`` clears the driver's greenfield assert without a live
# ``env``, and its ``analysis_extra_input`` reads only fields ``_Source`` supplies.
ECOSYSTEM = EVM

pytestmark = pytest.mark.asyncio

#: What a successful stub preflight hands forward to ``prepare_system``.
PREFLIGHT_RESULT = "prepared-workspace"


class _Store:
    def write_properties(self, *_a, **_kw): ...
    def write_artifact(self, *_a, **_kw): return "artifact"
    def write_report(self, *_a, **_kw): ...


class _Step:
    """A stub async step that takes ``delay`` seconds and then fails or returns ``value``."""

    def __init__(self, delay: float, error: Exception | None, value: object = None):
        self.delay, self.error, self.value = delay, error, value
        self.finished = False
        self.cancelled = False

    async def run(self):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        self.finished = True
        return self.value


class _Prepared:
    """A prepared system whose ``prepare_formalization`` runs one :class:`_Step`."""

    main = "main-unit"

    def __init__(self, step: _Step):
        self.step = step

    async def prepare_formalization(self, _run):
        return await self.step.run()


class _Backend:
    analysis_spec = core.SystemAnalysisSpec("analysis-key", "properties-key")
    core_phases = {"analysis": 1, "extraction": 2, "formalization": 3, "report": 4}
    backend_guidance = "guidance"
    artifact_store = _Store()

    def __init__(self, prepared: _Prepared, preflight: _Step | None = None):
        self._prepared = prepared
        self.preflight_step = preflight or _Step(0, None, PREFLIGHT_RESULT)
        #: What the driver handed to ``prepare_system`` — the preflight's own result.
        self.seen_preflight: object = None

    async def preflight(self, _run):
        return await self.preflight_step.run()

    async def prepare_system(self, _analyzed, _run, preflight):
        self.seen_preflight = preflight
        return self._prepared

    def to_artifact_id(self, _c): return "artifact-id"


class _Ctx:
    """A workflow context that hands back itself for any child scope."""

    recursion_limit = 10

    def child(self, *_a, **_kw):
        return self


class _Source:
    """The source fields the shared front half reads: two that go into the analysis prompt, and
    the surface the analysis validator resolves declared paths against."""

    contract_name = "Counter"
    relative_path = "src/Counter.sol"
    project_root = "/nonexistent/project"
    forbidden_read = staticmethod(fs_forbidden_read)


class _Run:
    """Just enough ``PipelineRun`` for the driver: runners that await the job inline."""

    source = _Source()
    run_mode = RunMode.COMPREHENSIVE
    env = None
    ctx = _Ctx()

    async def runner(self, _task_info, job):
        return await job()

    async def cpu_runner(self, _task_info, job):
        return await job()


async def _drive(
    monkeypatch,
    *,
    prep: _Step,
    extract: _Step,
    analysis: _Step | None = None,
    preflight: _Step | None = None,
) -> dict:
    """Run the driver with stubbed analysis + extraction; report the outcome and the backend."""
    analysis = analysis or _Step(0, None, "analyzed")
    backend = _Backend(_Prepared(prep), preflight)
    seen: dict = {"backend": backend}

    async def fake_analysis(*_a, **_kw):
        # The source surface the driver resolved for the analysis validator.
        seen["project_root"] = _kw.get("project_root")
        seen["forbidden_read"] = _kw.get("forbidden_read")
        return await analysis.run()

    async def fake_extract_all(*_a, **_kw):
        await extract.run()
        return []  # no batches — the driver's own "nothing extracted" error, if it gets that far

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    try:
        await run_pipeline(backend, _Run(), max_bug_rounds=1, ecosystem=ECOSYSTEM)  # type: ignore[arg-type]
    except BaseException as exc:  # noqa: BLE001 — the outcome under test
        seen["raised"] = exc
    return seen


# ---------------------------------------------------------------------------
# preflight ∥ system analysis
# ---------------------------------------------------------------------------


async def test_a_preflight_failure_stops_system_analysis(monkeypatch):
    # The whole point of gating the workspace this early: a toolchain failure must not wait out the
    # analysis agent already running beside it (and must never reach extraction, which is where the
    # real spend is).
    boom = RuntimeError("the harness workspace does not build")
    analysis = _Step(30, None, "analyzed")
    extract = _Step(30, None)
    seen = await _drive(
        monkeypatch, prep=_Step(0, None), extract=extract,
        analysis=analysis, preflight=_Step(0.01, boom),
    )

    assert seen["raised"] is boom
    assert analysis.cancelled and not analysis.finished
    # Extraction never even started: it is created only after prepare_system.
    assert not extract.finished and not extract.cancelled


async def test_an_analysis_failure_stops_the_preflight(monkeypatch):
    # Symmetric: the gate spends no money, but it is the run's slowest non-LLM step, so an analysis
    # that has already failed must not wait out a workspace build whose result nothing will read.
    boom = RuntimeError("system analysis blew up")
    preflight = _Step(30, None, PREFLIGHT_RESULT)
    seen = await _drive(
        monkeypatch, prep=_Step(0, None), extract=_Step(0, None),
        analysis=_Step(0.01, boom), preflight=preflight,
    )

    assert seen["raised"] is boom
    assert preflight.cancelled and not preflight.finished


async def test_a_double_failure_reports_both(monkeypatch):
    # The one case the driver cannot answer with a single error: both sides raise before either
    # cancellation lands, so neither is "the" cause and the group carries them both.
    analysis_boom = RuntimeError("system analysis blew up")
    preflight_boom = RuntimeError("the harness workspace does not build")
    seen = await _drive(
        monkeypatch, prep=_Step(0, None), extract=_Step(0, None),
        analysis=_Step(0, analysis_boom), preflight=_Step(0, preflight_boom),
    )

    raised = seen["raised"]
    assert isinstance(raised, BaseExceptionGroup)
    assert set(raised.exceptions) == {analysis_boom, preflight_boom}


async def test_cancelling_the_run_cancels_the_steps_it_was_waiting_on(monkeypatch):
    # When the *caller* goes away (Ctrl-C, an enclosing timeout), both overlapped steps must go with
    # it: the awaited one comes along for free, but the analysis the driver is not sitting on would
    # keep running detached — a multi-minute agent outliving the run that started it.
    analysis = _Step(30, None, "analyzed")
    preflight = _Step(30, None, PREFLIGHT_RESULT)

    async def fake_analysis(*_a, **_kw):
        return await analysis.run()

    async def fake_extract_all(*_a, **_kw):
        return []

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    driver = asyncio.create_task(
        run_pipeline(  # type: ignore[arg-type]
            _Backend(_Prepared(_Step(0, None)), preflight), _Run(),
            max_bug_rounds=1, ecosystem=ECOSYSTEM,
        )
    )
    await asyncio.sleep(0.05)  # both overlapped steps are now in flight
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await driver

    assert analysis.cancelled and not analysis.finished
    assert preflight.cancelled and not preflight.finished


async def test_the_preflight_result_is_handed_to_prepare_system(monkeypatch):
    # The backend's prep travels forward as a value, so a backend can build on it as immutable state
    # instead of stashing it on itself between the two calls.
    seen = await _drive(monkeypatch, prep=_Step(0, None), extract=_Step(0, None))
    assert seen["backend"].seen_preflight == PREFLIGHT_RESULT


# ---------------------------------------------------------------------------
# prepare_formalization ∥ property extraction
# ---------------------------------------------------------------------------


async def test_a_setup_failure_ends_the_run_once_extraction_is_done(monkeypatch):
    # This pair is only overlapped, not gated: extraction runs to completion, and setup's failure is
    # then reported as itself — not as a downstream "no properties extracted", which is what an
    # entirely *unobserved* setup failure would look like.
    boom = RuntimeError("cargo-build-sbf failed (exit 101)")
    extract = _Step(0.03, None)
    seen = await _drive(monkeypatch, prep=_Step(0.01, boom), extract=extract)

    assert seen["raised"] is boom
    assert extract.finished and not extract.cancelled


async def test_an_extraction_failure_ends_the_run_with_its_own_error(monkeypatch):
    # The other direction: extraction is awaited first, so its failure is what surfaces.
    boom = RuntimeError("extraction blew up")
    prep = _Step(0, None)
    seen = await _drive(monkeypatch, prep=prep, extract=_Step(0.01, boom))

    assert seen["raised"] is boom
    assert prep.finished


async def test_both_succeeding_still_reaches_the_drivers_own_checks(monkeypatch):
    # The overlap is preserved: nothing is cancelled when both sides are fine, and the driver's
    # "nothing extracted" guard is what speaks for an empty extraction.
    prep, extract = _Step(0.01, None), _Step(0.01, None)
    seen = await _drive(monkeypatch, prep=prep, extract=extract)

    assert extract.finished and not extract.cancelled
    assert prep.finished
    # The analysis validator resolves the model's declared source paths against this surface, so
    # the driver has to hand it the source's own, not a default.
    assert seen["project_root"] == Path(_Source.project_root)
    assert seen["forbidden_read"] is fs_forbidden_read
    assert isinstance(seen["raised"], ValueError)
    assert "No properties extracted" in str(seen["raised"])
