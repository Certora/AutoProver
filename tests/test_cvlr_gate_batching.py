"""A gate tool, or the code editor, called in the same turn as a tool that changes what it reads is
refused: the calls in one turn all start from the same state, so it would read the draft from before
the change."""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from composer.spec.cvlr.editor import CHANGES_WHAT_THE_EDITOR_READS
from composer.spec.cvlr.verify import CHANGES_WHAT_IS_CHECKED, VERIFY_TOOL, batched_with_a_change
from composer.spec.source.munge.tool_names import CODE_EDITOR


def _turn(*names: str) -> dict[str, Any]:
    calls = [{"name": n, "args": {}, "id": f"call_{i}"} for i, n in enumerate(names)]
    return {"messages": [HumanMessage("go"), AIMessage(content="", tool_calls=calls)]}


def test_a_commit_in_the_same_turn_is_refused() -> None:
    refusal = batched_with_a_change(_turn("commit_edit", "memory", VERIFY_TOOL), VERIFY_TOOL, CHANGES_WHAT_IS_CHECKED)  # type: ignore[arg-type]
    assert refusal is not None
    assert "`commit_edit`" in refusal and "in a turn of its own" in refusal


def test_every_change_in_the_turn_is_named() -> None:
    refusal = batched_with_a_change(
        _turn("summarize_for_prover", "put_harness", "cargo_check"),  # type: ignore[arg-type]
        "cargo_check",
        CHANGES_WHAT_IS_CHECKED,
    )
    assert refusal is not None
    assert "`put_harness`, `summarize_for_prover`" in refusal


def test_reading_tools_in_the_same_turn_are_allowed() -> None:
    state = _turn("memory", "get_harness", "get_cvlr_recipe", VERIFY_TOOL)
    assert batched_with_a_change(state, VERIFY_TOOL, CHANGES_WHAT_IS_CHECKED) is None  # type: ignore[arg-type]


def test_a_call_alone_is_allowed() -> None:
    assert batched_with_a_change(_turn(VERIFY_TOOL), VERIFY_TOOL, CHANGES_WHAT_IS_CHECKED) is None  # type: ignore[arg-type]


def test_no_turn_to_read_is_allowed() -> None:
    assert batched_with_a_change({"messages": []}, VERIFY_TOOL, CHANGES_WHAT_IS_CHECKED) is None  # type: ignore[arg-type]
    assert batched_with_a_change({"messages": [HumanMessage("go")]}, VERIFY_TOOL, CHANGES_WHAT_IS_CHECKED) is None  # type: ignore[arg-type]


def test_the_editor_is_refused_beside_the_harness_it_would_build_against() -> None:
    state = _turn("put_harness", CODE_EDITOR)
    refusal = batched_with_a_change(state, CODE_EDITOR, CHANGES_WHAT_THE_EDITOR_READS)  # type: ignore[arg-type]
    assert refusal is not None
    assert "`put_harness`" in refusal and f"Call `{CODE_EDITOR}` again" in refusal


def test_the_editor_is_not_refused_beside_a_change_it_does_not_read() -> None:
    state = _turn("record_skip", "expect_rule_failure", "adjust_prover_config", CODE_EDITOR)
    assert batched_with_a_change(state, CODE_EDITOR, CHANGES_WHAT_THE_EDITOR_READS) is None  # type: ignore[arg-type]
