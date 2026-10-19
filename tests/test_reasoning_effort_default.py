"""The default thinking budget runs the OpenAI-shaped providers at full effort, so a
run that sets no budget is not quietly cheaper on OpenAI than on Claude."""

from composer.input.types import _DEFAULT_THINKING_TOKENS
from composer.llm.provider import reasoning_effort


def test_default_thinking_budget_is_high_effort() -> None:
    assert reasoning_effort(_DEFAULT_THINKING_TOKENS) == "high"
