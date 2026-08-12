from typing import override

from pydantic import Field

from langchain_core.tools import BaseTool

from graphcore.tools.schemas import WithAsyncImplementation

from composer.ui.tool_display import CommonTools, tool_display_of
from .kb_context import kb_loader, KB_TOOL_NAME

def kb_tools() -> list[BaseTool]:
    loader = kb_loader()
    @tool_display_of(CommonTools.get_cvl_recipe)
    class KBGet(WithAsyncImplementation[str]):
        """
        Retrieve the contents of a CVL recipe
        """
        id: str = Field(description="The retrieval ID of the recipe")

        @override
        async def run(self) -> str:
            res = loader(self.id)
            if res is None:
                return f"Recipe ID {self.id} not found"
            return res
    return [KBGet.as_tool(KB_TOOL_NAME)]