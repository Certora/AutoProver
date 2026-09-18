from typing import override

from pydantic import Field

from langchain_core.tools import BaseTool

from graphcore.tools.schemas import WithAsyncImplementation

from composer.ui.tool_display import CommonTools, tool_display_of
from .kb_context import KnowledgeBundle, kb_loader

def kb_tools[C: str](bundle: KnowledgeBundle[C]) -> list[BaseTool]:
    """The bundle's recipe tool, or none at all: a family with no recipes yet contributes no
    tool rather than one that answers every id with a miss."""
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

    # ``as_tool`` hands the class docstring to the model as the tool description, and a class
    # defined inside a function cannot write the bundle's name into its own.
    KBGet.__doc__ = f"Retrieve the contents of a {bundle.label} recipe"
    return [KBGet.as_tool(bundle.recipes.tool_name)]
