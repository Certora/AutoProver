"""Tests for the two fan-outs inside ``_extract_all`` (``composer/pipeline/core.py``).

Property extraction runs one agent per unit, and each unit first runs its plugins' pre-inference
hooks concurrently. Both fan-outs are gates: extraction is where a run's money goes, so when one
member fails the rest stop instead of spending out a run that can no longer complete, and the
caller gets that member's own error rather than a group to unpack.

Stubs only — no LLM, no DB, no backend wheel: the driver's real fan-out code runs, with the agent
call at the bottom of it replaced.
"""

import asyncio
from typing import Any, cast

import pytest

import composer.pipeline.core as core

pytestmark = pytest.mark.asyncio


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


class _Unit:
    """A feature unit that is nothing but its identity — the driver keys and labels by it."""

    def __init__(self, index: int):
        self.unit_index = index
        self.display_name = f"unit-{index}"
        self.slug = f"unit-{index}"

    def cache_material(self) -> str:
        return f"unit-{self.unit_index}"

    def context_tag(self) -> dict[str, object]:
        return {"unit": self.unit_index}


class _Ctx:
    """A workflow context that hands back itself for any child scope, sync or async."""

    def child(self, *_a, **_kw):
        return _AsyncCtx()


class _AsyncCtx:
    async def child(self, *_a, **_kw):
        return self


class _Source:
    #: No design doc, so the driver adds nothing to the plugins' pre-inference input.
    content = None


class _Run:
    """Just enough ``PipelineRun`` for extraction: a runner that awaits the job inline."""

    source = _Source()
    env = None
    ctx = _Ctx()

    async def runner(self, _task_info, job):
        return await job(None)


class _Prompts:
    """The property-prompt pair, read as arguments of the (stubbed) inference call."""

    system = "system-prompt"
    render_initial = None


class _Ecosystem:
    def __init__(self, units: list[_Unit]):
        self._units = units
        self.property_prompts = _Prompts()

    def units(self, _main) -> list[_Unit]:
        return self._units


class _Plugin:
    def __init__(self, step: _Step):
        self.step = step

    async def property_inference_input_hook(self, _feat, _runner):
        return await self.step.run()


class _Runner:
    """A plugin phase runner bound to one pre-inference hook."""

    def __init__(self, plugin_id: str, step: _Step):
        self.plugin_id = plugin_id
        self.plugin = _Plugin(step)

    def bind(self, *_a, **_kw):
        return self


class _Plugins:
    """A plugin phase manager offering ``pre_runners`` for the pre-inference sub-phase."""

    plugin_digest = None
    plugin_manifest: dict[str, object] = {}

    def __init__(self, pre_runners: list[_Runner] | None = None):
        self._pre_runners = pre_runners or []

    def runners(self, *, sub_phase_id: str, **_kw) -> list[_Runner]:
        return self._pre_runners if sub_phase_id == "pre-inference" else []


async def _extract(monkeypatch, *, units: list[_Unit], plugins: _Plugins, inference: _Step | None,
                   per_unit: dict[int, _Step] | None = None) -> BaseException:
    """Run the real ``_extract_all`` over ``units``; hand back what it raised."""
    steps = per_unit or {}

    async def fake_inference(_ctx, _env, feat, **_kw):
        step = steps.get(feat.unit_index) or inference
        assert step is not None
        return await step.run()

    monkeypatch.setattr(core, "run_property_inference", fake_inference)
    with pytest.raises(BaseException) as caught:  # noqa: PT011 — the outcome under test
        await core._extract_all(
            "properties-key", "main-unit", "guidance", cast(Any, _Run()), phase=1,
            interactive=False, threat_model=None, extra_context=[], max_rounds=1,
            ecosystem=cast(Any, _Ecosystem(units)), plugins=cast(Any, plugins),
        )
    return caught.value


async def test_one_units_failure_cancels_the_other_units_agents(monkeypatch):
    # The per-unit fan-out is the run's biggest concurrent spend, so a unit whose failure ends the
    # run must not leave its siblings running: they stop, and its own error is what surfaces.
    boom = RuntimeError("property inference blew up")
    sibling = _Step(30, None)

    raised = await _extract(
        monkeypatch, units=[_Unit(0), _Unit(1)], plugins=_Plugins(), inference=None,
        per_unit={0: _Step(0.01, boom), 1: sibling},
    )

    assert raised is boom
    assert sibling.cancelled and not sibling.finished


async def test_one_pre_inference_hook_failure_cancels_the_other_hooks(monkeypatch):
    # Same gate one level down: a unit's plugin hooks feed a single inference call, so once one of
    # them has failed the others are working towards a call that will never happen.
    boom = RuntimeError("a pre-inference hook blew up")
    sibling = _Step(30, None)
    plugins = _Plugins([_Runner("failing", _Step(0.01, boom)), _Runner("slow", sibling)])

    raised = await _extract(
        monkeypatch, units=[_Unit(0)], plugins=plugins, inference=_Step(30, None),
    )

    assert raised is boom
    assert sibling.cancelled and not sibling.finished
