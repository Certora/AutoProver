"""What a knowledge bundle carries, and what stays separate between two of them.

The documents are a cacheable prefix: the same text on every call, then a cache marker, then the
run's own prompt.
"""

import pytest
from pydantic import ValidationError

from graphcore.graph import CacheMarker

from composer.kb.kb_context import (
    CVL_RECIPES,
    KB_INDEX_TEMPLATE,
    ContextSpec,
    CvlChannel,
    IndexModel,
    KnowledgeBundle,
    RecipeSet,
    context_documents,
    kb_loader,
    with_context,
)
from composer.kb.knowledge_base import kb_tools


def _stub_bundle() -> KnowledgeBundle[CvlChannel]:
    """A second bundle. It reuses CVL's recipes and carries its own label and one spec."""
    return KnowledgeBundle[CvlChannel](
        label="STUB",
        recipes=CVL_RECIPES,
        specs=(ContextSpec(title="Stub Facts", loader=lambda: "nothing to declare"),),
    )


def test_a_bundle_renders_its_own_documents_and_its_own_label():
    docs = context_documents(_stub_bundle())
    assert "Title: Stub Facts" in docs[0]
    index = docs[-1]
    assert "Title: STUB Recipes" in index
    # The preamble names this bundle's manual. A hardcoded CVL would point at the wrong corpus.
    assert "the STUB manual" in index and "the CVL manual" not in index


def test_the_context_is_a_cached_prefix():
    bundle = _stub_bundle()
    docs = context_documents(bundle)
    assert with_context(bundle, "the run") == [*docs, CacheMarker, "the run"]
    assert context_documents(bundle) is docs


def test_a_channel_outside_the_vocabulary_is_rejected():
    """``SKIP`` is not a CVL channel. The legend lists every CVL channel, so the failure is the
    channel itself."""
    legend = {"CVL": "c", "CONF": "c", "EDIT": "e"}
    with pytest.raises(ValidationError) as raised:
        IndexModel[CvlChannel].model_validate(
            {"channels": legend,
             "recipes": [{"id": "R0", "name": "n", "triggers": [], "file": "f.md",
                          "channel": "SKIP"}]}
        )
    (error,) = raised.value.errors()
    assert error["loc"][-2:] == ("channel", 0)


def test_a_recipe_channel_missing_from_the_legend_is_rejected():
    """The agent learns what a channel means from the rendered legend."""
    with pytest.raises(ValidationError, match="does not explain"):
        IndexModel[CvlChannel].model_validate(
            {"channels": {"CVL": "c"},
             "recipes": [{"id": "R0", "name": "n", "triggers": [], "file": "f.md",
                          "channel": "CONF"}]}
        )


def test_prose_in_the_index_file_is_rejected_rather_than_dropped():
    """The agent only sees what the template renders. A preamble in the file would have no reader."""
    with pytest.raises(ValidationError):
        IndexModel[CvlChannel].model_validate(
            {"title": "t", "channels": {"CVL": "c"}, "recipes": []}
        )


def test_the_recipe_tool_name_comes_from_the_recipe_set():
    """The tool name comes from the recipe set. The description is filled from the bundle label."""
    (tool,) = kb_tools(_stub_bundle())
    assert tool.name == CVL_RECIPES.tool_name
    assert tool.description is not None and "STUB recipe" in tool.description


def test_an_unknown_recipe_id_is_a_miss():
    assert kb_loader(CVL_RECIPES)("no-such-recipe") is None


def test_a_recipe_file_that_is_not_there_is_a_miss():
    """The packaged index points only at files that exist, so this index names one that does not.
    The loader reports a miss when the agent asks."""
    recipes = RecipeSet[CvlChannel](
        tool_name=CVL_RECIPES.tool_name,
        index_resource=CVL_RECIPES.index_resource,
        parse_index=lambda _raw: IndexModel[CvlChannel].model_validate(
            {"channels": {"CVL": "c"},
             "recipes": [{"id": "MISSING", "name": "n", "triggers": [],
                          "file": "recipes/no-such-file.md", "channel": "CVL"}]}
        ),
        index_template=KB_INDEX_TEMPLATE,
    )
    assert kb_loader(recipes)("MISSING") is None


def test_a_bundle_without_recipes_contributes_no_tool_and_no_index():
    """A bundle with no recipes has no tool and no index. An empty tool would miss for every id,
    and the index would offer that tool."""
    facts_only = KnowledgeBundle[CvlChannel](
        label="STUB", specs=(ContextSpec(title="Stub Facts", loader=lambda: "x"),)
    )
    assert kb_tools(facts_only) == []
    docs = context_documents(facts_only)
    assert len(docs) == 1 and "Recipes" not in docs[0]
