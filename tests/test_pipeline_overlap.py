"""Tests for the driver's two overlaps — the build-shaped steps run alongside the LLM steps that
don't depend on them.

``run_pipeline`` overlaps twice (``composer/pipeline/core.py``):

* ``backend.preflight`` (Crucible's program build + harness-skeleton build) with **system analysis**;
* ``prepared.prepare_formalization`` (the prover's autosetup) with **property extraction**.

Neither side of either pair needs the other. Each pair is additionally a *gate*: the two sides share
a task group, so a failure on either cancels the other rather than letting it spend on a run that can
no longer complete.

Stubs throughout — no LLM, no DB, no backend wheel.
"""

import asyncio

import pytest

import composer.pipeline.core as core
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import EVM

# The driver needs *an* ecosystem to reach the overlap under test, but never exercises this one:
# the analysis and extraction it feeds are both monkeypatched below. EVM is the convenient real
# value — ``supports_greenfield=True`` clears the driver's greenfield assert without a live
# ``env``, and its ``analysis_extra_input`` reads only the two fields ``_Source`` supplies.
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
        #: Set once the step is running, so a test that cancels mid-flight can wait for the
        #: thing it means to cancel instead of guessing how long the driver takes to get there.
        self.started = asyncio.Event()

    async def run(self):
        self.started.set()
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
    """The two source fields the shared front half puts in the analysis prompt."""

    contract_name = "Counter"
    relative_path = "src/Counter.sol"


class _Run:
    """Just enough ``PipelineRun`` for the driver: runners that await the job inline."""

    source = _Source()
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

    async def fake_analysis(*_a, **_kw):
        return await analysis.run()

    async def fake_extract_all(*_a, **_kw):
        await extract.run()
        return []  # no batches — the driver's own "nothing extracted" error, if it gets that far

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    backend = _Backend(_Prepared(prep), preflight)
    seen: dict = {"backend": backend}
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
    await asyncio.gather(analysis.started.wait(), preflight.started.wait())
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


async def test_a_setup_failure_cancels_extraction(monkeypatch):
    # Extraction is where the money goes, and its properties have nowhere to be formalized once the
    # setup is dead: the agents stop the moment the setup fails, and that failure is what surfaces —
    # not a downstream "no properties extracted", which is what an *unobserved* setup failure would
    # eventually look like.
    boom = RuntimeError("the formalization setup failed")
    extract = _Step(30, None)
    seen = await _drive(monkeypatch, prep=_Step(0.01, boom), extract=extract)

    assert seen["raised"] is boom
    assert extract.cancelled and not extract.finished


async def test_an_extraction_failure_cancels_the_setup(monkeypatch):
    # Symmetric: a setup whose properties will never arrive is stopped rather than run out its
    # build, and the extraction error is what the caller sees.
    boom = RuntimeError("extraction blew up")
    prep = _Step(30, None)
    seen = await _drive(monkeypatch, prep=prep, extract=_Step(0.01, boom))

    assert seen["raised"] is boom
    assert prep.cancelled and not prep.finished


async def test_cancelling_the_run_cancels_the_second_pair_too(monkeypatch):
    # As with the first pair, a caller that goes away takes both sides with it: neither the setup
    # nor the extraction agents outlive the run that started them.
    prep = _Step(30, None)
    extract = _Step(30, None)

    async def fake_analysis(*_a, **_kw):
        return "analyzed"

    async def fake_extract_all(*_a, **_kw):
        return await extract.run()

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    driver = asyncio.create_task(
        run_pipeline(  # type: ignore[arg-type]
            _Backend(_Prepared(prep)), _Run(), max_bug_rounds=1, ecosystem=ECOSYSTEM,
        )
    )
    await asyncio.gather(prep.started.wait(), extract.started.wait())
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await driver

    assert prep.cancelled and not prep.finished
    assert extract.cancelled and not extract.finished


async def test_both_succeeding_still_reaches_the_drivers_own_checks(monkeypatch):
    # The overlap is preserved: nothing is cancelled when both sides are fine, and the driver's
    # "nothing extracted" guard is what speaks for an empty extraction.
    prep, extract = _Step(0.01, None), _Step(0.01, None)
    seen = await _drive(monkeypatch, prep=prep, extract=extract)

    assert extract.finished and not extract.cancelled
    assert prep.finished
    assert isinstance(seen["raised"], ValueError)
    assert "No properties extracted" in str(seen["raised"])
