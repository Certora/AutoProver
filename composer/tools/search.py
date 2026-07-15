from graphcore.graph import WithToolCallId
from pydantic import BaseModel, Field
from typing import Annotated, Protocol, ClassVar, Any, Callable, overload

from langchain_core.tools import tool, InjectedToolCallId, BaseTool
from langgraph.runtime import get_runtime
from composer.rag.db import ComposerRAGDB
from composer.rag.types import ManualRef
from dataclasses import Field as DField
from composer.ui.tool_display import tool_display_of, CommonTools

class RAGDBContext(Protocol):
    __dataclass_fields__: ClassVar[dict[str, DField[Any]]]

    @property
    def rag_db(self) -> ComposerRAGDB:
        ...

class CVLManualSearchSchema(WithToolCallId):
    """
    Search the CVL manual database for information relevant to a question about CVL.

    This tool uses semantic similarity search to find the most relevant documentation
    sections from the CVL manual that can help answer questions about CVL syntax,
    semantics, and best practices.

    The result is a list of quotes from the manual, identified with the name of the relevant section.

    Your question MUST be a single, self-contained question. Do not ask multiple questions in a single tool invocation.
    """
    question: str = Field(description="A single, self-contained question about CVL. Avoid open-ended 'how do I...?' questions in favor of 'What is the syntax for ...?' style questions.")
    similarity_cutoff: float = Field(default=0.5, description="Minimum cosine similarity threshold for results (default: 0.7)")
    max_results: int = Field(default=10, description="Maximum number of search results to return (default: 10)")
    manual_section: list[str] = \
        Field(default=[], description="A list of manual sections to search. "
              "If specified, at least one section heading must match at least one of the values provided here")

def _format_results(refs: list[ManualRef]) -> str:
    """Render CVL search hits as a plain pretty-printed string."""
    if not refs:
        return "No matching sections found."
    blocks: list[str] = []
    for t in refs:
        title = " / ".join(t.headers)
        blocks.append(
            f"## {title}\n\n{t.content}\n\n— similarity: {t.similarity:.4f}"
        )
    return "\n\n---\n\n".join(blocks)


def _cvl_manual_search_factory(
    db_provider: Callable[[], ComposerRAGDB],
) -> BaseTool:
    @tool_display_of(CommonTools.cvl_manual)
    @tool("cvl_manual_search", args_schema=CVLManualSearchSchema)
    async def _cvl_manual_search(
        question: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        similarity_cutoff: float = 0.5,
        max_results: int = 10,
        manual_section: list[str] = []
    ) -> str:
        """Search the CVL manual database for relevant documentation."""
        rag_db = db_provider()

        try:
            refs = await rag_db.find_refs(
                query=question,
                similarity_cutoff=similarity_cutoff,
                top_k=max_results,
                manual_section=manual_section,
            )
            return _format_results(refs)
        except Exception as e:
            return f"Failed to search CVL manual: {str(e)}"
    return _cvl_manual_search

class CVLKeywordSearchSchema(BaseModel):
    """
    Search the CVL manual for sections matching keywords using full-text search.

    Returns the headers of matching sections ranked by relevance. Use get_cvl_manual_section
    to retrieve the full content of a section returned by this tool.
    """
    query: str = Field(description=(
        "A websearch-style query string. Unquoted terms are combined with AND. "
        "Use 'OR' between terms for alternatives, quotes for exact phrases, "
        "and '-' to exclude terms. Example: '\"ghost variable\" OR storage -mapping'"
    ))
    min_depth: int = Field(default=0, description="Minimum section depth (0-6). Only return sections where at least this many header levels (h1..hN) are present. 0 means no filtering.")
    limit: int = Field(default=10, description="Maximum number of results to return.")

class CVLGetSectionSchema(BaseModel):
    """
    Retrieve the full content of a CVL manual section by its exact headers.

    Use cvl_keyword_search to discover section headers first, then use this tool
    to fetch the complete text of a specific section.
    """
    headers: list[str] = Field(description="The section header path, e.g. ['Types', 'Integer Types']. Must match exactly.")

def _cvl_keyword_search_factory(
    db_provider: Callable[[], ComposerRAGDB]
) -> BaseTool:
    @tool_display_of(CommonTools.cvl_keyword_search)
    @tool("cvl_keyword_search", args_schema=CVLKeywordSearchSchema)
    async def _cvl_keyword_search(
        query: str,
        min_depth: int = 0,
        limit: int = 10,
    ) -> str:
        """Search the CVL manual for sections matching keywords."""
        rag_db = db_provider()
        try:
            hits = await rag_db.search_manual_keywords(query, min_depth=min_depth, limit=limit)
            if not hits:
                return "No matching sections found."
            lines = []
            for h in hits:
                section_path = " > ".join(h.headers)
                lines.append(f"[{h.relevance:.4f}] {section_path}")
            return "\n".join(lines)
        except Exception as e:
            return f"Failed to search CVL manual: {str(e)}"
    return _cvl_keyword_search

def _cvl_get_section_factory(
    db_provider: Callable[[], ComposerRAGDB]
) -> BaseTool:
    @tool_display_of(CommonTools.get_cvl_manual_section)
    @tool("get_cvl_manual_section", args_schema=CVLGetSectionSchema)
    async def _get_cvl_manual_section(
        headers: list[str],
    ) -> str:
        """Retrieve the full content of a CVL manual section by its headers."""
        rag_db = db_provider()
        try:
            content = await rag_db.get_manual_section(headers)
            if content is None:
                return f"No section found matching headers: {headers}"
            return content
        except Exception as e:
            return f"Failed to retrieve section: {str(e)}"
    return _get_cvl_manual_section

def _get_provider(ctxt: type[RAGDBContext] | ComposerRAGDB) -> Callable[[], ComposerRAGDB]:
    if isinstance(ctxt, ComposerRAGDB):
        return lambda: ctxt
    else:
        return lambda: get_runtime(ctxt).context.rag_db

def cvl_manual_search(
    ctxt: type[RAGDBContext] | ComposerRAGDB,
) -> BaseTool:
    return _cvl_manual_search_factory(_get_provider(ctxt))

def cvl_manual_tools(
    ctxt: type[RAGDBContext] | ComposerRAGDB,
) -> list[BaseTool]:
    db_provider = _get_provider(ctxt)
    return [
        _cvl_manual_search_factory(db_provider),
        _cvl_keyword_search_factory(db_provider),
        _cvl_get_section_factory(db_provider),
    ]
