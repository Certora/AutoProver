"""Model-facing rendering of RAG search results.

Every corpus's search tools (CVL, CVLR, Foundry) sit on the same two result types and present
them the same way, so that one description of the output format fits all of them. A section path
is always joined with `>`, which is how the search tools teach the model to name a section back to
the get-section tools.
"""

from composer.rag.types import ManualRef, ManualSectionHit

_NO_RESULTS = "No results found."


def header_path(headers: list[str]) -> str:
    return " > ".join(h for h in headers if h)


def render_section_hits(hits: list[ManualSectionHit]) -> str:
    if not hits:
        return _NO_RESULTS
    return "\n".join(
        f"{header_path(h.headers)} [relevance: {h.relevance:.4f}]" for h in hits
    )


def render_refs(refs: list[ManualRef]) -> str:
    if not refs:
        return _NO_RESULTS
    return "\n".join(
        f"----\nSection: {header_path(r.headers)}\n\n{r.content}\n\n"
        f"Similarity: {r.similarity:.4f}"
        for r in refs
    )


def no_such_section(headers: list[str]) -> str:
    return f"No section found for {header_path(headers)!r}"
