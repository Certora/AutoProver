"""End-to-end tests for the Rust application/backend framework (composer.rustapp).

These drive the ``echoprover`` demo wheel (built from ``rust/example-app``) as a
:class:`~autoprover_sdk.Backend`: the pure callouts (``descriptor`` / ``target_for`` /
``author_prompt`` / ``compile`` / ``validate``) plus the descriptor synthesis and the
host wiring. They need the ``echoprover`` wheel importable — ``uv sync`` builds it (the
``apps`` group, pulled in via ``dev``); tests skip cleanly otherwise.

No Postgres / LLM is required — the callouts are pure (echoprover's ``compile`` is a
no-op and reading the spec is its whole checker), which is the point of the
passive-service design: the loop lives in Python and the wheel just answers questions.
"""

import json

import pytest
from pydantic import ValidationError

echoprover = pytest.importorskip(
    "echoprover",
    reason="demo wheel not built; run `uv sync` (builds rust/example-app)",
)

from composer.rustapp.descriptor import AppDescriptor, PhaseRole
from composer.rustapp.result import RustFormalResult
from composer.rustapp.wire import ComponentInput, Property, Target, Check, Verdict
from composer.authoring.state import SkippedProperty
from composer.spec.source.report.schema import Outcome
from tests.conftest import wire_verdict


def _component_input(*titles: str) -> str:
    return ComponentInput(
        program="Counter",
        unit={"name": "Counter"},
        props=[
            Property(
                component="Counter", title=t, sort="invariant", description="x",
                slug=t.replace(" ", "_"),
            )
            for t in titles
        ],
    ).model_dump_json()


def _target(*checks: str) -> str:
    """A target and the report rows it covers — what the host passes ``validate``."""
    return Target(
        name=checks[0], checks=[Check(name=c, properties=["p"], target=None) for c in checks]
    ).model_dump_json()


def _sandbox() -> str:
    """A passthrough ``Sandbox``: no confinement wrapper (``composer.sandbox.config.BackendSpec``)."""
    return json.dumps({"argv_prefix": [], "timeout_s": 600})


def test_descriptor_parses_and_maps_core_phases():
    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    assert desc.name == "echoprover"
    # A tag from the closed ``ReportBackend`` set: the demo borrows the prover's outcome wording
    # rather than inventing a vocabulary the report can't render. Anything outside the set fails
    # here, at descriptor load, rather than later when the formalizer is constructed.
    assert desc.backend_tag == "prover"
    # Every *required* slot is mapped, plus a UI-only "solving" phase. The optional DISCOVERY slot is
    # left unclaimed, which is the common case: the design-doc task then groups under the first phase.
    slots = desc.role_map()
    assert set(slots) == set(PhaseRole.required())
    assert PhaseRole.DISCOVERY not in slots
    keys = [p.key for p in desc.ordered_phases()]
    assert keys == ["analysis", "extraction", "solving", "formalization", "report"]


def test_each_declared_check_is_its_own_target_by_default():
    # The demo groups nothing, so every check the author declares is its own invocation. `None`,
    # not a name — the host reads that as "its own target" without the wheel spelling one.
    assert echoprover.target_for(_component_input("increment_increases"), "rule_x") is None


def test_author_prompt_lists_the_properties():
    prompt = json.loads(echoprover.author_prompt(_component_input("increment_increases")))
    assert "increment_increases" in prompt["instruction"]
    assert prompt.get("system") is None


def test_compile_is_a_noop_ok():
    # The demo accepts any well-formed spec — compile is a no-op gate.
    r = json.loads(echoprover.compile(_component_input("p"), "spec", "/tmp", _sandbox()))
    assert r == {"status": "ok"}


def test_compile_takes_no_spec_at_all_for_a_preflight():
    # The preflight has nothing authored to build, so the callout crosses the FFI with `None` —
    # what tells a wheel whose toolchain writes the spec to a file to render its own skeleton
    # instead of writing an empty one.
    r = json.loads(echoprover.compile(_component_input("p"), None, "/tmp", _sandbox()))
    assert r == {"status": "ok"}


def test_validate_returns_a_verdict_for_every_row_the_target_covers():
    sandbox = _sandbox()
    spec = "rule rule_p: ok\nrule rule_a: ok\nrule rule_b: ok"
    res = json.loads(
        echoprover.validate(_component_input("p"), spec, _target("rule_p"), "/tmp", sandbox)
    )
    # ValidateOutcome: the spec declares the rule, so per-unit verdicts (not build_failed).
    assert res == {"kind": "verdicts", "verdicts": [["rule_p", wire_verdict("GOOD")]]}

    # A target covering several rows answers for all of them in the one run — the wheel keys the
    # verdicts off the units the host sent, so it never has to spell a unit name itself.
    shared = json.loads(
        echoprover.validate(_component_input("p"), spec, _target("rule_a", "rule_b"), "/tmp", sandbox)
    )
    assert [u for u, _v in shared["verdicts"]] == ["rule_a", "rule_b"]


def test_a_declared_check_the_spec_does_not_contain_does_not_pass():
    # The names are the author's, so validate is what holds them to the artifact: a rule nobody
    # wrote has nothing behind it and must not stamp a property as verified.
    res = json.loads(
        echoprover.validate(_component_input("p"), "rule rule_p: ok", _target("rule_ghost"),
                            "/tmp", _sandbox())
    )
    (name, verdict), = res["verdicts"]
    assert name == "rule_ghost"
    assert verdict["outcome"] == "ERROR" and "rule_ghost" in (verdict["detail"] or "")


