"""The CEX analyses of one prover result share the author's history; only the counterexample
differs. ``TrivialFanoutCexHandler`` with a ``cache_marker`` puts a cache breakpoint at the end
of that history and runs the first analysis alone, so the rest read the history from the prompt
cache instead of each writing it again.

These tests pin where the breakpoint goes (and where it must not), that the author's state is
left untouched, and the first-alone ordering.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from composer.prover import core
from composer.prover.core import TrivialFanoutCexHandler, mark_history_end
from composer.prover.ptypes import Counterexample, RulePath, RuleResult

_CC = {"type": "ephemeral", "ttl": "5m"}


def _marker(block):
    assert isinstance(block, dict)
    return {**block, "cache_control": _CC}


def _violated(rule: str) -> RuleResult:
    return RuleResult(
        path=RulePath(rule=rule),
        counterexample=Counterexample(trace=f"<cex {rule}/>"),
        status="VIOLATED",
    )


def _submit_call(*, thinking: bool = False) -> AIMessage:
    content: list = []
    if thinking:
        content.append({"type": "thinking", "thinking": "hmm", "signature": "sig"})
    content.append({"type": "text", "text": "Submitting the buffer."})
    content.append({"type": "tool_use", "id": "toolu_1", "name": "submit_buffer", "input": {"name": "b"}})
    return AIMessage(
        content=content,
        tool_calls=[{"id": "toolu_1", "name": "submit_buffer", "args": {"name": "b"}}],
    )


def _history(*, thinking: bool = False) -> list:
    return [SystemMessage(content="system"), HumanMessage(content="write a spec"), _submit_call(thinking=thinking)]


def _marked_blocks(messages) -> list[dict]:
    return [
        b for m in messages if isinstance(m.content, list)
        for b in m.content if isinstance(b, dict) and "cache_control" in b
    ]


def test_breakpoint_goes_on_the_text_before_the_final_tool_call_and_the_state_is_untouched() -> None:
    history = _history()
    marked = mark_history_end(history, _marker)

    assert marked is not None
    assert marked[-1].content[0] == {"type": "text", "text": "Submitting the buffer.", "cache_control": _CC}
    assert marked[-1].content[1] == history[-1].content[1]
    assert marked[-1].tool_calls == history[-1].tool_calls
    assert marked[:-1] == history[:-1]
    assert _marked_blocks(history) == []


def test_tool_use_and_thinking_blocks_are_skipped_for_earlier_text() -> None:
    no_text = AIMessage(
        content=[
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "submit_buffer", "input": {}},
        ],
        tool_calls=[{"id": "toolu_1", "name": "submit_buffer", "args": {}}],
    )
    marked = mark_history_end([HumanMessage(content="hi"), no_text], _marker)
    assert marked is not None
    assert marked[0].content == [{"type": "text", "text": "hi", "cache_control": _CC}]
    assert marked[1] is no_text

    marked = mark_history_end(_history(thinking=True), _marker)
    assert marked is not None
    assert [b["type"] for b in _marked_blocks(marked)] == ["text"]


def test_no_breakpoint_without_any_text() -> None:
    assert mark_history_end([HumanMessage(content="   ")], _marker) is None
    assert mark_history_end([], _marker) is None


def test_no_breakpoint_when_the_request_would_exceed_the_limit() -> None:
    # Two existing breakpoints + this one + the request-level one = 4: allowed.
    two = [HumanMessage(content=[{"type": "text", "text": f"p{i}", "cache_control": _CC}]) for i in range(2)]
    assert mark_history_end([*two, _submit_call()], _marker) is not None
    # Three existing would make 5.
    three = [HumanMessage(content=[{"type": "text", "text": f"p{i}", "cache_control": _CC}]) for i in range(3)]
    assert mark_history_end([*three, _submit_call()], _marker) is None


def test_the_breakpoint_survives_into_the_anthropic_request(monkeypatch) -> None:
    # Builds the request body only; nothing is sent.
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import ToolMessage

    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    llm = ChatAnthropic(model_name="claude-sonnet-5", model_kwargs={"cache_control": _CC}, max_tokens_to_sample=100)
    marked = mark_history_end(_history(), _marker)
    assert marked is not None

    payload = llm._get_request_payload(
        [*marked, ToolMessage(tool_call_id="toolu_1", content="<cex/>"), HumanMessage(content="analyze")]
    )

    assistant = payload["messages"][-2]["content"]
    assert assistant[0] == {"type": "text", "text": "Submitting the buffer.", "cache_control": _CC}
    assert assistant[1]["type"] == "tool_use"


class _Callbacks:
    async def on_analysis_start(self, rule: RuleResult) -> None:
        pass

    async def on_analysis_complete(self, rule: RuleResult, explanation: str) -> None:
        pass


def _recording_analyzer(events: list[str], seen_histories: list):
    async def fake(llm, messages, rule: RuleResult, tool_call_id: str) -> str:
        events.append(f"start {rule.name}")
        seen_histories.append(messages)
        await asyncio.sleep(0.01)
        events.append(f"end {rule.name}")
        return f"explanation for {rule.name}"
    return fake


@pytest.mark.asyncio
async def test_first_analysis_finishes_before_the_rest_start(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    seen: list = []
    monkeypatch.setattr(core, "analyze_cex_raw", _recording_analyzer(events, seen))
    handler = TrivialFanoutCexHandler(llm=None, state={"messages": _history()}, cache_marker=_marker)  # type: ignore[arg-type]

    report = await handler.analyze([_violated("a"), _violated("b"), _violated("c")], "t", _Callbacks(), tmp_path)  # type: ignore[arg-type]

    assert events[:2] == ["start a", "end a"]
    assert sorted(events[2:4]) == ["start b", "start c"]
    assert all(len(_marked_blocks(h)) == 1 for h in seen)
    for rule in "abc":
        assert f"explanation for {rule}" in report


@pytest.mark.asyncio
async def test_without_a_marker_all_analyses_run_concurrently_on_the_raw_history(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    seen: list = []
    monkeypatch.setattr(core, "analyze_cex_raw", _recording_analyzer(events, seen))
    history = _history()
    handler = TrivialFanoutCexHandler(llm=None, state={"messages": history})  # type: ignore[arg-type]

    await handler.analyze([_violated("a"), _violated("b")], "t", _Callbacks(), tmp_path)  # type: ignore[arg-type]

    assert sorted(events[:2]) == ["start a", "start b"]
    assert all(h is history for h in seen)


@pytest.mark.asyncio
async def test_a_failed_first_analysis_does_not_sink_the_rest(monkeypatch, tmp_path) -> None:
    async def fake(llm, messages, rule: RuleResult, tool_call_id: str) -> str:
        if rule.name == "a":
            raise RuntimeError("api blew up")
        return f"explanation for {rule.name}"

    monkeypatch.setattr(core, "analyze_cex_raw", fake)
    handler = TrivialFanoutCexHandler(llm=None, state={"messages": _history()}, cache_marker=_marker)  # type: ignore[arg-type]

    report = await handler.analyze([_violated("a"), _violated("b")], "t", _Callbacks(), tmp_path)  # type: ignore[arg-type]

    assert "explanation for b" in report
