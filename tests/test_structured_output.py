"""Tests for `DecodesJsonStrings` (composer/llm/structured.py): a structured-output model accepts a
list or object argument that the LLM encoded as a JSON string, at any depth, and leaves every
string-typed field alone."""
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from pydantic import BaseModel, ValidationError

from composer.llm.structured import DecodesJsonStrings
from composer.spec.source.report.grouping import GroupingResult

type Pair = tuple[str, str]


class Leaf(BaseModel):
    name: str
    tags: list[str]


class Root(DecodesJsonStrings):
    note: str
    maybe_note: str | None = None
    leaves: list[Leaf]
    pairs: list[Pair]
    by_name: dict[str, Leaf] = {}
    optional_leaf: Leaf | None = None


_LEAF = {"name": "a", "tags": ["x", "y"]}


def test_top_level_list_arriving_as_a_json_string_is_decoded():
    got = Root.model_validate({"note": "n", "leaves": json.dumps([_LEAF]), "pairs": []})
    assert got.leaves == [Leaf(name="a", tags=["x", "y"])]


def test_nested_fields_and_list_elements_arriving_as_json_strings_are_decoded():
    got = Root.model_validate({
        "note": "n",
        # a list whose element is itself encoded, holding a field that is encoded again
        "leaves": [json.dumps({"name": "a", "tags": json.dumps(["x"])})],
        # a tuple alias inside a list
        "pairs": [json.dumps(["c", "t"])],
        "by_name": json.dumps({"a": _LEAF}),
        "optional_leaf": json.dumps(_LEAF),
    })
    assert got.leaves == [Leaf(name="a", tags=["x"])]
    assert got.pairs == [("c", "t")]
    assert got.by_name == {"a": Leaf(name="a", tags=["x", "y"])}
    assert got.optional_leaf == Leaf(name="a", tags=["x", "y"])


def test_string_fields_keep_json_looking_text_verbatim():
    got = Root.model_validate({
        "note": '["not", "a", "list"]', "maybe_note": "{}", "leaves": [], "pairs": [],
    })
    assert got.note == '["not", "a", "list"]'
    assert got.maybe_note == "{}"


def test_a_string_that_is_not_json_still_fails_validation():
    with pytest.raises(ValidationError):
        Root.model_validate({"note": "n", "leaves": "not json", "pairs": []})


def test_a_json_string_of_the_wrong_shape_still_fails_validation():
    with pytest.raises(ValidationError):
        Root.model_validate({"note": "n", "leaves": json.dumps({"name": "a"}), "pairs": []})


def test_grouping_tool_call_with_json_encoded_groups_parses():
    """The grouping result as langchain parses a forced tool call whose ``groups`` argument (and a
    group's ``members``) came back JSON-encoded."""
    group = {
        "slug": "g", "title": "G", "description": "d",
        "members": json.dumps([["C", "p1"], ["C", "p2"]]),
    }
    msg = AIMessage(content="", tool_calls=[{
        "name": "GroupingResult", "args": {"groups": json.dumps([group])}, "id": "call-1",
    }])

    parsed = PydanticToolsParser(tools=[GroupingResult], first_tool_only=True).invoke(msg)

    assert isinstance(parsed, GroupingResult)
    assert [g.slug for g in parsed.groups] == ["g"]
    assert parsed.groups[0].members == [("C", "p1"), ("C", "p2")]
