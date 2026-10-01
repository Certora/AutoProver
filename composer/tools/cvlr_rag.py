"""Search over ``cvlr_kb`` — the Solana Prover manual, and nothing else.

Once the second CVLR corpus existed this one had to say what it is *not*. It is prose: methodology,
idiom, why one shape of rule beats another, what a pitfall costs. It was written against particular
releases and is allowed to lag them, so it is not authority on what exists — that is
:mod:`composer.tools.cvlr_api_rag`, generated from the crates at the releases this build pins
(``docs/cvlr-api-docs-plan.md`` §4.1, §4.6).

The ordering is not stated in these docstrings for an agent to weigh, because these tools do not
reach an agent that has to weigh it: they go to the ``cvlr_research`` sub-agent alone, which holds
both corpora and is told which wins. What the docstrings have to do is stop *this* tool being
reached for the questions the other one answers — the docstrings used to advertise "the CVLR API
(what a macro does, what a helper's signature is)", which is now exactly the wrong reason to come
here.

``cvlr_kb`` once also carried a generated crate reference and project-derived practice entries.
The first is what ``cvlr_api_kb`` replaces; the second moved to the always-in-context bundle and
the trigger-indexed recipes (``cvlr-knowledge-plan.md`` §4), because corpus search is allowed to be
absent at run time and nothing load-bearing may depend on it.
"""

from typing import Iterable

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.rag.db import ComposerRAGDB
from composer.rag.render import no_such_section, render_refs, render_section_hits
from graphcore.tools.schemas import WithAsyncDependencies


class CvlrKeywordSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    """
    Search the CVLR manual by keyword (full text search). Use it to find *where the manual
    discusses* a term — a conf option (`solana_inlining`), a cargo feature, a technique with a name
    ("module redirect", "parametric rule").

    Not for establishing that an item exists or what its signature is: this is hand-written prose
    that lags the crates. `cvlr_api_lookup` answers that from the crates themselves.

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
            return render_section_hits(
                await db.search_manual_keywords(self.query, limit=self.limit)
            )


class CvlrVectorSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    """
    Search the Solana Prover manual with a natural-language question. It covers *methodology*: how
    to mock an SDK boundary, how to make a counterexample readable, when a rule should be
    parametric, which pitfalls cost people time.

    It does not settle what exists. The manual was written against particular releases and is
    allowed to be out of date about the CVLR surface; `cvlr_api_search` and `cvlr_api_lookup` read
    the crates this build pins. Where the two disagree, the crates are right.

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
            return render_refs(
                await db.find_refs(
                    self.query,
                    similarity_cutoff=self.similarity_cutoff,
                    top_k=self.max_results,
                )
            )


class CvlrSectionGet(WithAsyncDependencies[str, ComposerRAGDB]):
    """
    Retrieve a whole section of the Solana Prover manual by its heading path.
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
                return no_such_section(self.section_names)
            return content


def get_tools(db: ComposerRAGDB) -> Iterable[BaseTool]:
    return [
        CvlrSectionGet.bind(db).as_tool("cvlr_get_section"),
        CvlrVectorSearch.bind(db).as_tool("cvlr_manual_search"),
        CvlrKeywordSearch.bind(db).as_tool("cvlr_keyword_search"),
    ]
