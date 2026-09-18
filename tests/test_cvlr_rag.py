"""The search tools bind, and `cvlr_rag_tools.j2` names tools that exist.

The template is shown to the model even if the tools did not bind, so a rename
on one side only shows up as a tool call coming back unknown.
"""

from pathlib import Path

from composer.tools.cvlr_rag import get_tools

_GUIDANCE = Path(__file__).parent.parent / "composer" / "templates" / "cvlr_rag_tools.j2"


def test_get_tools_binds_the_three_search_tools():
    # bind() does not open the database or load an embedding model.
    names = {t.name for t in get_tools(object())}  # type: ignore[arg-type]
    assert names == {"cvlr_get_section", "cvlr_manual_search", "cvlr_keyword_search"}


def test_the_prompt_names_the_tools_that_exist():
    guidance = _GUIDANCE.read_text()
    for tool in get_tools(object()):  # type: ignore[arg-type]
        assert tool.name in guidance, f"{_GUIDANCE.name} never mentions {tool.name}"
