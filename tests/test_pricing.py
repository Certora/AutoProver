"""Model-name lookup in the price table: which entry a model name lands on, and
which context tier a call's size selects."""

import pytest

from composer.llm.pricing import price_per_mtok


@pytest.mark.parametrize("model, expected_input", [
    # A model whose name extends another entry's lands on its own entry.
    ("claude-opus-5-5", 4.00),
    ("claude-opus-5", 5.00),
    ("claude-sonnet-5-5", 2.00),
    ("claude-sonnet-5", 2.00),
    ("claude-fable-5-1", 10.00),
    ("gpt-5.5-pro", 30.00),
    ("gpt-5.5", 5.00),
    ("gpt-5.4-mini", 0.75),
    ("gpt-5.4", 2.50),
    ("gpt-6.1-sol", 2.00),
    ("gpt-6-sol", 2.00),
    # Dated snapshots collapse into their model, case-insensitively.
    ("claude-haiku-4-5-20251001", 1.00),
    ("claude-opus-5-5-20261001", 4.00),
    ("GPT-5.5-2026-04-01", 5.00),
])
def test_model_lands_on_its_entry(model: str, expected_input: float) -> None:
    tier = price_per_mtok(model, 0)
    assert tier is not None
    assert tier.input == expected_input


@pytest.mark.parametrize("model", [
    # Unlisted versions must not borrow a neighbour's price.
    "gpt-5.3",
    "gpt-5.6",
    "gpt-5.6-cyber",
    "gpt-5",
    "claude-opus-6",
    "kimi-k3",
    # A suffix other than a date names a different model, not a snapshot.
    "claude-opus-5-6",
    "gpt-5.5-preview",
    "gpt-5.4-mini-2026",
])
def test_unlisted_model_is_unpriced(model: str) -> None:
    assert price_per_mtok(model, 0) is None


def test_table_order_does_not_matter() -> None:
    """No entry matches another entry's name, so lookup cannot depend on which
    of two related entries the table lists first."""
    from composer.llm.pricing import _PRICING, _matches
    names = [name for name, _ in _PRICING]
    for name in names:
        assert [entry for entry in names if _matches(name, entry)] == [name]


def test_none_model_is_unpriced() -> None:
    assert price_per_mtok(None, 0) is None


def test_openai_long_tier_starts_above_272k() -> None:
    assert price_per_mtok("gpt-5.5", 272_000).input == 5.00  # type: ignore[union-attr]
    assert price_per_mtok("gpt-5.5", 272_001).input == 10.00  # type: ignore[union-attr]


def test_haiku_5_5_long_tier_starts_above_100k() -> None:
    assert price_per_mtok("claude-haiku-5-5", 100_000).input == 0.10  # type: ignore[union-attr]
    assert price_per_mtok("claude-haiku-5-5", 100_001).input == 0.50  # type: ignore[union-attr]


def test_model_without_long_tier_ignores_call_size() -> None:
    assert price_per_mtok("claude-opus-5", 900_000).input == 5.00  # type: ignore[union-attr]
