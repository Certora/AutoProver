"""A knowledge bundle is a record, and a second one is an instance of it.

`composer/kb/kb_context.py` held one agent family's knowledge as module constants. These tests are
the executable spec for adding another: what a bundle has to carry, and what stays separate between
two of them. The one thing worth stating outright is the constraint that makes the bundle cheap —
its documents are read once per process and sit behind a cache marker, so nothing in one may vary
per run.
"""

from importlib.resources import files

import pytest
from pydantic import ValidationError

from composer.kb.kb_context import (
    CVL_BUNDLE,
    CVL_RECIPES,
    ContextSpec,
    CvlChannel,
    IndexModel,
    KnowledgeBundle,
    _kb_model,
    context_documents,
    kb_loader,
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
    docs = context_documents(_stub_bundle())
    assert [d for d in docs if "Title: Stub Facts" in d], "the bundle's own spec is missing"
    index = next(d for d in docs if "Title: STUB Recipes" in d)
    # The preamble points at a manual by name; a second bundle inheriting "CVL" there would send
    # its agents to the wrong corpus.
    assert "the STUB manual" in index and "the CVL manual" not in index


def test_the_recipe_index_is_last():
    """It is appended by the builder rather than listed among the specs, because every bundle has
    exactly one and it is rendered from the bundle rather than read from a file."""
    assert "Recipes" in context_documents(_stub_bundle())[-1]


def test_two_bundles_do_not_share_their_loaded_index():
    cvl, stub = context_documents(CVL_BUNDLE), context_documents(_stub_bundle())
    assert len(cvl) == 4 and len(stub) == 2
    assert "CVL Baseline Knowledge" not in stub[0]


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


def test_the_recipe_tool_takes_its_name_and_description_from_the_bundle():
    (tool,) = kb_tools(_stub_bundle())
    assert tool.name == "get_cvl_recipe"
    assert tool.description is not None and "STUB recipe" in tool.description


def test_the_loader_resolves_an_id_to_its_file_and_nothing_else():
    loader = kb_loader(CVL_RECIPES)
    assert loader("no-such-recipe") is None
    some = _kb_model(CVL_RECIPES).recipes[0]
    assert loader(some.id) == (files("composer.kb") / "resources" / some.file).read_text()


def test_a_bundle_without_recipes_contributes_no_tool_and_no_index():
    """A family acquires recipes after it has knowledge worth indexing. Until then the tool would
    answer every id with a miss, and the index document would advertise it."""
    facts_only = KnowledgeBundle[CvlChannel](
        label="STUB", specs=(ContextSpec(title="Stub Facts", loader=lambda: "x"),)
    )
    assert kb_tools(facts_only) == []
    docs = context_documents(facts_only)
    assert len(docs) == 1 and "Recipes" not in docs[0]
