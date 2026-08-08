"""Unit tests for the message history ``analyze_cex_raw`` builds.

Counter-example analysis is a one-shot LLM call made from *inside* the ``verify_spec`` tool, so
it inherits the agent's live history: the trailing ``AIMessage`` is the turn that opened the
currently-executing tools node. The Messages API requires every ``tool_use`` block in that turn
to be followed by a matching ``tool_result``, so the analysis history has to answer *all* of the
turn's tool calls — and the model is free to batch another tool call alongside ``verify_spec``.
"""

from typing import Sequence, cast

import pytest
from langchain_core.language_models.base import LanguageModelInput
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableLambda

from composer.prover.analysis import analyze_cex_raw
from composer.prover.ptypes import RulePath, RuleResult

VERIFY_ID = "toolu_verify"
SIBLING_ID = "toolu_sibling"

CapturedLLM = tuple[Runnable[LanguageModelInput, BaseMessage], list[BaseMessage]]


def _capturing_llm() -> CapturedLLM:
    """An LLM stand-in plus the list its invocation messages land in."""
    seen: list[BaseMessage] = []

    def _record(messages: LanguageModelInput) -> BaseMessage:
        assert isinstance(messages, list)
        seen.extend(cast(list[BaseMessage], messages))
        return AIMessage(content="analysis")

    return RunnableLambda(_record), seen


def _violated() -> RuleResult:
    return RuleResult(path=RulePath(rule="count_nonneg"), cex_dump="<cex/>", status="VIOLATED")


def _history(*tool_call_ids: str) -> list[AnyMessage]:
    """A history ending in one assistant turn that opened ``tool_call_ids`` and nothing more."""
    return [
        HumanMessage(content="write a spec"),
        AIMessage(
            content="running the prover",
            tool_calls=[
                {"name": "tool", "args": {}, "id": call_id} for call_id in tool_call_ids
            ],
        ),
    ]


def _results_by_id(messages: Sequence[BaseMessage]) -> dict[str, str]:
    return {
        msg.tool_call_id: str(msg.content)
        for msg in messages
        if isinstance(msg, ToolMessage)
    }


@pytest.mark.asyncio
async def test_a_batched_sibling_call_gets_a_tool_result() -> None:
    llm, seen = _capturing_llm()
    await analyze_cex_raw(llm, _history(SIBLING_ID, VERIFY_ID), _violated(), VERIFY_ID)

    results = _results_by_id(seen)
    assert set(results) == {VERIFY_ID, SIBLING_ID}
    # The prover's own result carries the counter-example; the sibling's is a stand-in.
    assert "<cex/>" in results[VERIFY_ID]
    assert "<cex/>" not in results[SIBLING_ID]


@pytest.mark.asyncio
async def test_the_lone_verify_spec_call_gets_exactly_one_tool_result() -> None:
    llm, seen = _capturing_llm()
    await analyze_cex_raw(llm, _history(VERIFY_ID), _violated(), VERIFY_ID)

    assert list(_results_by_id(seen)) == [VERIFY_ID]


@pytest.mark.asyncio
async def test_tool_calls_already_answered_are_not_answered_twice() -> None:
    # A sibling that finished first has its result in the history already; a second
    # ``tool_result`` for the same id is as invalid as none at all.
    history = _history(SIBLING_ID, VERIFY_ID)
    history.append(ToolMessage(tool_call_id=SIBLING_ID, content="sibling done"))

    llm, seen = _capturing_llm()
    await analyze_cex_raw(llm, history, _violated(), VERIFY_ID)

    ids = [msg.tool_call_id for msg in seen if isinstance(msg, ToolMessage)]
    assert sorted(ids) == sorted({SIBLING_ID, VERIFY_ID})
    assert _results_by_id(seen)[SIBLING_ID] == "sibling done"


@pytest.mark.asyncio
async def test_the_caller_history_is_left_untouched() -> None:
    history = _history(SIBLING_ID, VERIFY_ID)
    before = list(history)
    llm, _ = _capturing_llm()

    await analyze_cex_raw(llm, history, _violated(), VERIFY_ID)

    assert history == before
