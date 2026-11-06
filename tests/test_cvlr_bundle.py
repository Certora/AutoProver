"""The CVLR knowledge bundle.

One facts document for every CVLR agent, and a recipe index for the agents that can retrieve a
recipe. The index names the author's tools, so a reader without that tool gets the facts alone.
"""

from composer.kb.kb_context import (
    CVLR_BUNDLE,
    CVLR_FACTS_BUNDLE,
    CVLR_RECIPES,
    _kb_model,
    context_documents,
    kb_loader,
)
from composer.kb.knowledge_base import kb_tools
from composer.ui.tool_display import CommonTools


def test_shipped_text_has_no_template_syntax():
    """Resource files are not rendered. A jinja delimiter in one is handed to the model verbatim."""
    texts = [spec.loader() for spec in CVLR_FACTS_BUNDLE.specs]
    loader = kb_loader(CVLR_RECIPES)
    for recipe in _kb_model(CVLR_RECIPES).recipes:
        body = loader(recipe.id)
        assert body is not None, recipe.id
        texts.append(body)
    for text in texts:
        assert "{{" not in text and "{%" not in text


def test_the_index_says_what_each_channel_means():
    """A channel tells a stuck agent whether the fix is in its action space. The legend has to be
    in the rendered index, where the agent reads it."""
    index = context_documents(CVLR_BUNDLE)[-1]
    for name, meaning in _kb_model(CVLR_RECIPES).channels.items():
        assert f"`{name}`" in index
        assert meaning in index


def test_every_recipe_resolves():
    """A dangling path is a miss when the agent asks, and nothing earlier reports it."""
    loader = kb_loader(CVLR_RECIPES)
    for recipe in _kb_model(CVLR_RECIPES).recipes:
        assert loader(recipe.id) is not None, recipe.id


def test_the_facts_bundle_is_the_same_facts_without_the_index():
    """For the readers with no `get_cvlr_recipe`. The index tells its reader to retrieve a file and
    names the author's tools in its channels, so it is only for an agent that can act on it; the
    facts are the same document either way, so the two cannot drift."""
    assert context_documents(CVLR_FACTS_BUNDLE) == context_documents(CVLR_BUNDLE)[:1]
    assert "get_cvlr_recipe" not in "".join(context_documents(CVLR_FACTS_BUNDLE))


def test_the_index_names_the_recipe_tool():
    """The display table is indexed by tool name when the tool is built. The index tells the agent
    to call that same name."""
    (tool,) = kb_tools(CVLR_BUNDLE)
    assert tool.name in CommonTools.kb_displays()
    assert tool.name in context_documents(CVLR_BUNDLE)[-1]
