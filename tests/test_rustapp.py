"""End-to-end tests for the Rust application/backend framework (composer.rustapp).

These drive the *real* Rust decider (the ``echoprover`` demo wheel built from
``rust/example-app``) through the inversion-of-control loop, plus the descriptor
synthesis and result round-trip. They need the ``echoprover`` wheel importable
(``maturin develop`` in ``rust/example-app``); tests skip cleanly otherwise.

No Postgres / LLM is required: the loop's effects are supplied by a fake, which
is the whole point of the IoC design — the async I/O lives in Python and can be
stubbed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

echoprover = pytest.importorskip(
    "echoprover",
    reason="build the demo wheel first: (cd rust/example-app && maturin develop)",
)

from composer.rustapp.descriptor import AppDescriptor, CoreSlot
from composer.rustapp.loop import Effects, GaveUp, drive_session
from composer.rustapp.result import RustFormalResult


SESSION_INPUT = json.dumps(
    {
        "label": "Counter (2 properties)",
        "component": {"name": "Counter"},
        "props": [
            {"title": "increment_increases", "sort": "safety_property", "description": "x"},
            {"title": "never_overflows", "sort": "invariant", "description": "y"},
        ],
        "config": {},
    }
)


class FakeEffects:
    """Records the effect trace and returns canned observations. ``verified``
    controls the prover result; ``cached`` seeds a cache hit."""

    def __init__(self, *, verified: bool = True, cached: dict[str, Any] | None = None):
        self.verified = verified
        self.store: dict[str, Any] = dict(cached or {})
        self.trace: list[str] = []
        self.events: list[tuple[str, dict]] = []

    async def call_llm(self, messages: Any) -> str:
        self.trace.append("call_llm")
        return "spec { rule increment_increases; rule never_overflows; }"

    async def run_prover(self, spec: str, config: Any, rules: list[str] | None) -> dict:
        self.trace.append("run_prover")
        return {"verified": self.verified}

    async def run_feedback(self, spec: str, skipped: Any, rebuttals: Any) -> dict:
        self.trace.append("run_feedback")
        return {"good": True}

    async def cache_get(self, key: str) -> Any | None:
        self.trace.append("cache_get")
        return self.store.get(key)

    async def cache_put(self, key: str, value: Any) -> None:
        self.trace.append("cache_put")
        self.store[key] = value

    async def emit(self, event_kind: str, payload: dict) -> None:
        self.trace.append("emit")
        self.events.append((event_kind, payload))


def test_descriptor_parses_and_maps_core_phases():
    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    assert desc.name == "echoprover"
    assert desc.backend_tag == "echoprover"
    # All four core slots are mapped, plus a UI-only "solving" phase.
    slots = desc.core_slot_map()
    assert set(slots) == set(CoreSlot)
    keys = [p.key for p in desc.ordered_phases()]
    assert keys == ["analysis", "extraction", "solving", "formalization", "report"]


@pytest.mark.asyncio
async def test_loop_publishes_on_verified():
    session = echoprover.new_session(SESSION_INPUT)
    fx = FakeEffects(verified=True)
    result = await drive_session(session, fx)

    assert not isinstance(result, GaveUp)
    res = RustFormalResult.from_formalized(result.data)
    assert res.property_units() == [
        ("increment_increases", ["rule_increment_increases"]),
        ("never_overflows", ["rule_never_overflows"]),
    ]
    assert res.output_link == "local://echo/run"
    # Full effect trace: emit → cache miss → llm → cache put → prover.
    assert fx.trace == ["emit", "cache_get", "call_llm", "cache_put", "run_prover"]
    assert fx.events == [("solver_line", {"line": "echo: starting formalization"})]


@pytest.mark.asyncio
async def test_loop_gives_up_when_not_verified():
    session = echoprover.new_session(SESSION_INPUT)
    fx = FakeEffects(verified=False)
    result = await drive_session(session, fx)
    assert isinstance(result, GaveUp)
    assert "did not verify" in result.reason


@pytest.mark.asyncio
async def test_loop_uses_cache_hit_and_skips_llm():
    session = echoprover.new_session(SESSION_INPUT)
    fx = FakeEffects(verified=True, cached={"echo_draft": "cached spec text"})
    result = await drive_session(session, fx)
    assert not isinstance(result, GaveUp)
    # On a cache hit the loop must skip the LLM and go straight to the prover.
    assert "call_llm" not in fx.trace
    assert fx.trace == ["emit", "cache_get", "run_prover"]


def test_result_round_trips_through_cache_serialization():
    # The driver caches by model_dump/validate; ensure that survives.
    res = RustFormalResult.from_formalized(
        {
            "commentary": "c",
            "artifact_text": "spec",
            "property_units": [("p", ["rule_p"])],
            "skipped": [{"property_title": "q", "reason": "n/a"}],
            "output_link": "local://x",
        }
    )
    reloaded = RustFormalResult.model_validate_json(res.model_dump_json())
    assert reloaded.property_units() == [("p", ["rule_p"])]
    assert reloaded.artifact_text == "spec"
    assert reloaded.skipped[0].property_title == "q"


def test_effects_protocol_is_satisfied_by_fake():
    # Structural sanity: FakeEffects satisfies the Effects protocol.
    fx: Effects = FakeEffects()
    assert fx is not None


# ---------------------------------------------------------------------------
# Generic host: entry point (argparse), shared-enum identity, frontend.
# These import the heavier host (needs the full composer stack). If it can't
# import (e.g. running against a slim env), skip rather than error.
# ---------------------------------------------------------------------------

host = pytest.importorskip(
    "composer.rustapp.host", reason="needs the full composer stack installed"
)


def test_entry_argparser_has_positionals_and_declared_flags():
    from composer.rustapp.entry import build_arg_parser

    app = host.build_application("echoprover")
    parser = build_arg_parser(app)

    # Declared flag default (from the descriptor's ArgSpec) is applied.
    args = parser.parse_args(["/proj", "src/C.sol:C", "doc.md"])
    assert args.project_root == "/proj"
    assert args.main_contract == "src/C.sol:C"
    assert args.system_doc == "doc.md"
    assert args.max_concurrent == 4
    assert args.echo_tag == "demo"

    # …and is overridable.
    args2 = parser.parse_args(["/proj", "src/C.sol:C", "doc.md", "--echo-tag", "hi"])
    assert args2.echo_tag == "hi"


def test_frontend_labels_and_backend_phases_share_one_enum():
    # The correctness invariant: the phases the driver stamps on TaskInfo (from
    # the backend's core_phases) must be the SAME enum members the frontend's
    # phase_labels are keyed by, or label lookup silently misses.
    app = host.build_application("echoprover")
    backend = app.make_backend("/tmp/echo-proj")
    for slot, member in backend.core_phases.items():
        assert member in app.phase_labels, (slot, member)
    # Section order lists every declared phase's label.
    assert set(app.section_order) == set(app.phase_labels.values())


def test_generic_console_handler_renders_declared_events(capsys):
    import asyncio

    from composer.rustapp.frontend import GenericRustConsoleHandler, _render_event

    assert _render_event({"type": "solver_line", "line": "hello"}) == "hello"
    assert _render_event({"type": "x", "a": 1}) == '{"a": 1}'

    handler = GenericRustConsoleHandler({"solver_line"})
    asyncio.run(handler.handle_event({"type": "solver_line", "line": "L1"}, ["t"], "cp"))
    # An undeclared kind is ignored.
    asyncio.run(handler.handle_event({"type": "other", "line": "nope"}, ["t"], "cp"))
    out = capsys.readouterr().out
    assert "solver_line: L1" in out
    assert "nope" not in out


def test_generic_tui_app_constructs():
    from composer.rustapp.frontend import GenericRustApp

    app = host.build_application("echoprover")
    tui = GenericRustApp(
        phase_labels=app.phase_labels,
        section_order=app.section_order,
        header_text=app.header_text,
        event_kinds={e.kind for e in app.descriptor.event_kinds},
    )
    assert tui is not None


def test_descriptor_carries_ecosystem_and_resolves():
    from composer.pipeline.ecosystem import EVM
    from composer.rustapp.host import resolve_ecosystem

    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    assert desc.ecosystem == "evm"
    assert resolve_ecosystem(desc) is EVM


def test_build_application_carries_resolved_ecosystem():
    from composer.pipeline.ecosystem import EVM

    app = host.build_application("echoprover")
    assert app.ecosystem is EVM


def test_resolve_ecosystem_rejects_unregistered_chain():
    from composer.rustapp.host import resolve_ecosystem

    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    # solana is a valid ChainTag but not registered until a later phase.
    unregistered = desc.model_copy(update={"ecosystem": "solana"})
    with pytest.raises(ValueError, match="not registered"):
        resolve_ecosystem(unregistered)


def test_descriptor_ecosystem_defaults_to_evm_when_absent():
    # Wheels built before the field existed omit it; the mirror defaults to evm.
    import json

    raw = json.loads(echoprover.descriptor())
    del raw["ecosystem"]
    desc = AppDescriptor.model_validate(raw)
    assert desc.ecosystem == "evm"
