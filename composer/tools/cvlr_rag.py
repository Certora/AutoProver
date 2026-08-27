"""Search tools for the ``cvlr_kb`` corpus — CVLR reference and verification practice.

Bound onto an authoring env by :mod:`composer.tools.rag_env`, which resolves the ``cvlr_kb``
tag to this module's :func:`get_tools`. Structurally a sibling of
:mod:`composer.tools.foundry_rag`: the same three retrieval styles over the same
:class:`~composer.rag.db.ComposerRAGDB` API.

The corpus is fed by **two manifests sharing this one tag** (``docs/cvlr-capture-plan.md`` §8.2):
a public one built from the published Solana/CVLR documentation plus a generated CVLR crate
reference, and a project-derived one carrying idioms extracted from completed verification
projects. Which manifests a given database was loaded from is deliberately invisible here — the
tag is the unit of resolution, and an install with only the public half is a supported state.

One thing the tool surface *does* expose: a project-derived entry can be marked unreviewed
(``docs/cvlr-capture-plan.md`` §4.8 — ``proposed`` status, pending expert sign-off). The
descriptions say so, because an agent that cannot tell a certified idiom from a machine-abstracted
guess will trust both equally, and the two have very different failure modes.
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
    Search the CVLR knowledge base with a natural-language question. Covers both the CVLR API
    (what a macro does, what a helper's signature is) and verification practice (how to mock an
    SDK boundary, how to make a counterexample readable, when a property is not worth pursuing).

    Returns the section title, the relevant text, and a relevance score. An entry whose text is
    marked UNREVIEWED was machine-abstracted from prior projects and has not been signed off:
    treat it as a lead worth compiling, not as authority.
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
