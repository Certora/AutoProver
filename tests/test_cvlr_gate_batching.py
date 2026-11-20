"""A gate tool called in the same turn as a tool that changes what it checks is refused: the calls
in one turn all start from the same state, so it would check the draft from before the change."""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from composer.spec.cvlr.verify import VERIFY_TOOL, batched_with_a_change


def _turn(*names: str) -> dict[str, Any]:
    calls = [{"name": n, "args": {}, "id": f"call_{i}"} for i, n in enumerate(names)]
    return {"messages": [HumanMessage("go"), AIMessage(content="", tool_calls=calls)]}


def test_a_commit_in_the_same_turn_is_refused() -> None:
    refusal = batched_with_a_change(_turn("commit_edit", "memory", VERIFY_TOOL), VERIFY_TOOL)  # type: ignore[arg-type]
    assert refusal is not None
    assert "`commit_edit`" in refusal and "in a turn of its own" in refusal


def test_every_change_in_the_turn_is_named() -> None:
    refusal = batched_with_a_change(
        _turn("summarize_for_prover", "put_harness", "cargo_check"), "cargo_check"  # type: ignore[arg-type]
    )
    assert refusal is not None
    assert "`put_harness`, `summarize_for_prover`" in refusal


def test_reading_tools_in_the_same_turn_are_allowed() -> None:
    state = _turn("memory", "get_harness", "get_cvlr_recipe", VERIFY_TOOL)
    assert batched_with_a_change(state, VERIFY_TOOL) is None  # type: ignore[arg-type]


def test_a_call_alone_is_allowed() -> None:
    assert batched_with_a_change(_turn(VERIFY_TOOL), VERIFY_TOOL) is None  # type: ignore[arg-type]


def test_no_turn_to_read_is_allowed() -> None:
    assert batched_with_a_change({"messages": []}, VERIFY_TOOL) is None  # type: ignore[arg-type]
    assert batched_with_a_change({"messages": [HumanMessage("go")]}, VERIFY_TOOL) is None  # type: ignore[arg-type]
