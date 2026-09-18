"""Search tools for the ``cvlr_kb`` corpus — the published Solana and CVLR documentation.

Bound onto an authoring env by :mod:`composer.tools.rag_env`, which resolves the ``cvlr_kb``
tag to this module's :func:`get_tools`. Structurally a sibling of
:mod:`composer.tools.foundry_rag`: the same three retrieval styles over the same
:class:`~composer.rag.db.ComposerRAGDB` API.

What a given database was loaded from is deliberately invisible here: the tag is the unit of
resolution, and the corpus is meant to take further sources under it without these tools learning
which one answered a search.

Possible future work:

A corpus this size admits an alternative these tools do not: inlining it. The Solana manual is
~15k words over ~157 sections against the CVL manual's ~40k over ~167 — about as many things to
tell apart, each a third as long — and its authoring core (speclanguage, spec, nondet, accounts,
mocks, parametric-rules, methodology) is ~10k words, the same order as the CVL context documents
:mod:`composer.kb.kb_context` already puts in front of every CVL agent. Inlining that core and
leaving these tools the lookup-shaped tail (options, troubleshooting, installation, output) is
therefore a live option, and not an obviously better one: a context document is paid by every
agent on every call, amortized only while it stays byte-stable enough to cache, whereas a search
is paid only when someone searches.

What would settle it, none of it observed yet:

* Retrieval concentrates — a handful of sections answer most searches. Then the tool call buys
  what a cached prefix gives away.
* Searches cluster at the start of an authoring step instead of spreading through it. That is
  orienting, which a document does better than a query.
* Specs go wrong in ways a section covers and no search asked after. Retrieval cannot fix not
  knowing to ask; inlining can.
* The core stays inlinable. This tag is meant to take further sources, and the first one to land
  may put the corpus past what a prompt should carry — which settles it the other way.
"""

from typing import Iterable

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.rag.db import ComposerRAGDB
from graphcore.tools.schemas import WithAsyncDependencies


def _header_string(s: list[str]) -> str:
    return " > ".join(i for i in s if i)


class CvlrKeywordSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    """
    Search the CVLR knowledge base by keyword (full text search). Use this when you know the
    identifier you are looking for — a macro (`cvlr_assert`, `cvlr_assume`, `clog`), a derive
    (`Nondet`, `CvlrLog`), a conf option (`solana_inlining`), or a cargo feature.

    Returns matching section titles in relevance order; read one with `cvlr_get_section`.
    """

    query: str = Field(description=(
        "A websearch-style query string. Unquoted terms are combined with AND. "
        "Use 'OR' between terms for alternatives, quotes for exact phrases, "
        "and '-' to exclude terms. Example: '\"module redirect\" OR cfg_attr -soroban'"
    ))
    limit: int = Field(default=10, description="Maximum number of results to return.")

    async def run(self) -> str:
        with self.tool_deps() as db:
            res = await db.search_manual_keywords(self.query, limit=self.limit)
            to_ret = [
                f"{_header_string(r.headers)} [relevance: {r.relevance:.4f}]" for r in res
            ]
            if not to_ret:
                return "No results found"
            return "\n".join(to_ret)


class CvlrVectorSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    """
    Search the CVLR knowledge base with a natural-language question. Covers the CVLR API (what a
    macro does, what a helper's signature is) and the methodology around it (how to mock an SDK
    boundary, how to make a counterexample readable, when a rule should be parametric).

    Returns the section title, the relevant text, and a relevance score.
    """

    query: str = Field(description=(
        "A single natural-language question. For example, 'how do I give an account field a "
        "nondeterministic value?' or 'how do I stub a cross-program invocation?'"
    ))
    similarity_cutoff: float = Field(
        default=0.5, description="Minimum cosine similarity threshold for results."
    )
    max_results: int = Field(default=10, description="Maximum number of results to return.")

    async def run(self) -> str:
        with self.tool_deps() as db:
            res = await db.find_refs(
                self.query,
                similarity_cutoff=self.similarity_cutoff,
                top_k=self.max_results,
            )
            to_ret = [
                f"----\nSection: {_header_string(r.headers)}\n\n{r.content}\n"
                f"Similarity: {r.similarity:.4f}"
                for r in res
            ]
            if not to_ret:
                return "(No results found)"
            return "\n".join(to_ret)


class CvlrSectionGet(WithAsyncDependencies[str, ComposerRAGDB]):
    """
    Retrieve a whole section of the CVLR knowledge base by its heading path.
    """

    section_names: list[str] = Field(description=(
        "The heading path identifying the section to read. The search tools print paths with "
        "`>` separators, so 'Mocking & Munging > Module redirect' is passed as "
        "['Mocking & Munging', 'Module redirect']."
    ))

    async def run(self) -> str:
        with self.tool_deps() as db:
            content = await db.get_manual_section(self.section_names)
            if content is None:
                return f"No section found for {' > '.join(self.section_names)!r}"
            return content


def get_tools(db: ComposerRAGDB) -> Iterable[BaseTool]:
    return [
        CvlrSectionGet.bind(db).as_tool("cvlr_get_section"),
        CvlrVectorSearch.bind(db).as_tool("cvlr_manual_search"),
        CvlrKeywordSearch.bind(db).as_tool("cvlr_keyword_search"),
    ]