def test_result_round_trips_through_cache_serialization():
    # The driver caches by model_dump/validate, so everything the loop accumulates has to survive
    # that — including the nested per-check verdicts the wheel published.
    res = RustFormalResult(
        commentary="c",
        artifact_text="spec",
        checks=[("p", ["rule_p"])],
        skipped=[SkippedProperty(property_title="q", reason="n/a")],
        output_link="local://x",
        verdicts={"rule_p": Verdict(outcome=Outcome.BAD, line=7, detail="counterexample",
                                    duration_seconds=None, unit_file=None,
                                    accounting="campaign spent 41231 executions",
                                    finding=None)},
    )
    reloaded = RustFormalResult.model_validate_json(res.model_dump_json())
    assert reloaded.property_checks() == [("p", ["rule_p"])]
    assert reloaded.artifact_text == "spec"
    assert reloaded.skipped[0].property_title == "q"
    assert reloaded.verdicts["rule_p"].outcome is Outcome.BAD
    assert reloaded.verdicts["rule_p"].detail == "counterexample"


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


def test_the_parser_the_entry_point_runs_is_the_one_that_carries_help_text():
    # ``build_arg_parser`` is the single definition ``rust_entry_point`` runs, not a hand-copy of an
    # inline one — a second copy drifts, and a copy missing its help strings makes ``--help`` from
    # the introspection path document nothing.
    from composer.rustapp.entry import build_arg_parser

    # argparse re-wraps help to the terminal width, so compare on collapsed whitespace.
    help_text = " ".join(build_arg_parser(host.build_application("echoprover")).format_help().split())
    # Hyphenated words are what argparse breaks *across* lines, so these fragments avoid them.
    for expected in (
        "Project root",
        "Main contract as path:ContractName",
        "Path to the design document",
        "Max concurrent agents",
        "Cache namespace",
        "Memory namespace",
        "Interactively refine extracted properties",
        "rounds per component",
        "Max graph iterations",
    ):
        assert expected in help_text, expected


def test_declared_flags_are_threaded_by_dest_and_nothing_else_is():
    # What reaches ``validate_preconditions`` / every component's context: the descriptor's own
    # flags, keyed by the dest argparse gave them — not the host's built-in options.
    from composer.rustapp.entry import _declared_args, build_arg_parser

    app = host.build_application("echoprover")
    ns = build_arg_parser(app).parse_args(["/proj", "src/C.sol:C", "doc.md", "--echo-tag", "hi"])
    assert _declared_args(ns, app.descriptor.args) == {"echo_tag": "hi"}


def test_the_unit_noun_defaults_and_pluralizes():
    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    assert desc.component_noun is None  # the demo declares none
    assert desc.unit_noun() == "component"
    assert desc.unit_noun(plural=True) == "components"
    named = desc.model_copy(update={"component_noun": "instruction"})
    assert named.unit_noun() == "instruction"
    assert named.unit_noun(plural=True) == "instructions"


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
    assert _discovery_phase(app) is app.phases.member(first_key)


def test_frontend_labels_and_backend_phases_share_one_enum():
    # The correctness invariant: the phases the driver stamps on TaskInfo (from
    # the backend's core_phases) must be the SAME enum members the frontend's
    # phase_labels are keyed by, or label lookup silently misses.
    from composer.input.files import InMemoryTextFile
    from composer.llm.anthropic import AnthropicRenderer
    from composer.spec.context import SourceCode
    from composer.spec.system_model import SolidityIdentifier

    app = host.build_application("echoprover")
    source = SourceCode(
        content=InMemoryTextFile(
            basename="doc.md", string_contents="doc", renderer=AnthropicRenderer()
        ),
        project_root="/tmp/echo-proj",
        contract_name=SolidityIdentifier("C"),
        relative_path="src/C.sol",
        forbidden_read="",
    )
    backend = app.make_backend(source)
    for slot, member in backend.core_phases.items():
        assert member in app.phases.labels, (slot, member)
    # Section order lists every declared phase's label.
    assert set(app.phases.section_order) == set(app.phases.labels.values())


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
        phase_labels=app.phases.labels,
        section_order=app.phases.section_order,
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


def test_resolve_ecosystem_resolves_soroban():
    from composer.pipeline.ecosystem import SOROBAN
    from composer.rustapp.host import resolve_ecosystem

    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    soroban = desc.model_copy(update={"ecosystem": "soroban"})
    assert resolve_ecosystem(soroban) is SOROBAN


def test_resolve_ecosystem_rejects_unregistered_chain(monkeypatch):
    from composer.rustapp.host import resolve_ecosystem

    desc = AppDescriptor.model_validate_json(echoprover.descriptor())
    monkeypatch.setattr("composer.rustapp.host.ECOSYSTEMS", {})
    with pytest.raises(ValueError, match="not registered"):
        resolve_ecosystem(desc)


def test_a_descriptor_missing_a_field_is_refused():
    # No wheel is old enough to be excused one — the SDK and the host ship together, so an absent
    # `ecosystem` is a drifted mirror, and guessing "evm" would run the whole front half against the
    # wrong system model and prompts.
    raw = json.loads(echoprover.descriptor())
    del raw["ecosystem"]
    with pytest.raises(ValidationError):
        AppDescriptor.model_validate(raw)
