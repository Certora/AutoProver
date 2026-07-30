from typing import override, TypedDict, cast, TYPE_CHECKING

from pydantic import Field

from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore

from graphcore.tools.schemas import WithAsyncImplementation

from composer.workflow.services import Embeddings

from composer.rag.models import get_model

from composer.ui.tool_display import CommonTools, tool_display_of
from .kb_context import kb_loader, KB_TOOL_NAME

# tell the type checker we always import ST
if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer
else:
    # we're probably in test, in which case just gracefully pretend ST doesn't exist
    try:
        from sentence_transformers import SentenceTransformer #type: ignore
    except ImportError:
        pass

class DefaultEmbedder(Embeddings):
    def __init__(self, model: "SentenceTransformer | None" = None):
        self.model : "SentenceTransformer" = get_model() if not model else model

    @override
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode_document(
            texts
        ).tolist() #type: ignore

    @override
    def embed_query(self, text: str) -> list[float]:
        return self.model.encode_query(
            [text]
        ).tolist()[0] #type: ignore

def kb_tools() -> list[BaseTool]:
    loader = kb_loader()
    @tool_display_of(CommonTools.get_knowledge_base_article)
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