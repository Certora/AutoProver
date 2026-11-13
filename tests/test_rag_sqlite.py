"""The SQLite corpus file: its keyword-query translation and its file-level guarantees. Retrieval
itself is covered against every backend in ``test_rag_db``."""

import pathlib
import sqlite3
from typing import TYPE_CHECKING

import pytest

from composer.rag.db import SqliteRag, parse_rag_location, PostgresRag, rag_build_context, rag_context
from composer.rag.fts_query import to_fts5_query
from composer.rag.sqlite import CorpusFileError
from composer.rag.types import BlockChunk

from .conftest import MockSentenceTransformer

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


@pytest.mark.parametrize(("query", "expected"), [
    ("invariant", '("invariant")'),
    ("how are invariants checked", '("invariants") AND ("checked")'),
    ('"the old value"', '("the old value")'),
    ("ghost or hook", '("ghost" OR "hook")'),
    ("ghost or hook sstore", '("ghost" OR "hook") AND ("sstore")'),
    ("ghost -hook", '("ghost") NOT "hook"'),
    ('ghost -"old value"', '("ghost") NOT "old value"'),
    ("require_invariant", '("require invariant")'),
    ("Rule.sanity: x", '("rule sanity") AND ("x")'),
    ('"unbalanced quote', '("unbalanced quote")'),
    ("or ghost", '("ghost")'),
    ("ghost or", '("ghost")'),
])
def test_to_fts5_query(query: str, expected: str) -> None:
    assert to_fts5_query(query) == expected


@pytest.mark.parametrize("query", ["", "   ", "the of and", "-ghost", '"" -', "() : ."])
def test_nothing_searchable(query: str) -> None:
    assert to_fts5_query(query) is None


@pytest.mark.parametrize("query", ['"a', "-", 'x "" y', "a OR OR b", 'NOT AND "x"', "a* ^b", "c++ <=> d"])
def test_every_translation_is_valid_fts5(query: str) -> None:
    match = to_fts5_query(query)
    if match is None:
        return
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
    conn.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()


def test_parse_rag_location() -> None:
    assert parse_rag_location("postgresql://u:p@h:5432/rag_db") == PostgresRag("postgresql://u:p@h:5432/rag_db")
    assert parse_rag_location("postgres://h/db") == PostgresRag("postgres://h/db")
    assert parse_rag_location("rag/cvl.sqlite") == SqliteRag(pathlib.Path("rag/cvl.sqlite"))


@pytest.fixture
def model() -> "SentenceTransformer":
    return MockSentenceTransformer()  # type: ignore[return-value]


def _chunk(text: str) -> BlockChunk:
    return BlockChunk(headers=["Doc", text], part=0, code_refs=[], chunk=text)


async def _build(location: SqliteRag, model: "SentenceTransformer", *texts: str) -> None:
    async with rag_build_context(location, model) as db:
        for t in texts:
            await db.add_manual_section(_chunk(t))
            await db.add_chunks_batch([_chunk(t)])


@pytest.mark.asyncio
async def test_a_missing_file_fails_on_open(tmp_path: pathlib.Path, model: "SentenceTransformer") -> None:
    with pytest.raises(CorpusFileError, match="does not exist"):
        async with rag_context(SqliteRag(tmp_path / "absent.sqlite"), model):
            pass
    # Opening for search must not leave an empty corpus behind for the next open to "succeed" on.
    assert not (tmp_path / "absent.sqlite").exists()


@pytest.mark.asyncio
async def test_a_rebuild_replaces_the_corpus(tmp_path: pathlib.Path, model: "SentenceTransformer") -> None:
    location = SqliteRag(tmp_path / "kb.sqlite")
    await _build(location, model, "alpha")
    await _build(location, model, "beta")
    async with rag_context(location, model) as db:
        assert await db.get_manual_section(["Doc", "alpha"]) is None
        assert await db.get_manual_section(["Doc", "beta"]) == "beta"


@pytest.mark.asyncio
async def test_a_failed_build_leaves_the_old_corpus(tmp_path: pathlib.Path, model: "SentenceTransformer") -> None:
    location = SqliteRag(tmp_path / "kb.sqlite")
    await _build(location, model, "alpha")
    with pytest.raises(RuntimeError, match="boom"):
        async with rag_build_context(location, model) as db:
            await db.add_manual_section(_chunk("beta"))
            raise RuntimeError("boom")
    async with rag_context(location, model) as db:
        assert await db.get_manual_section(["Doc", "alpha"]) == "alpha"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kb.sqlite"]


@pytest.mark.asyncio
async def test_a_corpus_is_read_only_once_built(tmp_path: pathlib.Path, model: "SentenceTransformer") -> None:
    location = SqliteRag(tmp_path / "kb.sqlite")
    await _build(location, model, "alpha")
    async with rag_context(location, model) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await db.add_manual_section(_chunk("beta"))


@pytest.mark.asyncio
async def test_a_corpus_from_another_embedding_model_is_refused(
    tmp_path: pathlib.Path, model: "SentenceTransformer"
) -> None:
    path = tmp_path / "kb.sqlite"
    await _build(SqliteRag(path), model, "alpha")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value = 'some/other-model' WHERE key = 'embedding_model'")
    with pytest.raises(CorpusFileError, match="rebuild the corpus"):
        async with rag_context(SqliteRag(path), model):
            pass


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_corpus_is_refused(
    tmp_path: pathlib.Path, model: "SentenceTransformer"
) -> None:
    path = tmp_path / "kb.sqlite"
    path.write_text("not a database")
    with pytest.raises(CorpusFileError, match="not a RAG corpus file"):
        async with rag_context(SqliteRag(path), model):
            pass
