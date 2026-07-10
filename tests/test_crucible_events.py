"""Unit tests for the Crucible decider's UI event emission (no toolchain / LLM).

Drives the real `crucible_app` setup / per-component sessions through `drive_session`
with a fake `Effects` that returns canned command results, and asserts the sessions
emit the declared `build_output` / `fuzz_pulse` / `fuzz_finding` events (each carrying
a rendered `line`). This is the decider half of the telemetry parity work; the
Python routing to `handle_event` is covered separately.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

crucible_app = pytest.importorskip(
    "crucible_app",
    reason="crucible_app wheel not built (uv run maturin develop -m rust/crucible-app/Cargo.toml)",
)

from composer.rustapp.loop import GaveUp, drive_session  # noqa: E402
from composer.rustapp.result import RustFormalResult  # noqa: E402


class FakeEffects:
    """Returns canned `run_command` results (one per call, in order) and a fixed
    LLM reply; records every emitted event."""

    def __init__(self, command_results: list[dict], *, llm_reply: str = "fn c_x() {}"):
        self._results = list(command_results)
        self._llm_reply = llm_reply
        self.events: list[tuple[str, dict]] = []

    async def call_llm(self, messages: Any) -> str:
        return self._llm_reply

    async def run_command(self, program: str, args: list[str], files: dict[str, str]) -> dict:
        return self._results.pop(0)

    async def emit(self, event_kind: str, payload: dict) -> None:
        self.events.append((event_kind, payload))

    async def cache_get(self, key: str) -> Any | None:
        return None

    async def cache_put(self, key: str, value: Any) -> None:
        pass

    async def run_prover(self, spec: str, config: Any, rules: list[str] | None) -> dict:
        raise AssertionError("crucible does not use run_prover")

    async def run_feedback(self, spec: str, skipped: Any, rebuttals: Any) -> dict:
        raise AssertionError("crucible does not use run_feedback")


def _kinds(fx: FakeEffects) -> list[str]:
    return [k for k, _ in fx.events]


def _per_component_session(program: str = "vault", slug: str = "deposit"):
    return crucible_app.new_session(
        json.dumps(
            {
                "label": f"{slug} (1 property)",
                "component": {"name": slug, "program": program},
                "props": [{"title": "p1", "sort": "invariant", "description": "d"}],
                "config": {
                    "fixture": "struct Fixture {}",
                    "slug": slug,
                    "program": program,
                    "fuzz_timeout": 5,
                },
            }
        )
    )


def _ok(stdout: str = "fuzzing done", exit_code: int = 0) -> dict:
    return {"exit_code": exit_code, "stdout": stdout, "stderr": ""}


def _assert_all_lines(fx: FakeEffects) -> None:
    for _, payload in fx.events:
        assert isinstance(payload.get("line"), str) and payload["line"], payload


@pytest.mark.asyncio
async def test_per_component_clean_run_emits_pulse_then_held():
    fx = FakeEffects([_ok("ran to timeout, no crash")])
    result = await drive_session(_per_component_session(), fx)

    assert not isinstance(result, GaveUp)
    assert RustFormalResult.from_formalized(result.data).verdicts["c_deposit"]["outcome"] == "GOOD"
    # A "fuzzing…" pulse before the run, then a "held" pulse on the clean result.
    assert _kinds(fx) == ["fuzz_pulse", "fuzz_pulse"]
    _assert_all_lines(fx)


@pytest.mark.asyncio
async def test_per_component_finding_emits_fuzz_finding_and_bad_verdict():
    fx = FakeEffects([_ok("boom\n[FUZZ_FINDING] assertion failed")])
    result = await drive_session(_per_component_session(), fx)

    assert not isinstance(result, GaveUp)
    assert RustFormalResult.from_formalized(result.data).verdicts["c_deposit"]["outcome"] == "BAD"
    assert _kinds(fx) == ["fuzz_pulse", "fuzz_finding"]
    _assert_all_lines(fx)


@pytest.mark.asyncio
async def test_per_component_build_error_emits_build_output_then_retries():
    # First fuzz build fails to compile, second run is clean → GOOD.
    fx = FakeEffects([_ok("error[E0425]: cannot find value"), _ok("clean")])
    result = await drive_session(_per_component_session(), fx)

    assert not isinstance(result, GaveUp)
    # pulse(fuzz) → build_output(revise) → pulse(fuzz again) → pulse(held)
    assert _kinds(fx) == ["fuzz_pulse", "build_output", "fuzz_pulse", "fuzz_pulse"]
    _assert_all_lines(fx)


class _RecordingEventHandler:
    def __init__(self) -> None:
        self.events: list[tuple[dict, list[str]]] = []

    async def handle_event(self, payload: dict, path: list[str], checkpoint_id: str) -> None:
        self.events.append((payload, path))

    async def handle_progress_event(self, payload: dict) -> None:
        pass


class _NullIO:
    async def log_checkpoint_id(self, *, path, checkpoint_id): ...
    async def log_state_update(self, path, st): ...
    async def log_start(self, *, path, description, tool_id): ...
    async def log_end(self, path): ...
    async def human_interaction(self, ty, debug_thunk): return ""


@pytest.mark.asyncio
async def test_push_custom_update_reaches_handle_event_outside_a_graph():
    """The out-of-graph routing RealEffects.emit relies on: a CustomUpdate pushed
    to the with_handler scope reaches EventHandler.handle_event (not the no-op
    progress channel)."""
    from composer.io.context import push_custom_update, with_handler

    rec = _RecordingEventHandler()
    async with with_handler(_NullIO(), rec):
        delivered = push_custom_update(
            {"type": "fuzz_pulse", "line": "fuzzing `c_deposit`"}, thread_id="formalize-0"
        )
        assert delivered is True
    assert rec.events, "custom update never reached handle_event"
    payload, path = rec.events[0]
    assert payload == {"type": "fuzz_pulse", "line": "fuzzing `c_deposit`"}
    assert path == ["formalize-0"]


def test_push_custom_update_without_scope_is_dropped_not_raised():
    from composer.io.context import push_custom_update

    assert push_custom_update({"type": "x"}, thread_id="t") is False


@pytest.mark.asyncio
async def test_setup_session_emits_build_output():
    session = crucible_app.new_setup_session(
        json.dumps({"program": "vault", "analyzed": {"programs": []}, "config": {}})
    )
    assert session is not None
    fx = FakeEffects([_ok("dry-run ok")])
    result = await drive_session(session, fx)

    assert not isinstance(result, GaveUp)
    # A "compiling…" build_output before the dry-run, then a "dry-run OK" one.
    assert _kinds(fx) == ["build_output", "build_output"]
    _assert_all_lines(fx)
