"""The default thinking budget runs the OpenAI-shaped providers at full effort, so a
run that sets no budget is not quietly cheaper on OpenAI than on Claude. On Claude
4.6+ the budget's size does not reach the request at all, so raising it costs
nothing there."""

from dataclasses import dataclass
from typing import Any

import pytest

from composer.input.types import _DEFAULT_THINKING_TOKENS
from composer.llm.anthropic import AnthropicModelProvider
from composer.llm.provider import reasoning_effort


def test_default_thinking_budget_is_high_effort() -> None:
    assert reasoning_effort(_DEFAULT_THINKING_TOKENS) == "high"


@dataclass(frozen=True)
class _Options:
    thinking_tokens: int | None
    tokens: int = 128_000
    memory_tool: bool = False
    interleaved_thinking: bool = False


# Two clearly different budgets, independent of the default: the Claude tests are
# about how the provider uses a budget, whatever the default happens to be.
_SMALL_BUDGET = 1024
_LARGE_BUDGET = 65_536


def _claude_request(model: str, thinking_tokens: int) -> dict[str, Any]:
    """The request settings the Claude provider builds; building them sends nothing."""
    chat = AnthropicModelProvider.create(model, _Options(thinking_tokens)).builder_for()
    return chat.model_dump(exclude={"callbacks"})


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5"])
def test_adaptive_claude_request_ignores_the_budget_size(monkeypatch, model: str) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    assert _claude_request(model, _SMALL_BUDGET) == _claude_request(model, _LARGE_BUDGET)


def test_budgeted_claude_request_carries_the_budget(monkeypatch) -> None:
    """Claude 4.5 and older take the number as ``budget_tokens``: the one Claude case
    where the size of the budget matters."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    assert _claude_request("claude-haiku-4-5", _SMALL_BUDGET) != _claude_request(
        "claude-haiku-4-5", _LARGE_BUDGET
    )
