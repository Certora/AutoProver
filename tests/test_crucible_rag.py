"""The Crucible docs-search tools must degrade gracefully — a backend failure (DB
down, or the embedding model choking on a query, as seen with nomic-embed on certain
inputs) returns "no results", never crashing the authoring turn / failing a component.
"""

import asyncio

from composer.tools.crucible_rag import (
    CrucibleKeywordSearch,
    CrucibleSectionGet,
    CrucibleVectorSearch,
    _UNAVAILABLE,
)


class _BoomDB:
    """A ComposerRAGDB stand-in whose every call raises (e.g. the embedding model
    RuntimeError observed on some queries)."""

    async def find_refs(self, *a, **k):
        raise RuntimeError("size of tensor a (21) must match tensor b (18)")

    async def search_manual_keywords(self, *a, **k):
        raise RuntimeError("db connection reset")

    async def get_manual_section(self, *a, **k):
        raise RuntimeError("db connection reset")


class _EmptyDB:
    async def find_refs(self, *a, **k):
        return []

    async def search_manual_keywords(self, *a, **k):
        return []


def _run(tool_cls, db, **fields) -> str:
    tool_cls._dep_ctx.set(db)  # what `tool_deps()` reads; normally set by binding
    return asyncio.run(tool_cls(**fields).run())


def test_vector_search_degrades_when_embedding_crashes():
    out = _run(CrucibleVectorSearch, _BoomDB(), query="how do I write an invariant?")
    assert out == _UNAVAILABLE


def test_keyword_search_degrades_on_db_error():
    out = _run(CrucibleKeywordSearch, _BoomDB(), query="fuzz_fixture")
    assert out == _UNAVAILABLE


def test_section_get_degrades_on_db_error():
    out = _run(CrucibleSectionGet, _BoomDB(), section_names=["A", "B"])
    assert out == _UNAVAILABLE


def test_vector_search_reports_no_results_without_crashing():
    out = _run(CrucibleVectorSearch, _EmptyDB(), query="anything")
    assert out == "(No results found)"


def test_keyword_search_reports_no_results():
    out = _run(CrucibleKeywordSearch, _EmptyDB(), query="anything")
    assert out == "No results found"
