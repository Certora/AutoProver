"""A failing per-CEX analysis must cost only that counterexample's explanation.

``TrivialFanoutCexHandler.analyze`` fans the violated rules out concurrently. The
fan-out used to be a bare ``asyncio.gather``, so the first analysis to raise
propagated out of ``analyze`` → ``run_prover`` → the pipeline and ended the run —
after the prover had already done its work. A transient API error on one
counterexample was enough to discard hours of verification.

These tests pin the isolation: the surviving rules keep their explanations, the
report still renders, and the summarization threshold is unmoved by whether an
analysis succeeded or blew up.
"""

import asyncio

import pytest

from composer.prover import core
from composer.prover.core import TrivialFanoutCexHandler
from composer.prover.ptypes import RulePath, RuleResult


def _violated(rule: str) -> RuleResult:
    return RuleResult(path=RulePath(rule=rule), cex_dump=f"<cex {rule}/>", status="VIOLATED")


class _RecordingCallbacks:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: dict[str, str] = {}

    async def on_analysis_start(self, rule: RuleResult) -> None:
        self.started.append(rule.name)

    async def on_analysis_complete(self, rule: RuleResult, explanation: str) -> None:
        self.completed[rule.name] = explanation


def handler_for() -> TrivialFanoutCexHandler:
    # ``llm`` is only reached through the patched ``analyze_cex_raw`` (and by
    # ``_report_to_todo_list``, which these cases stay under the threshold of).
    return TrivialFanoutCexHandler(llm=None, state={"messages": []})  # type: ignore[arg-type]


async def _analyze(handler: TrivialFanoutCexHandler, rules: list[RuleResult], tmp_path):
    callbacks = _RecordingCallbacks()
    report = await handler.analyze(rules, "tool-call-1", callbacks, tmp_path)  # type: ignore[arg-type]
    return report, callbacks


@pytest.mark.asyncio
async def test_one_failed_analysis_does_not_sink_the_others(monkeypatch, tmp_path) -> None:
    async def fake_analyze(llm, messages, rule: RuleResult, tool_call_id: str) -> str:
        if rule.name == "boom":
            raise RuntimeError("api blew up")
        return f"explanation for {rule.name}"

    monkeypatch.setattr(core, "analyze_cex_raw", fake_analyze)

    rules = [_violated("first"), _violated("boom"), _violated("second")]
    report, callbacks = await _analyze(handler_for(), rules, tmp_path)

    assert "explanation for first" in report
    assert "explanation for second" in report
    assert set(callbacks.completed) == {"first", "second"}


@pytest.mark.asyncio
async def test_every_analysis_failing_still_renders_a_report(monkeypatch, tmp_path) -> None:
    async def fake_analyze(llm, messages, rule: RuleResult, tool_call_id: str) -> str:
        raise RuntimeError("api blew up")

    monkeypatch.setattr(core, "analyze_cex_raw", fake_analyze)

    rules = [_violated("first"), _violated("second")]
    report, callbacks = await _analyze(handler_for(), rules, tmp_path)

    # The rules' statuses come from the prover, not from their analyses, so the
    # report still has something to say about them.
    assert "first" in report
    assert "second" in report
    assert callbacks.completed == {}


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(monkeypatch, tmp_path) -> None:
    async def fake_analyze(llm, messages, rule: RuleResult, tool_call_id: str) -> str:
        raise asyncio.CancelledError()

    monkeypatch.setattr(core, "analyze_cex_raw", fake_analyze)

    with pytest.raises(asyncio.CancelledError):
        await _analyze(handler_for(), [_violated("first")], tmp_path)
