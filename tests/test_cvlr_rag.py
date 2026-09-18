"""The `cvlr_kb` search tools' wiring, which is checkable without a database.

The tool names are a contract with the prompt: `cvlr_rag_tools.j2` tells the author which tools to
reach for by name, and an agent is given that text whether or not the tools behind it bound. A
rename on one side leaves the prompt advertising something that does not exist, and the model's
only evidence is a tool call that comes back unknown.
"""

from pathlib import Path

from composer.tools.cvlr_rag import get_tools

_GUIDANCE = Path(__file__).parent.parent / "composer" / "templates" / "cvlr_rag_tools.j2"


def test_get_tools_binds_the_three_search_tools():
    # Binding is lazy, so no database and no embedding model are needed to check the wiring.
    names = {t.name for t in get_tools(object())}  # type: ignore[arg-type]
    assert names == {"cvlr_get_section", "cvlr_manual_search", "cvlr_keyword_search"}


def test_the_prompt_names_the_tools_that_exist():
    guidance = _GUIDANCE.read_text()
    for tool in get_tools(object()):  # type: ignore[arg-type]
        assert tool.name in guidance, f"{_GUIDANCE.name} never mentions {tool.name}"
