"""A counterexample analysis must answer every tool call of the turn that ran the prover.

The analysis request is the author's history plus the prover's result. When the author called
another tool in the same turn (``memory`` beside ``verify_rules`` is common), that call has no
result in the history yet, and the API rejected the whole request with a 400: "`tool_use` ids were
found without `tool_result` blocks immediately after". Every analysis in a run that did this was
lost.
"""

import pytest
from langchain_anthropic.chat_models import _format_messages
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from composer.prover.analysis import analyze_cex_raw
from composer.prover.ptypes import Counterexample, RulePath, RuleResult, UserAssertion

PROVER_CALL = "toolu_prover"
MEMORY_CALL = "toolu_memory"


class _Capture:
    """Stands in for the model: keeps the request and answers it."""

    def __init__(self) -> None:
        self.request: list[BaseMessage] = []

    async def ainvoke(self, messages: list[BaseMessage], *args, **kwargs) -> AIMessage:
        self.request = messages
        return AIMessage(content="explained")


def _violated() -> RuleResult:
    return RuleResult(
        path=RulePath(rule="rule_x"),
        counterexample=Counterexample(trace="<cex/>", origin=UserAssertion()),
        status="VIOLATED",
    )


def _turn(*call_ids: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[
        {"name": "verify_rules" if i == PROVER_CALL else "memory", "args": {}, "id": i,
         "type": "tool_call"}
        for i in call_ids
    ])


def _answered_by_next_user_turn(request: list[BaseMessage]) -> set[str]:
    """The tool calls the API would see answered: the tool results in the user turn that follows
    the last assistant turn, after the client merges consecutive messages."""
    _, formatted = _format_messages(request)
    last_assistant = max(i for i, f in enumerate(formatted) if f["role"] == "assistant")
    reply = formatted[last_assistant + 1]
    assert reply["role"] == "user"
    return {b["tool_use_id"] for b in reply["content"] if b.get("type") == "tool_result"}


@pytest.mark.asyncio
async def test_a_call_made_beside_the_prover_is_answered_too() -> None:
    llm = _Capture()
    history = [HumanMessage("write rules"), _turn(MEMORY_CALL, PROVER_CALL)]

    assert await analyze_cex_raw(llm, history, _violated(), PROVER_CALL) == "explained"  # type: ignore[arg-type]

    assert _answered_by_next_user_turn(llm.request) == {MEMORY_CALL, PROVER_CALL}


@pytest.mark.asyncio
async def test_a_prover_call_alone_gets_only_its_own_result() -> None:
    llm = _Capture()
    history = [HumanMessage("write rules"), _turn(PROVER_CALL)]

    await analyze_cex_raw(llm, history, _violated(), PROVER_CALL)  # type: ignore[arg-type]

    assert _answered_by_next_user_turn(llm.request) == {PROVER_CALL}
    assert sum(isinstance(m, ToolMessage) for m in llm.request) == 1


@pytest.mark.asyncio
async def test_a_sibling_already_answered_is_not_answered_twice() -> None:
    llm = _Capture()
    history = [
        HumanMessage("write rules"),
        _turn(MEMORY_CALL, PROVER_CALL),
        ToolMessage(tool_call_id=MEMORY_CALL, content="saved"),
    ]

    await analyze_cex_raw(llm, history, _violated(), PROVER_CALL)  # type: ignore[arg-type]

    results = [m.tool_call_id for m in llm.request if isinstance(m, ToolMessage)]
    assert sorted(results) == [MEMORY_CALL, PROVER_CALL]
