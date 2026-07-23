"""Search tools over the `crucible_kb` RAG — the Crucible harness-authoring docs.

Mirrors ``composer/tools/foundry_rag.py`` (keyword + vector + get-section over a
``ComposerRAGDB``), bound into the Crucible author's env so the tool-enabled
``call_llm`` (§7.5) can retrieve the harness guide / API reference / writing-tests
material alongside the static cheat-sheet.
"""

import logging
from typing import Iterable

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.rag.db import ComposerRAGDB
from graphcore.tools.schemas import WithAsyncDependencies

_log = logging.getLogger(__name__)

# A documentation-search tool is an *aid*, not a dependency — the author also has the
# static cheat-sheet + worked example. So a backend failure (DB down, or an embedding
# model that chokes on a particular query) must degrade to "no results", never crash
# the authoring turn / fail the component.
_UNAVAILABLE = (
    "(Documentation search is temporarily unavailable; proceed using the cheat-sheet "
    "and worked example already in your prompt.)"
)


def _header_string(s: list[str]) -> str:
    return " > ".join(i for i in s if i)


class CrucibleKeywordSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    """Full-text search of the Crucible docs. Returns matching section titles in
    relevance order; use ``crucible_docs_get_section`` to read one."""

    query: str = Field(description=(
        "A websearch-style query. Unquoted terms are AND-ed; use 'OR' for alternatives, "
        "quotes for exact phrases, '-' to exclude. Example: '\"fuzz_fixture\" setup -coverage'"
    ))
    limit: int = Field(default=10, description="Maximum number of results.")

    async def run(self) -> str:
        try:
            with self.tool_deps() as db:
                res = await db.search_manual_keywords(self.query, limit=self.limit)
        except Exception as e:  # noqa: BLE001 — a search aid must never fail the authoring turn
            _log.warning("crucible_docs_keyword_search failed (%s); returning no results", e)
            return _UNAVAILABLE
        hits = [f"{_header_string(r.headers)} [relevance: {r.relevance:.4f}]" for r in res]
        return "\n".join(hits) if hits else "No results found"


class CrucibleVectorSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    """Semantic search of the Crucible docs for sections matching a natural-language
    question. Returns section title, text, and similarity."""

    query: str = Field(description=(
        "A single natural-language question, e.g. 'how do I write an invariant that reads "
        "on-chain account state?' or 'how are PDA seeds encoded in a harness?'"
    ))
    similarity_cutoff: float = Field(default=0.5, description="Minimum cosine similarity (default 0.5).")
    max_results: int = Field(default=10, description="Maximum results (default 10).")

    async def run(self) -> str:
        try:
            with self.tool_deps() as db:
                res = await db.find_refs(
                    self.query, similarity_cutoff=self.similarity_cutoff, top_k=self.max_results
                )
        except Exception as e:  # noqa: BLE001 — the embedding model can choke on a query; don't crash the turn
            _log.warning("crucible_docs_search failed (%s); returning no results", e)
            return _UNAVAILABLE
        out = [
            f"----\nSection: {_header_string(r.headers)}\n\n{r.content}\nSimilarity: {r.similarity:.4f}"
            for r in res
        ]
        return "\n".join(out) if out else "(No results found)"


class CrucibleSectionGet(WithAsyncDependencies[str, ComposerRAGDB]):
    """Retrieve the full contents of a Crucible docs section by its header path."""

    section_names: list[str] = Field(description=(
        "The section heading path (the searches separate headers with ' > '). E.g. to read "
        "'Writing Solana/Anchor Fuzz Harnesses > PDA Seed Encoding', pass "
        "['Writing Solana/Anchor Fuzz Harnesses', 'PDA Seed Encoding']."
    ))

    async def run(self) -> str:
        try:
            with self.tool_deps() as db:
                content = await db.get_manual_section(self.section_names)
        except Exception as e:  # noqa: BLE001 — a search aid must never fail the authoring turn
            _log.warning("crucible_docs_get_section failed (%s)", e)
            return _UNAVAILABLE
        if content is None:
            return f"No section found for {' > '.join(self.section_names)!r}"
        return content


def get_tools(db: ComposerRAGDB) -> Iterable[BaseTool]:
    return [
        CrucibleSectionGet.bind(db).as_tool("crucible_docs_get_section"),
        CrucibleVectorSearch.bind(db).as_tool("crucible_docs_search"),
        CrucibleKeywordSearch.bind(db).as_tool("crucible_docs_keyword_search"),
    ]
