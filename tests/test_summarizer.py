"""Unit tests for composer.spec.source.summarizer input construction.

Regression guard: a contract with no user-defined types must not cause an empty
text content block to be sent to the LLM (Anthropic rejects those with
"messages: text content blocks must be non-empty").
"""

from composer.spec.source.summarizer import _format_types, _types_input


def test_format_types_empty():
    assert _format_types([]) == ""


def test_types_input_empty_is_omitted():
    # The bug: an empty udts must yield no input element, never [""].
    assert _types_input("") == []


def test_types_input_nonempty_keeps_preamble():
    assert _types_input("A struct Foo at the top level: use `Foo`") == [
        "The following types are available for use in your spec",
        "A struct Foo at the top level: use `Foo`",
    ]
