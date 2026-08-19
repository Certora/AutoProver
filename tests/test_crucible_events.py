"""Unit tests for the Crucible backend's pure callouts (no toolchain / LLM).

The Rust wheel is now a passive service (docs/rust-applications.md): these exercise the pure
callouts (`units` / `author_prompt` / `judge` / `judge_instruction` / `target_for` / `validate`)
directly.
"""

import json

import pytest

from composer.rustapp.wire import CalloutFailed, parse_validate
from tests.conftest import wire_verdict

crucible_app = pytest.importorskip(
    "crucible_app",
    reason="crucible_app wheel not built (uv run maturin develop -m rust/crucible-app/Cargo.toml)",
)


def _author_input(**payload) -> str:
    """One `AuthorInput` with every field the wire requires, plus the kind's own payload.

    Spelled in full because absence is an error on this seam: `AuthorInput` is the one wire type that
    cannot carry `deny_unknown_fields` (serde rejects it beside the `flatten` its kind needs), so a
    stale key here would be *ignored* rather than reported — which is exactly how a fixture ends up
    asserting less than it looks like it does.
    """
    return json.dumps({
        "program": "vault",
        "source_unit": {},
        "prep_facts": {},
        "props": [],
        "run_props": [],
        "setup": None,
        "args": {"fuzz_timeout": 5},
        **payload,
    })


def _component_input(*slugs: str) -> str:
    return _author_input(
        kind="component",
        unit={"name": "vault", "program": "vault", "slug": "vault"},
        props=[
            {"component": "vault", "title": f"p {s}", "sort": "invariant", "description": "d", "slug": s}
            for s in slugs
        ],
        setup="struct Fixture {}",
    )


def _setup_input() -> str:
    # ``units`` is the run's unit set, which a setup turn carries so the wheel's gate can build the
    # deliverable's crate root rather than a provisional one.
    return _author_input(kind="setup", model={"programs": []}, units=[])


def test_descriptor_declares_design_doc_discovery_phase():
    from composer.rustapp.entry import _discovery_phase
    from composer.rustapp.host import build_application

    app = build_application("crucible_app")
    assert app.phases.section_order[0] == "Design Doc Discovery"
    assert _discovery_phase(app) is app.phases.member("discover_design_doc")


def test_every_check_of_a_component_shares_one_fuzz_target():
    # Collapse (docs/crucible.md §8): whatever the author named its checks, they
    # all share ONE fuzz `target` (`c_invariants`), so the host runs a single build + fuzz and the
    # wheel attributes the outcome per property.
    component = _component_input("solvency", "conservation")
    for name in ("c_solvency", "whatever_the_author_called_it"):
        assert crucible_app.target_for(component, name) == "c_vault"


def test_setup_groups_no_checks():
    # The shared fixture formalizes nothing, so it has no check to place.
    assert crucible_app.target_for(_setup_input(), "c_solvency") is None


def test_component_author_prompt_asks_for_one_invariant_fn_covering_all_props():
    prompt = json.loads(crucible_app.author_prompt(_component_input("solvency", "conservation")))
    ins = prompt["instruction"]
    # One `invariants` fn asserting ALL listed properties. The name is constant across components —
    # the per-component `c_<slug>` belongs to the wheel-generated entry and never reaches the model.
    assert "named EXACTLY `invariants`" in ins
    assert "c_invariants" not in ins
    assert "p solvency" in ins and "p conservation" in ins


def test_component_judge_reviews_the_suite():
    spec = "#[invariant_test]\nfn c_invariants(fixture: &mut Fixture) {}"
    component = _component_input("solvency")
    assert crucible_app.judge(component) is not None
    ins = crucible_app.judge_instruction(component, spec)
    assert "p solvency" in ins and "c_invariants" in ins
    assert spec in ins
    # …and NOT how the verdict comes back. That is the host's protocol, appended to the reviewer's
    # system prompt (`session._JUDGE_PROTOCOL`) because the host is what reads the verdict. The
    # wheel answering that half is how this drifted: it kept asking for a `{"accept": …}` final
    # message long after the host had moved to the `result` tool, so nothing read what it asked for.
    assert '{"accept"' not in ins and "FINAL message" not in ins


def test_setup_has_no_judge():
    # The shared fixture is scaffolding, not test evidence — nothing to judge.
    assert crucible_app.judge(_setup_input()) is None


def test_fetch_verdicts_threads_finding_detail_into_message():
    # A BAD verdict's `detail` (the fuzzer's counterexample / assertion message, captured by
    # `validate`) must reach the report `Verdict.message` so a bare BAD explains itself.
    import asyncio
    from pathlib import Path

    from composer.pipeline.core import Delivered
    from composer.rustapp.adapter import RustFormalizer
    from composer.rustapp.descriptor import AppDescriptor
    from composer.rustapp.result import RustFormalResult
    from composer.spec.source.report.schema import Outcome

    desc = AppDescriptor.model_validate_json(crucible_app.descriptor())
    fz = RustFormalizer(crucible_app, desc)
    res = RustFormalResult(
        verdicts={
            "c_x": wire_verdict("BAD", detail="crash abc: deposit(5) — expected 105 got 100"),
            "c_y": wire_verdict("GOOD"),
        }
    )
    verdicts = asyncio.run(fz.fetch_verdicts(
        Delivered(res, Path("certora/crucible/fuzz/vault/src/main.rs"))
    ))
    assert verdicts["c_x"].outcome == Outcome.BAD
    assert verdicts["c_x"].message == "crash abc: deposit(5) — expected 105 got 100"
    # GOOD verdict with no detail carries no message.
    assert verdicts["c_y"].message is None


def test_validate_reports_a_parse_error_as_the_error_envelope():
    # A payload that will not parse is a host bug, and the wheel says so: `{"kind":"error"}`,
    # which `parse_validate` raises as `CalloutFailed`. It is deliberately NOT a verdict set —
    # an ERROR per covered check is a claim about the user's program, so fabricating one here
    # would hide a broken seam behind a finding the run never made.
    target = json.dumps(
        {"name": "c_invariants",
         "checks": [{"name": "c_invariants", "properties": ["p"], "target": None}],
         "exploration": "to_budget"}
    )
    raw = crucible_app.validate("not json", "spec", target, "/tmp", "{}")
    assert json.loads(raw)["kind"] == "error"
    with pytest.raises(CalloutFailed, match="AuthorInput"):
        parse_validate(raw)


def test_the_wheel_declares_what_its_findings_rest_on():
    """A findings write-up is prose about what the evidence means, and only the wheel knows.

    The write-up model rates exploitability from whatever it is handed, so the prose is what keeps
    that rating honest: a campaign shows a tagged assertion can be made to fail against a fixture
    the author wrote, and a crash on a broken precondition looks exactly like one on a real path.
    Above all `SUSPECT HARNESS BUG`, where the likely defect is the harness rather than the program.
    """
    from composer.rustapp.descriptor import AppDescriptor

    desc = AppDescriptor.model_validate_json(crucible_app.descriptor())
    declared = desc.findings
    assert declared is not None, "a wheel that declares nothing writes no findings at all"
    assert "SUSPECT HARNESS BUG" in declared.domain
    # The declaration/reproduction split is the one distinction this evidence cannot make on the
    # outcome alone, so the prompt has to make it.
    assert "DECLARED" in declared.domain
