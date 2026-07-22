"""End-to-end tests for the Rust application/backend framework (composer.rustapp).

These drive the ``echoprover`` demo wheel (built from ``rust/example-app``) as a
:class:`~autoprover_sdk.Backend`: the pure callouts (``descriptor`` / ``units`` /
``author_prompt`` / ``compile`` / ``validate``) plus the descriptor synthesis and the
host wiring. They need the ``echoprover`` wheel importable (``maturin develop`` in
``rust/example-app``); tests skip cleanly otherwise.

No Postgres / LLM is required — the callouts are pure (echoprover's ``compile`` is a
no-op and ``validate`` returns GOOD), which is the point of the passive-service design:
the loop lives in Python and the wheel just answers questions.
"""

import json

import pytest

echoprover = pytest.importorskip(
    "echoprover",
    reason="build the demo wheel first: (cd rust/example-app && maturin develop)",
)

from composer.rustapp.descriptor import AppDescriptor, CoreSlot
from composer.rustapp.result import RustFormalResult


def _component_input(*titles: str) -> str:
    return json.dumps(
        {
            "kind": "component",
            "program": "Counter",
            "component": {"name": "Counter"},
            "props": [
                {"title": t, "sort": "invariant", "description": "x", "slug": t.replace(" ", "_")}
                for t in titles
            ],
            "context": {},
        }
    )


def test_descriptor_parses_and_maps_core_phases():
    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    assert desc.name == "echoprover"
    assert desc.backend_tag == "echoprover"
    # All four core slots are mapped, plus a UI-only "solving" phase.
    slots = desc.core_slot_map()
    assert set(slots) == set(CoreSlot)
    keys = [p.key for p in desc.ordered_phases()]
    assert keys == ["analysis", "extraction", "solving", "formalization", "report"]


def test_units_are_one_per_property():
    units = json.loads(echoprover.units(_component_input("increment_increases", "never_overflows")))
    assert units == [
        {"property": "increment_increases", "unit": "rule_increment_increases"},
        {"property": "never_overflows", "unit": "rule_never_overflows"},
    ]


def test_author_prompt_lists_the_properties():
    prompt = json.loads(echoprover.author_prompt(_component_input("increment_increases"), None))
    assert "increment_increases" in prompt["instruction"]
    assert prompt.get("system") is None


def test_compile_is_a_noop_ok():
    # The demo accepts any well-formed spec — compile is a no-op gate.
    r = json.loads(echoprover.compile(_component_input("p"), "spec", "/tmp", json.dumps({"argv_prefix": []})))
    assert r == {"status": "ok"}


def test_validate_returns_a_good_verdict():
    res = json.loads(
        echoprover.validate(_component_input("p"), "spec", "rule_p", "/tmp", json.dumps({"argv_prefix": []}))
    )
    # ValidateOutcome: the demo always builds, so a verdict (not build_failed).
    assert res == {"kind": "verdict", "verdict": {"outcome": "GOOD"}}


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


def test_system_doc_is_optional_with_discovery_phase_fallback():
    from composer.rustapp.entry import _discovery_phase, build_arg_parser

    app = host.build_application("echoprover")
    parser = build_arg_parser(app)

    # system_doc may be omitted (→ discovery); still parses.
    ns = parser.parse_args(["/proj", "src/C.sol:C"])
    assert ns.system_doc is None
    assert parser.parse_args(["/proj", "src/C.sol:C", "doc.md"]).system_doc == "doc.md"

    # A wheel that declares no discover_design_doc phase falls back to its first phase.
    first_key = app.descriptor.ordered_phases()[0].key
    assert _discovery_phase(app) is app.phase[first_key]


def test_frontend_labels_and_backend_phases_share_one_enum():
    # The correctness invariant: the phases the driver stamps on TaskInfo (from
    # the backend's core_phases) must be the SAME enum members the frontend's
    # phase_labels are keyed by, or label lookup silently misses.
    from composer.input.files import InMemoryTextFile
    from composer.spec.context import SourceCode
    from composer.spec.system_model import SolidityIdentifier

    app = host.build_application("echoprover")
    source = SourceCode(
        content=InMemoryTextFile(basename="doc.md", string_contents="doc", provider="test"),
        project_root="/tmp/echo-proj",
        contract_name=SolidityIdentifier("C"),
        relative_path="src/C.sol",
        forbidden_read="",
    )
    backend = app.make_backend(source)
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
    # soroban is a valid ChainTag but not registered until a later phase.
    unregistered = desc.model_copy(update={"ecosystem": "soroban"})
    with pytest.raises(ValueError, match="not registered"):
        resolve_ecosystem(unregistered)


def test_descriptor_ecosystem_defaults_to_evm_when_absent():
    # Wheels built before the field existed omit it; the mirror defaults to evm.
    raw = json.loads(echoprover.descriptor())
    del raw["ecosystem"]
    desc = AppDescriptor.model_validate(raw)
    assert desc.ecosystem == "evm"
