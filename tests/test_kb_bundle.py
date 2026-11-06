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
    """A second bundle over CVL's own resources: enough to tell what a bundle contributes to its
    rendering from what the resources do."""
    return KnowledgeBundle[CvlChannel](
        label="STUB",
        recipes=CVL_RECIPES,
        specs=(ContextSpec(title="Stub Facts", loader=lambda: "nothing to declare"),),
    )


def test_a_bundle_renders_its_own_documents_and_its_own_label():
    """The index is appended after the specs. Its preamble names this bundle's manual."""
    docs = context_documents(_stub_bundle())
    assert "Title: Stub Facts" in docs[0]
    index = docs[-1]
    assert "Title: STUB Recipes" in index
    # A second bundle that still said "CVL" here would send its agents to the wrong corpus.
    assert "the STUB manual" in index and "the CVL manual" not in index


def test_the_context_is_a_cached_prefix():
    """Read once per process, then a cache marker, then the run's own prompt."""
    bundle = _stub_bundle()
    docs = context_documents(bundle)
    assert with_context(bundle, "the run") == [*docs, CacheMarker, "the run"]
    assert context_documents(bundle) is docs


def test_a_channel_outside_the_vocabulary_is_rejected():
    """A channel names a tool the reading agent has. A vocabulary wide enough for two agent
    families would validate a recipe pointing at an action its readers cannot take."""
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
    """The rendered legend is how the agent learns what a channel means, so a recipe may only use a
    channel the legend explains."""
    with pytest.raises(ValidationError, match="does not explain"):
        IndexModel[CvlChannel].model_validate(
            {"channels": {"CVL": "c"},
             "recipes": [{"id": "R0", "name": "n", "triggers": [], "file": "f.md",
                          "channel": "CONF"}]}
        )


def test_prose_in_the_index_file_is_rejected_rather_than_dropped():
    """Only what the template renders reaches the agent, so a preamble in the file would be a
    second copy nobody reads."""
    with pytest.raises(ValidationError):
        IndexModel[CvlChannel].model_validate(
            {"title": "t", "channels": {"CVL": "c"}, "recipes": []}
        )


def test_the_recipe_tool_name_comes_from_the_recipe_set():
    """The description names the bundle. The tool name does not: a stub over CVL's recipes keeps
    the CVL tool, and the description is filled in from the label after the class exists."""
    (tool,) = kb_tools(_stub_bundle())
    assert tool.name == CVL_RECIPES.tool_name
    assert tool.description is not None and "STUB recipe" in tool.description


def test_an_unknown_recipe_id_is_a_miss():
    assert kb_loader(CVL_RECIPES)("no-such-recipe") is None


def test_a_recipe_file_that_is_not_there_is_a_miss():
    """The packaged index has no dangling path, so this substitutes one. A missing file is a miss
    when the agent asks."""
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
    """A family acquires recipes after it has knowledge worth indexing. Until then the tool would
    answer every id with a miss, and the index document would advertise it."""
    facts_only = KnowledgeBundle[CvlChannel](
        label="STUB", specs=(ContextSpec(title="Stub Facts", loader=lambda: "x"),)
    )
    assert kb_tools(facts_only) == []
    docs = context_documents(facts_only)
    assert len(docs) == 1 and "Recipes" not in docs[0]
