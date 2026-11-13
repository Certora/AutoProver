"""Tests for composer.rag.db — every corpus backend's insert and retrieval, against one corpus."""

from typing import AsyncIterator, TYPE_CHECKING, Iterator
from dataclasses import dataclass, field

import numpy as np
import pytest
import pytest_asyncio
from numpy import ndarray
from psycopg_pool.pool_async import AsyncConnectionPool

from composer.rag.db import (
    BlockChunk, ComposerRAGDB, PostgreSQLRAGDatabase, SqliteRag, rag_build_context, rag_context,
)
from composer.rag.text import code_ref_tag

from .conftest import MockSentenceTransformer, EMBEDDING_DIM

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


# ── Test corpus ──

@dataclass
class MockManualSection:
    text: str
    question: str | None = None
    code_refs: list[str] = field(default_factory=list)

type ManualSectionPart = str | tuple[str, str] | MockManualSection

type TestCorpus = dict[
    tuple[str, ...],
    ManualSectionPart | list[ManualSectionPart]
]

_TEST_CORPUS : TestCorpus  = {
    ("CVL", "Invariants"): "Invariants must hold across all transactions in the smart contract.",
    ("CVL", "Invariants", "Checking"): (
         "How are invariants verified?",
         "Invariants are properties that must hold before and after every transaction. They are assumed in the prestate and asserted in the post state"
    ),
    ("CVL", "Rules"): "Rules describe expected behavior of specific contract functions.",
    ("CVL", "Rules", "Example"): MockManualSection(
        text = f"Example usage: {code_ref_tag(0)}",
        question = "show me a CVL example",
        code_refs=["rule sanity { assert true; }"]
    ),
    ("CVL", "Ghosts"): "Ghost variables track abstract state not present in the contract.",
    ("CVL", "Long Topic"): [
        "Part one of the long section.",
        "Part two of the long section.",
    ],
    ("CVL", "Hooks", "Syntax"): MockManualSection(
        code_refs=["hook Sstore uint256 uint256 {}"],
        text=f"Hook example: {code_ref_tag(0)}",
    ),
    ("CVL", "Hooks"): (
        "What are hooks in CVL?",
        "What are hooks in CVL?"
    )
}

def _random_unit_vector(rng: np.random.RandomState) -> ndarray:
    vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec


def _perturb(vec: ndarray, rng: np.random.RandomState, epsilon: float = 0.05) -> ndarray:
    """Small perturbation of a unit vector — stays close in cosine distance."""
    noise = rng.randn(EMBEDDING_DIM).astype(np.float32)
    noise /= np.linalg.norm(noise)
    perturbed = vec + epsilon * noise
    perturbed /= np.linalg.norm(perturbed)
    return perturbed

def content_iter() -> Iterator[tuple[tuple[str, ...], int, MockManualSection]]:
    for (k, sec) in _TEST_CORPUS.items():
        if not isinstance(sec, list):
            sec = [sec]
        for (part, content) in enumerate(sec):
            if isinstance(content, tuple):
                yield (k, part, MockManualSection(text=content[1], question=content[0]))
            elif isinstance(content, str):
                yield (k, part, MockManualSection(text=content))
            else:
                yield (k, part, content)

@pytest.fixture(scope="session")
def mock_model() -> "SentenceTransformer":
    to_ret = MockSentenceTransformer()
    rng = np.random.RandomState(seed=123456789)
    for (_, _, sec) in content_iter():
        content_vector = _random_unit_vector(rng)
        content : str = sec.text
        if sec.question is not None:
            to_ret.register(f"search_query: {sec.question}", _perturb(content_vector, rng))
        to_ret.register(f"search_document: {content}", content_vector)
    return to_ret #type: ignore

async def _populate(db: ComposerRAGDB) -> None:
    for (sec, part, content) in content_iter():
        mock_chunk = BlockChunk(
            code_refs=content.code_refs,
            part=part,
            chunk=content.text,
            headers=list(sec)
        )
        await db.add_manual_section(mock_chunk)
        await db.add_chunks_batch([mock_chunk])


@pytest_asyncio.fixture(scope="session")
async def postgres_rag(session_pg_database: AsyncConnectionPool | None, mock_model: "SentenceTransformer") -> AsyncIterator[PostgreSQLRAGDatabase | None]:
    if session_pg_database is None:
        yield None
        return
    to_ret = PostgreSQLRAGDatabase(
        conn_string=session_pg_database,
        model=mock_model
    )
    await to_ret.test_connection()
    await _populate(to_ret)
    yield to_ret


@pytest_asyncio.fixture(scope="session")
async def sqlite_rag(tmp_path_factory: pytest.TempPathFactory, mock_model: "SentenceTransformer") -> AsyncIterator[ComposerRAGDB]:
    # Built and then reopened read-only, the way a corpus file baked into an image is used.
    location = SqliteRag(tmp_path_factory.mktemp("sqlite") / "corpus.sqlite")
    async with rag_build_context(location, mock_model) as db:
        await _populate(db)
    async with rag_context(location, mock_model) as db:
        yield db


@pytest_asyncio.fixture(scope="session", params=["sqlite", "postgres"])
async def rag_db(
    request: pytest.FixtureRequest,
    postgres_rag: PostgreSQLRAGDatabase | None,
    sqlite_rag: ComposerRAGDB
) -> AsyncIterator[ComposerRAGDB]:
    if request.param == "sqlite":
        yield sqlite_rag
    else:
        if postgres_rag is None:
            pytest.skip("PG Containers not installed")
            return
        yield postgres_rag

# ── Manual section tests (no embeddings needed) ──


