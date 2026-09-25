"""Search over ``cvlr_api_kb`` — the CVLR API, generated from the crates.

The tool names are the point. A retrieval hit carries no provenance — :meth:`find_refs` returns
headers, content and a similarity score, and nothing about where the row came from — so the only
thing telling an agent whether an answer is the crates' own account of themselves or a paragraph
of hand-written prose is **which tool returned it** (``docs/cvlr-api-docs-plan.md`` §4.1). Hence
``cvlr_api_*`` rather than more ``cvlr_*`` tools over a shared corpus.

Three tools for three questions, and the middle one is not the shape
:mod:`composer.tools.cvlr_rag` uses:

* *"what helper does X?"* — natural language over the item docs.
* *"what is the exact signature of X, and which crate defines it?"* — one call, not
  keyword-search-then-fetch. That two-step is right when you are exploring a manual and wrong when
  you already have an identifier, which by the live run's tool census is the dominant case.
* *"does X exist?"* — the closed-world read. A vector search always returns its nearest rows, so
  "nothing came back" cannot distinguish absence from an oddly phrased query. The producer emits
  one section per crate listing its whole public surface, and this is the tool that fetches it.
"""

from typing import Iterable

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.rag.db import ComposerRAGDB
from graphcore.tools.schemas import WithAsyncDependencies

#: The heading a crate's complete-surface section is filed under, below its version level. The
#: producer writes it and :class:`CvlrApiSurface` reads it, so the string lives in one place.
SURFACE_HEADING = "Complete public surface"

#: The root of every header path in this corpus. Present in the path so a row read out of context
#: still says which corpus it came from.
API_ROOT = "CVLR API"

_AUTHORITY = (
    "This corpus is generated from the CVLR crates at the releases this build pins, so it is "
    "authoritative on what exists and what its signature is. Where it disagrees with the CVLR "
    "manual, with a past project, or with recall, it is right and they are stale."
)


def _header_string(s: list[str]) -> str:
    return " > ".join(i for i in s if i)


class CvlrApiSearch(WithAsyncDependencies[str, ComposerRAGDB]):
    __doc__ = (
        "Search the generated CVLR API reference with a natural-language question. Use it when "
        "you know what you want to *do* but not what it is called — 'how do I give an account "
        "field a nondeterministic value?', 'what gives me an unbounded integer?'.\n\n"
        f"{_AUTHORITY}\n\n"
        "If you already have an identifier, use `cvlr_api_lookup` instead; it returns the "
        "signature in one call. To ask whether something exists at all, use `cvlr_api_surface` — "
        "this tool always returns its nearest matches, so an unhelpful answer here is not "
        "evidence of absence."
    )

    query: str = Field(description="A single natural-language question about the CVLR API.")
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
            if not res:
                return (
                    "(No results found. This does not mean the item does not exist — vector "
                    "search returns nearest matches, so rephrase, or call `cvlr_api_surface` for "
                    "the crate to settle existence.)"
                )
            return "\n".join(
                f"----\nSection: {_header_string(r.headers)}\n\n{r.content}\n"
                f"Similarity: {r.similarity:.4f}"
                for r in res
            )


class CvlrApiLookup(WithAsyncDependencies[str, ComposerRAGDB]):
    __doc__ = (
        "Look up one CVLR item by name and get its entry in full: the signature, the crate that "
        "*defines* it, its feature gate, and whatever the crate documents about it. Use this "
        "whenever you have an identifier — a macro (`cvlr_assert_le`, `clog`), a function "
        "(`nondet`, `cvlr_deserialize_nondet_accounts`), a trait, a derive or a type.\n\n"
        f"{_AUTHORITY}\n\n"
        "The defining crate is what you get back, not the facade path: `cvlr` re-exports almost "
        "everything, so the item you reach as `cvlr::x::y` usually lives in a sibling crate. "
        "Write what compiles, which is whichever path the entry gives."
    )

    name: str = Field(description=(
        "The exact identifier, without a `!` and without a path — `cvlr_assert_le`, not "
        "`cvlr::cvlr_assert_le!`."
    ))
    limit: int = Field(default=5, description="How many candidate entries to return at most.")

    async def run(self) -> str:
        with self.tool_deps() as db:
            # Keyword search finds the candidates; the fetch is what makes this one call instead
            # of two. A bare identifier is already a websearch-style query for one term.
            hits = await db.search_manual_keywords(self.name, limit=self.limit)
            if not hits:
                return (
                    f"Nothing in the generated CVLR API reference matches {self.name!r}. Check "
                    f"the spelling, then call `cvlr_api_surface` for the crate you expect it in — "
                    f"that section lists the whole public surface and settles whether it exists."
                )
            sections = []
            for hit in hits:
                content = await db.get_manual_section(list(hit.headers))
                if content is not None:
                    sections.append(f"----\n{_header_string(list(hit.headers))}\n\n{content}")
            if not sections:
                return (
                    f"Matched {len(hits)} entries for {self.name!r} but none could be read back; "
                    f"the corpus may be mid-rebuild."
                )
            return "\n".join(sections)


class CvlrApiSurface(WithAsyncDependencies[str, ComposerRAGDB]):
    __doc__ = (
        "List a CVLR crate's **complete** public surface — every item it exports, at the release "
        "this build pins.\n\n"
        "This is the tool that answers *does X exist?*, and it is the only one that can: it "
        "returns a closed list, so a name that is absent from it is a name you do not have. Do "
        "not infer absence from an empty search, and do not write a helper you could not find "
        "here.\n\n"
        f"{_AUTHORITY}"
    )

    crate: str = Field(description=(
        "The crate name as cargo spells it — `cvlr-asserts`, `cvlr-solana`, `cvlr-mathint`. Pass "
        "the crate that *defines* the item if you know it; the facade `cvlr` lists only what it "
        "defines itself, which is almost nothing."
    ))

    async def run(self) -> str:
        with self.tool_deps() as db:
            # The version level sits between the root and the crate, and a caller does not know
            # it — the point of the pin is that they never have to. Keyword search over the
            # heading finds the one row whatever the version is.
            hits = await db.search_manual_keywords(f'"{self.crate}" "{SURFACE_HEADING}"', limit=5)
            for hit in hits:
                headers = list(hit.headers)
                if self.crate in headers and SURFACE_HEADING in headers:
                    content = await db.get_manual_section(headers)
                    if content is not None:
                        return f"{_header_string(headers)}\n\n{content}"
            return (
                f"No public-surface listing for a crate named {self.crate!r}. Either the name is "
                f"misspelled or this build does not pin that crate — in which case you cannot use "
                f"it, whatever it offers."
            )


def get_tools(db: ComposerRAGDB) -> Iterable[BaseTool]:
    return [
        CvlrApiSearch.bind(db).as_tool("cvlr_api_search"),
        CvlrApiLookup.bind(db).as_tool("cvlr_api_lookup"),
        CvlrApiSurface.bind(db).as_tool("cvlr_api_surface"),
    ]
