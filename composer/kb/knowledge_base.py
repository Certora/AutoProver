from typing import override

from pydantic import Field

from langchain_core.tools import BaseTool

from graphcore.tools.schemas import WithAsyncImplementation

from composer.ui.tool_display import CommonTools, tool_display_of
from .kb_context import KnowledgeBundle, kb_loader

def kb_tools[C: str](bundle: KnowledgeBundle[C]) -> list[BaseTool]:
    """The bundle's recipe tool, or an empty list when the bundle has no recipes.

    A tool over an empty index would miss for every id.
    """
    if bundle.recipes is None:
        return []
    loader = kb_loader(bundle.recipes)
    @tool_display_of(CommonTools.kb_displays()[bundle.recipes.tool_name])
    class KBGet(WithAsyncImplementation[str]):
        id: str = Field(description="The retrieval ID of the recipe")

        @override
        async def run(self) -> str:
            res = loader(self.id)
            if res is None:
                return f"Recipe ID {self.id} not found"
            return res

    # A literal docstring cannot include the bundle label. ``as_tool`` copies ``__doc__`` into
    # the tool description, so set it before that call.
    KBGet.__doc__ = f"Retrieve the contents of a {bundle.label} recipe"
    return [KBGet.as_tool(bundle.recipes.tool_name)]
