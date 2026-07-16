"""Unit tests for the Crucible backend's pure callouts + the event routing (no toolchain / LLM).

The Rust wheel is now a passive service (docs/rust-backend-api.md): these exercise the pure
callouts (`units` / `author_prompt` / `judge_prompt`) directly, and — separately — the
out-of-graph `push_custom_update` routing the Python loop's `emit` relies on.
"""

import json

import pytest

crucible_app = pytest.importorskip(
    "crucible_app",
    reason="crucible_app wheel not built (uv run maturin develop -m rust/crucible-app/Cargo.toml)",
)


def _component_input(*slugs: str) -> str:
    return json.dumps(
        {
            "kind": "component",
            "program": "vault",
            "component": {"name": "vault", "program": "vault"},
            "props": [
                {"title": f"p {s}", "sort": "invariant", "description": "d", "slug": s}
                for s in slugs
            ],
            "context": {"fixture": "struct Fixture {}", "fuzz_timeout": 5},
        }
    )


def _setup_input() -> str:
    return json.dumps(
        {"kind": "setup", "program": "vault", "component": {"programs": []}, "props": [], "context": {}}
    )


def test_descriptor_declares_design_doc_discovery_phase():
    from composer.rustapp.entry import _discovery_phase
    from composer.rustapp.host import build_application

    app = build_application("crucible_app")
    assert app.section_order[0] == "Design Doc Discovery"
    assert _discovery_phase(app) is app.phase["discover_design_doc"]


def test_units_are_one_c_slug_per_property():
    units = json.loads(crucible_app.units(_component_input("solvency", "conservation")))
    assert units == [
        {"property": "p solvency", "unit": "c_solvency"},
        {"property": "p conservation", "unit": "c_conservation"},
    ]


def test_setup_has_no_units():
    assert json.loads(crucible_app.units(_setup_input())) == []


def test_component_author_prompt_lists_each_units_fn_name():
    prompt = json.loads(crucible_app.author_prompt(_component_input("solvency"), None))
    assert prompt.get("system") is None
    # Lists the required fn name + frames the whole-program invariant authoring task.
    assert "c_solvency" in prompt["instruction"]
    assert "test function" in prompt["instruction"]


def test_setup_author_prompt_asks_for_a_fixture():
    prompt = json.loads(crucible_app.author_prompt(_setup_input(), None))
    assert "FIXTURE" in prompt["instruction"]


def test_author_prompt_failure_appends_revise_context():
    failure = json.dumps({"draft": "fn c_x() {}", "errors": "error[E0425]: cannot find value"})
    prompt = json.loads(crucible_app.author_prompt(_component_input("x"), failure))
    assert "FAILED" in prompt["instruction"] and "E0425" in prompt["instruction"]


def test_component_judge_prompt_reviews_the_suite():
    spec = "#[invariant_test]\nfn c_x(fixture: &mut Fixture) {}"
    raw = crucible_app.judge_prompt(_component_input("x"), spec)
    assert raw is not None
    prompt = json.loads(raw)
    # A reviewer persona + the criteria-based task, listing the unit under review and the
    # accept/reject JSON contract the host's _parse_judge consumes.
    assert "Solana security engineer" in prompt["system"]
    ins = prompt["instruction"]
    assert "c_x" in ins
    assert "Criterion 3 — Reachability" in ins
    assert '{"accept": false' in ins
    assert spec in ins


def test_setup_has_no_judge_prompt():
    # The shared fixture is scaffolding, not test evidence — nothing to judge.
    assert crucible_app.judge_prompt(_setup_input(), "spec") is None


def test_review_gate_blocks_until_the_submitted_draft_was_accepted():
    from composer.rustapp import adapter

    # Accept only when the judge accepted THIS exact draft.
    assert adapter._review_gate({"review_ok": True, "reviewed_text": "spec-A"}, "spec-A") is None
    # A draft that was never reviewed (or a different one) is blocked.
    assert adapter._review_gate({"review_ok": True, "reviewed_text": "spec-A"}, "spec-B") is not None
    # A reviewed-but-rejected draft is blocked.
    assert adapter._review_gate({"review_ok": False, "reviewed_text": "spec-A"}, "spec-A") is not None
    # No review yet → blocked.
    assert adapter._review_gate({}, "spec-A") is not None


def test_fetch_verdicts_threads_finding_detail_into_message():
    # A BAD verdict's `detail` (the fuzzer's counterexample / assertion message, captured by
    # `validate`) must reach the report `Verdict.message` so a bare BAD explains itself.
    import asyncio
    from pathlib import Path

    from composer.pipeline.core import Delivered
    from composer.rustapp.adapter import RustFormalizer
    from composer.rustapp.descriptor import AppDescriptor
    from composer.rustapp.result import RustFormalResult
    from composer.spec.source.report.collect import ReportComponentInput
    from composer.spec.source.report.schema import Outcome

    desc = AppDescriptor.model_validate_json(crucible_app.descriptor())
    fz = RustFormalizer(crucible_app, desc)
    res = RustFormalResult(
        verdicts={
            "c_x": {"outcome": "BAD", "detail": "crash abc: deposit(5) — expected 105 got 100"},
            "c_y": {"outcome": "GOOD"},
        }
    )
    inp = ReportComponentInput(
        name="vault", props=[], formalized=Delivered(res, Path("fuzz/vault/src/main.rs"))
    )
    verdicts = asyncio.run(fz.fetch_verdicts(inp))
    assert verdicts["c_x"].outcome == Outcome.BAD
    assert verdicts["c_x"].message == "crash abc: deposit(5) — expected 105 got 100"
    # GOOD verdict with no detail carries no message.
    assert verdicts["c_y"].message is None


def test_author_prompt_judge_failure_uses_review_framing():
    # A judge rejection is NOT a build failure (the draft compiled): the revise prompt must
    # frame it as review feedback, not compiler errors to fix.
    failure = json.dumps(
        {"draft": "fn c_x() {}", "errors": "REJECTED: c_x fails Criterion 3", "kind": "judge"}
    )
    ins = json.loads(crucible_app.author_prompt(_component_input("x"), failure))["instruction"]
    assert "reviewer REJECTED" in ins
    assert "FAILED to build" not in ins
    assert "Criterion 3" in ins


# ---------------------------------------------------------------------------
# The out-of-graph emit routing the Python loop's `emit` relies on.
# ---------------------------------------------------------------------------


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
    from composer.io.context import push_custom_update, with_handler

    rec = _RecordingEventHandler()
    async with with_handler(_NullIO(), rec):
        delivered = push_custom_update(
            {"type": "verdict", "outcome": "GOOD", "name": "solvency"}, thread_id="formalize-0"
        )
        assert delivered is True
    assert rec.events, "custom update never reached handle_event"
    payload, path = rec.events[0]
    assert payload["type"] == "verdict" and payload["outcome"] == "GOOD"
    assert path == ["formalize-0"]


def test_push_custom_update_without_scope_is_dropped_not_raised():
    from composer.io.context import push_custom_update

    assert push_custom_update({"type": "x"}, thread_id="t") is False
