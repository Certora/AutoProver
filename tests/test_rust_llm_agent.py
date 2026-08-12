"""The Rust authoring session's prompt handling (no wheel / LLM needed).

Two halves make up an author's system prompt, and the split is the point: the *host* owns the
protocol half — the tools, what the publish gate requires, what a skip and a give-up mean — and the
wheel owns the domain half. A wheel that hand-rolled the protocol could drift from what the host
actually enforces, so it is never asked to.

The wheel's payload is a :class:`composer.rustapp.wire.Prompt`, parsed at the seam, so the shape is
checked where it crosses rather than read key-by-key later: a wheel that sends no ``instruction``
fails here with the field named, rather than prompting the agent with a JSON dump of whatever it
did send.
"""

import json
import pathlib
from typing import Any, cast

import pytest
from pydantic import ValidationError

from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.session import (
    CheckVocab, GateDeps, ProtocolTemplate, PublishDeps, _expect_tools, _feedback_tool,
    _initial_prompt, _map_tool, _publish_tool, _validate_tool, rebuttal_model,
)
from composer.rustapp.wire import Prompt, parse_prompt
from composer.templates.loader import load_jinja_template
from tests.conftest import wire_descriptor, wire_prompt


def _descriptor(**overrides) -> AppDescriptor:
    return AppDescriptor.model_validate(wire_descriptor(**overrides))


def _protocol(*, gate_tool="validate_spec", has_judge=True, has_checks=True) -> str:
    return ProtocolTemplate.bind({
        "gate_tool": gate_tool,
        "has_judge": has_judge,
        "has_checks": has_checks,
        "check_noun": "check",
    }).render_to(load_jinja_template)


def test_instruction_is_taken_as_sent():
    assert parse_prompt(json.dumps(wire_prompt("author X"))).instruction == "author X"


def test_no_system_prompt_declared_means_the_protocol_stands_alone():
    # ``None`` is the wheel saying it has nothing domain-specific to add.
    assert parse_prompt(json.dumps(wire_prompt("author X"))).system is None


def test_backend_may_define_its_own_system_prompt():
    prompt = parse_prompt(json.dumps(wire_prompt("author X", "you are a fuzz author")))
    assert prompt == Prompt(system="you are a fuzz author", instruction="author X")


def test_a_payload_with_no_instruction_is_rejected_at_the_seam():
    with pytest.raises(ValidationError, match="instruction"):
        parse_prompt('{"system": "you are a fuzz author"}')


def test_the_protocol_half_is_backend_agnostic():
    # It describes the session, not a language or a checker: every wheel gets the same text.
    text = _protocol()
    assert "Rust" not in text and "cargo" not in text
    for tool in ("put_spec", "edit_spec", "get_spec", "result", "give_up"):
        assert tool in text


def test_the_protocol_names_the_gate_the_session_actually_bound():
    # The gate differs per session kind; naming the wrong one would send the author looking for a
    # tool that isn't on its belt.
    assert "validate_spec" in _protocol(gate_tool="validate_spec")
    assert "compile_spec" in _protocol(gate_tool="compile_spec", has_checks=False)


def test_a_session_with_no_judge_is_not_told_to_seek_review():
    assert "feedback_tool" not in _protocol(has_judge=False)
    assert "feedback_tool" in _protocol(has_judge=True)


def test_a_setup_session_is_told_nothing_about_skips_or_expected_failures():
    # It formalizes no properties of its own, so it has nothing to skip and no unit to mark.
    setup = _protocol(gate_tool="compile_spec", has_checks=False)
    assert "record_skip" not in setup and "expect_check_failure" not in setup


_GENERIC = CheckVocab("check", "checks")


def test_the_initial_prompt_states_the_obligation_the_gate_will_enforce():
    # The names are the author's to choose, but coverage and honesty about them are enforced the
    # same way for every backend — so the host words that once, rather than trusting each wheel's
    # prose to say it (or to say it compatibly).
    prompt = Prompt(system=None, instruction="Author the harness.")
    text = _initial_prompt(prompt, True, _GENERIC)
    assert "Author the harness." in text
    assert "map_checks" in text
    assert "skipped" in text and "really be in your spec" in text


