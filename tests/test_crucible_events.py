"""Unit tests for the Crucible backend's pure callouts (no toolchain / LLM).

The Rust wheel is now a passive service (docs/rust-applications.md): these exercise the pure
callouts (`units` / `author_prompt` / `judge` / `judge_instruction` / `target_for` / `validate`)
directly.
"""

import json

import pytest

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
        unit={"name": "vault", "program": "vault"},
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
        assert crucible_app.target_for(component, name) == "c_invariants"


def test_setup_groups_no_checks():
    # The shared fixture formalizes nothing, so it has no check to place.
    assert crucible_app.target_for(_setup_input(), "c_solvency") is None


def test_component_author_prompt_asks_for_one_invariant_fn_covering_all_props():
    prompt = json.loads(crucible_app.author_prompt(_component_input("solvency", "conservation")))
    assert prompt.get("system") is None
    ins = prompt["instruction"]
    # One `invariants` fn asserting ALL listed properties. The name is constant across components —
    # the per-component `c_<slug>` belongs to the wheel-generated entry and never reaches the model.
    assert "named EXACTLY `invariants`" in ins
    assert "c_invariants" not in ins
    assert "p solvency" in ins and "p conservation" in ins


def test_setup_author_prompt_asks_for_a_fixture():
    prompt = json.loads(crucible_app.author_prompt(_setup_input()))
    assert "FIXTURE" in prompt["instruction"]


def test_component_judge_reviews_the_suite():
    spec = "#[invariant_test]\nfn c_invariants(fixture: &mut Fixture) {}"
    component = _component_input("solvency")
    # The reviewer is declared once for the session, without a draft: a persona, no instruction.
    raw = crucible_app.judge(component)
    assert raw is not None
    assert "Solana security engineer" in json.loads(raw)["system"]
    # The per-round instruction is the criteria-based task: the properties under review, the draft,
    # and what makes the suite unacceptable.
    ins = crucible_app.judge_instruction(component, spec)
    assert "p solvency" in ins and "c_invariants" in ins
    assert "Criterion 3 — Reachability" in ins
    assert "Reject the suite if" in ins
    assert spec in ins
    # …and NOT how the verdict comes back. That is the host's protocol, appended to the reviewer's
    # system prompt (`session._JUDGE_PROTOCOL`) because the host is what reads the verdict. The
    # wheel answering that half is how this drifted: it kept asking for a `{"accept": …}` final
    # message long after the host had moved to the `result` tool, so nothing read what it asked for.
    assert '{"accept"' not in ins and "FINAL message" not in ins


def test_setup_has_no_judge():
    # The shared fixture is scaffolding, not test evidence — nothing to judge.
    assert crucible_app.judge(_setup_input()) is None


def test_the_publish_gate_blocks_until_this_exact_draft_was_accepted():
    # A reviewer's acceptance is a stamp over the buffer it saw, so it goes stale the moment the
    # author edits — which is what stops a spec being published on the strength of a review of a
    # different draft.
    from composer.authoring.state import check_completion, spec_digest
    from composer.rustapp.session import FEEDBACK_KEY

    def state(spec: str, stamped_for: str | None) -> dict:
        return {
            "curr_spec": spec,
            "skipped": [],
            "required_validations": [FEEDBACK_KEY],
            "validations": {} if stamped_for is None else {
                FEEDBACK_KEY: spec_digest(stamped_for, []),
            },
        }

    assert check_completion(state("spec-A", "spec-A")) is None
    assert check_completion(state("spec-B", "spec-A")) is not None
    assert check_completion(state("spec-A", None)) is not None


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


def test_validate_returns_per_check_verdicts_and_the_host_records_them():
    # The backend owns attribution: `validate` returns a verdict PER check the target covers
    # (the host does no verdict logic). Here we exercise the FFI contract shape + fetch_verdicts.
    import asyncio
    from pathlib import Path

    from composer.pipeline.core import Delivered
    from composer.rustapp.adapter import RustFormalizer
    from composer.rustapp.descriptor import AppDescriptor
    from composer.rustapp.result import RustFormalResult
    from composer.spec.source.report.schema import Outcome

    # A parse error still yields the per-check `verdicts` shape the host consumes — keyed by the
    # checks the target it was handed covers, which is the one part of the payload that did parse.
    target = json.dumps(
        {"name": "c_invariants",
         "checks": [{"name": "c_invariants", "properties": ["p"], "target": None}],
         "exploration": "to_budget"}
    )
    out = json.loads(crucible_app.validate("not json", "spec", target, "/tmp", "{}"))
    assert out["kind"] == "verdicts"
    assert out["verdicts"][0][0] == "c_invariants"
    assert out["verdicts"][0][1]["outcome"] == "ERROR"

    # And the host records whatever per-unit verdicts the backend produced (one BAD, one GOOD).
    fz = RustFormalizer(crucible_app, AppDescriptor.model_validate_json(crucible_app.descriptor()))
    res = RustFormalResult(
        units=[("solvency", ["c_solvency"]), ("conservation", ["c_conservation"])],
        verdicts={
            "c_conservation": wire_verdict("BAD", detail="crash abc: [conservation] drift"),
            "c_solvency": wire_verdict("GOOD"),
        },
    )
    verdicts = asyncio.run(fz.fetch_verdicts(
        Delivered(res, Path("certora/crucible/fuzz/vault/src/main.rs"))
    ))
    assert verdicts["c_conservation"].outcome == Outcome.BAD
    assert "conservation" in verdicts["c_conservation"].message
    assert verdicts["c_solvency"].outcome == Outcome.GOOD