@pytest.mark.asyncio
class TestManualSections:
    async def test_retrieve_by_headers(self, rag_db: ComposerRAGDB) -> None:
        result = await rag_db.get_manual_section(["CVL", "Invariants"])
        assert result is not None
        assert "Invariants must hold across all transactions" in result

    async def test_an_empty_header_is_absent(self, rag_db: ComposerRAGDB) -> None:
        # Stored paths never hold "" (it is stored as absent), so a lookup must read it the same way.
        assert await rag_db.get_manual_section(["CVL", "Invariants", ""]) == \
            await rag_db.get_manual_section(["CVL", "Invariants"])

    async def test_retrieve_nonexistent(self, rag_db: ComposerRAGDB) -> None:
        result = await rag_db.get_manual_section(["Nonexistent", "Section"])
        assert result is None

    async def test_multipart_section(self, rag_db: ComposerRAGDB) -> None:
        result = await rag_db.get_manual_section(["CVL", "Long Topic"])
        assert result is not None
        assert "Part one" in result
        assert "Part two" in result

    async def test_code_refs_expanded(self, rag_db: ComposerRAGDB) -> None:
        result = await rag_db.get_manual_section(["CVL", "Hooks", "Syntax"])
        assert result is not None
        assert "hook Sstore uint256 uint256 {}" in result
        assert code_ref_tag(0) not in result

    async def test_keyword_search(self, rag_db: ComposerRAGDB) -> None:
        hits = await rag_db.search_manual_keywords("invariants")
        assert len(hits) > 0
        assert hits[0].headers == ["CVL", "Invariants"]


# ── Embedding / vector search tests ──


@pytest.mark.asyncio
class TestFindRefs:
    async def test_find_matching_document(self, rag_db: ComposerRAGDB) -> None:
        # "How are invariants verified?" is a registered query near the
        # Invariants/Checking doc vector
        results = await rag_db.find_refs("How are invariants verified?", similarity_cutoff=0.9)
        assert len(results) >= 1
        assert results[0].similarity > 0.99
        assert "Invariants are properties" in results[0].content

    async def test_best_match_ranked_first(self, rag_db: ComposerRAGDB) -> None:
        results = await rag_db.find_refs(
            "How are invariants verified?", similarity_cutoff=0.3, top_k=10
        )
        assert len(results) >= 1
        # The closest doc should be the invariants entry
        assert "Invariants" in results[0].content
        assert results[0].similarity > 0.99
        # Any additional results should be less similar
        for r in results[1:]:
            assert r.similarity < results[0].similarity

    async def test_similarity_cutoff_filters(self, rag_db: ComposerRAGDB) -> None:
        # Even the best Q/A pair has similarity ~0.999, not 1.0 —
        # a cutoff of 0.9999 should exclude everything
        results = await rag_db.find_refs(
            "How are invariants verified?", similarity_cutoff=0.9999
        )
        assert len(results) == 0

    async def test_code_refs_inlined_in_results(self, rag_db: ComposerRAGDB) -> None:
        # "show me a CVL example" is near the Rules/Example doc which has a code ref
        results = await rag_db.find_refs("show me a CVL example", similarity_cutoff=0.9)
        assert len(results) >= 1
        assert "rule sanity { assert true; }" in results[0].content
        assert code_ref_tag(0) not in results[0].content


# ── Query-language parity: the tools promise websearch syntax on every backend ──


@pytest.mark.asyncio
class TestKeywordQueryLanguage:
    async def test_stop_words_are_not_required(self, rag_db: ComposerRAGDB) -> None:
        hits = await rag_db.search_manual_keywords("what are the invariants")
        assert hits and hits[0].headers == ["CVL", "Invariants"]

    async def test_a_minus_excludes(self, rag_db: ComposerRAGDB) -> None:
        hits = await rag_db.search_manual_keywords("invariants -transactions")
        assert ["CVL", "Invariants"] not in [h.headers for h in hits]

    async def test_or_matches_either(self, rag_db: ComposerRAGDB) -> None:
        hits = await rag_db.search_manual_keywords("ghost or hooks")
        found = {tuple(h.headers) for h in hits}
        assert ("CVL", "Ghosts") in found and ("CVL", "Hooks") in found

    async def test_an_exclusion_stays_in_its_alternative(self, rag_db: ComposerRAGDB) -> None:
        # (ghost AND NOT contract) OR rules: the Rules section mentions "contract", but its
        # alternative excludes nothing.
        hits = await rag_db.search_manual_keywords("ghost -contract or rules")
        assert ["CVL", "Rules"] in [h.headers for h in hits]
        assert ["CVL", "Ghosts"] not in [h.headers for h in hits]

    async def test_and_binds_tighter_than_or(self, rag_db: ComposerRAGDB) -> None:
        # (invariants AND transactions) OR ghost: Ghosts matches without "invariants".
        hits = await rag_db.search_manual_keywords("invariants transactions or ghost")
        assert ["CVL", "Ghosts"] in [h.headers for h in hits]

    async def test_stray_punctuation_is_not_an_error(self, rag_db: ComposerRAGDB) -> None:
        hits = await rag_db.search_manual_keywords('invariants: "unbalanced (quote')
        assert isinstance(hits, list)

    async def test_min_depth(self, rag_db: ComposerRAGDB) -> None:
        hits = await rag_db.search_manual_keywords("invariants", min_depth=3)
        assert [h.headers for h in hits] == [["CVL", "Invariants", "Checking"]]


@pytest.mark.asyncio
async def test_find_refs_section_filter(rag_db: ComposerRAGDB) -> None:
    results = await rag_db.find_refs(
        "How are invariants verified?", similarity_cutoff=-1.0, manual_section=["Rules"]
    )
    assert results
    assert all("Rules" in r.headers for r in results)
