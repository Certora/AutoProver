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
    """The agent reads the legend from the rendered index."""
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
    """An agent that cannot call ``get_cvlr_recipe`` gets the facts only. Both bundles load the
    same facts document."""
    assert context_documents(CVLR_FACTS_BUNDLE) == context_documents(CVLR_BUNDLE)[:1]
    assert "get_cvlr_recipe" not in "".join(context_documents(CVLR_FACTS_BUNDLE))


def test_the_index_names_the_recipe_tool():
    """Building the tool looks its name up in the display table. The index tells the agent to call
    that name."""
    (tool,) = kb_tools(CVLR_BUNDLE)
    assert tool.name in CommonTools.kb_displays()
    assert tool.name in context_documents(CVLR_BUNDLE)[-1]