def test_a_setup_sessions_initial_prompt_is_the_wheels_own():
    # Nothing to declare: a setup spec formalizes no properties, so there is no mapping at all.
    prompt = Prompt(system=None, instruction="Author the fixture.")
    assert _initial_prompt(prompt, False, _GENERIC) == "Author the fixture."


def test_the_obligation_speaks_the_wheels_own_noun():
    # A Crucible author reads about harness functions, not about "checks" — the word is the wheel's
    # (AppDescriptor.check_noun), and it is what its own prompts and generated code already use.
    text = _initial_prompt(
        Prompt(system=None, instruction="Author it."),
        True,
        CheckVocab("harness function", "harness functions"),
    )
    assert "harness functions verify which property" in text
    # The tool NAME is fixed (`map_checks`, like `expect_check_failure`); it is the prose that has
    # to speak the wheel's word, so the generic noun must not appear as a word of its own.
    assert "check " not in text and "checks " not in text


# ---------------------------------------------------------------------------
# The tool surface speaks the wheel's vocabulary too
# ---------------------------------------------------------------------------

def _crucible() -> CheckVocab:
    return CheckVocab("harness function", "harness functions")


def _tool_text(tool) -> str:
    """Everything about a tool the model actually reads: its description and every argument's."""
    schema = tool.tool_call_schema.model_json_schema()
    parts = [tool.description, *(
        p.get("description", "") for p in schema.get("properties", {}).values()
    )]
    parts += [
        p.get("description", "")
        for d in schema.get("$defs", {}).values()
        for p in d.get("properties", {}).values()
    ]
    return "\n".join(parts)


def _gate_deps(vocab: CheckVocab) -> GateDeps:
    return GateDeps(
        module=cast(Any, None), input_json="{}", workdir=pathlib.Path("."),
        sandbox_json="{}", emit=lambda _k, _p: None, vocab=vocab,
    )


def test_the_gate_tool_describes_itself_in_the_wheels_noun():
    vocab = _crucible()
    text = _tool_text(_validate_tool(_gate_deps(vocab), vocab))
    assert "harness function" in text
    # `checks` survives as the *argument* name — the tools keep their generic API so the protocol
    # can name them literally; it is the prose that speaks the wheel's language.
    assert "check " not in text and "checks " not in text


def test_the_expected_failure_tools_and_the_publish_mapping_follow():
    vocab = _crucible()
    fail, passage = _expect_tools(vocab)
    publish = _publish_tool(PublishDeps(titles=[]), vocab)
    for tool in (fail, passage, _map_tool(vocab)):
        assert "harness function" in _tool_text(tool)
    # The publish tool no longer carries the mapping — it is declared before the run, not after —
    # so what it must still say in the wheel's words is what the gate refuses over.
    assert "harness function" in _tool_text(publish) or "mapping" in _tool_text(publish)


def test_a_wheel_that_declares_no_noun_gets_the_generic_one():
    # `check_noun` is optional; a wheel with no better word gets the framework's.
    assert CheckVocab.of(_descriptor(check_noun=None)).one == "check"
    assert CheckVocab.of(_descriptor(check_noun="invariant")).many == "invariants"


def test_a_redescribed_tool_is_not_silently_the_base_one():
    # `@tool_display` rebinds `as_tool`/`bind` closed over the class it decorated, so a subclass of
    # a decorated schema hands back the BASE schema — the re-described text vanishes with no error.
    # The factories apply the display themselves to avoid that; this is the guard.
    generic = _tool_text(_validate_tool(_gate_deps(_GENERIC), _GENERIC))
    crucible = _tool_text(_validate_tool(_gate_deps(_crucible()), _crucible()))
    assert generic != crucible


def test_the_rebuttal_tool_carries_the_wheels_declared_evidence_kinds():
    # Same failure mode, on the tool that was already built by subclassing: if the base schema wins,
    # the judge is offered an untyped rebuttal instead of the wheel's closed set.
    async def _judge(*_a):
        raise AssertionError("not invoked")

    tool = _feedback_tool(_judge, rebuttal_model(["build_failure", "reasoned"]))
    evidence = tool.tool_call_schema.model_json_schema()["$defs"]["Rebuttal"]["properties"]
    assert evidence["evidence_type"]["enum"] == ["build_failure", "reasoned"]
