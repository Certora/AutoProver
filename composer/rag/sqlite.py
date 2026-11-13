"""A RAG corpus in a single SQLite file.

Corpora are small (hundreds to a few thousand chunks), read-only once built, and derived entirely
from inputs an image already carries, so one file per corpus replaces a database server:

- semantic search is an exact, brute-force cosine scan over every embedding, held in memory as one
  matrix — at this size it costs well under a millisecond and needs no approximate index;
- keyword search is an FTS5 index over the manual sections;
- section retrieval is a plain lookup by header path.

A corpus is written by :meth:`SqliteRAGDatabase.build` into a scratch file that replaces the target
only once the whole build succeeds, so a reader never sees a half-built or emptied corpus.
"""

import asyncio
import os
import pathlib
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast, override

import numpy as np
from numpy import ndarray

from composer.rag.base import ComposerRAGDB
from composer.rag.fts_query import to_fts5_query
from composer.rag.models import EMBEDDING_MODEL
from composer.rag.text import code_ref_tag
from composer.rag.types import BlockChunk, ManualRef, ManualSectionHit

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

#: Bumped whenever the tables below change shape; a reader refuses a file of any other version.
SCHEMA_VERSION = "1"

_HEADER_COLUMNS = "h1, h2, h3, h4, h5, h6"

_SCHEMA = f"""
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL,
    h1 TEXT, h2 TEXT, h3 TEXT, h4 TEXT, h5 TEXT, h6 TEXT
);

CREATE TABLE manual_sections (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    h1 TEXT, h2 TEXT, h3 TEXT, h4 TEXT, h5 TEXT, h6 TEXT,
    part INTEGER NOT NULL,
    UNIQUE ({_HEADER_COLUMNS}, part)
);

CREATE VIRTUAL TABLE manual_sections_fts USING fts5(
    content,
    content='manual_sections',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TABLE manual_section_code_refs (
    section_id INTEGER NOT NULL REFERENCES manual_sections(id),
    id INTEGER NOT NULL,
    code_body TEXT NOT NULL,
    PRIMARY KEY (section_id, id)
);
"""

type _Headers = tuple[str | None, str | None, str | None, str | None, str | None, str | None]


class CorpusFileError(Exception):
    """The corpus file is missing, or was built for a different schema or embedding model."""


def _padded_headers(headers: Sequence[str]) -> _Headers:
    padded = [h or None for h in headers[:6]] + [None] * (6 - min(len(headers), 6))
    return (padded[0], padded[1], padded[2], padded[3], padded[4], padded[5])


def _leading_headers(row: Sequence[str | None]) -> list[str]:
    out: list[str] = []
    for h in row:
        if h is None:
            break
        out.append(h)
    return out


def _expand_code_refs(content: str, code_refs: Sequence[str]) -> str:
    for i, code in enumerate(code_refs):
        content = content.replace(code_ref_tag(i), code)
    return content


@dataclass(frozen=True)
class _VectorIndex:
    #: One unit-norm row per document.
    matrix: ndarray
    contents: list[str]
    headers: list[_Headers]


def _open_reader(path: pathlib.Path) -> sqlite3.Connection:
    if not path.is_file():
        raise CorpusFileError(
            f"RAG corpus file {path} does not exist. To get the built-in corpora, copy them out of "
            "the AutoProver Docker image with scripts/fetch_rag.sh, or build them from the docs "
            "with scripts/refresh_rag.sh (see AICOMPOSER_INFRA.md)."
        )
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError as e:
        conn.close()
        raise CorpusFileError(f"{path} is not a RAG corpus file: {e}") from e
    expected = {"schema_version": SCHEMA_VERSION, "embedding_model": EMBEDDING_MODEL}
    found = {k: meta.get(k) for k, v in expected.items() if meta.get(k) != v}
    if found:
        wanted = {k: expected[k] for k in found}
        conn.close()
        raise CorpusFileError(
            f"{path} was built with {found}, but this code expects {wanted}; rebuild the corpus"
        )
    return conn


class SqliteRAGDatabase(ComposerRAGDB):
    """A corpus file. Open one for reading with :meth:`reader`, or build one with
    :meth:`build`."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], model: "SentenceTransformer"):
        super().__init__(model)
        self._connect = connect
        self._conn: sqlite3.Connection | None = None
        # One connection, shared by the worker threads every call runs on.
        self._lock = threading.Lock()
        self._index: _VectorIndex | None = None

    @staticmethod
    def reader(path: pathlib.Path, model: "SentenceTransformer") -> "SqliteRAGDatabase":
        """A read-only view of the corpus at ``path``. Nothing is opened until the first call
        (or :meth:`open`), which raises :class:`CorpusFileError` if the file is unusable."""
        return SqliteRAGDatabase(lambda: _open_reader(path), model)

    @staticmethod
    @asynccontextmanager
    async def build(
        path: pathlib.Path, model: "SentenceTransformer"
    ) -> AsyncIterator["SqliteRAGDatabase"]:
        """Build a fresh corpus at ``path``. It is written to a scratch file beside ``path`` and
        moved over it only when the block exits normally; on an exception ``path`` is untouched."""
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(path.name + ".partial")
        scratch.unlink(missing_ok=True)

        conn = sqlite3.connect(scratch, check_same_thread=False)
        db = SqliteRAGDatabase(lambda: conn, model)
        try:
            # The scratch file is discarded on any failure, so durability during the build buys
            # nothing.
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.executescript(_SCHEMA)
            yield db
            await db._seal()
            os.replace(scratch, path)
        except BaseException:
            await db.aclose()
            scratch.unlink(missing_ok=True)
            raise

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    async def _run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return fn(self._connection())
        return await asyncio.to_thread(locked)

    @override
    async def open(self) -> None:
        await self._run(lambda _conn: None)

    async def _seal(self) -> None:
        """Stamp the corpus with what it was built for, commit it, and close it."""
        def seal(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("schema_version", SCHEMA_VERSION), ("embedding_model", EMBEDDING_MODEL)],
            )
            conn.commit()
        await self._run(seal)
        await self.aclose()

    @override
    async def aclose(self) -> None:
        def close() -> None:
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None
        await asyncio.to_thread(close)

    @override
    async def add_chunks_batch(self, chunks: list[BlockChunk]) -> None:
        if not chunks:
            return
        embeddings = await self.embed_docs(chunks)
        rows = [
            (
                _expand_code_refs(chunk.chunk, chunk.code_refs),
                np.asarray(embedding, dtype=np.float32).tobytes(),
                *_padded_headers(chunk.headers),
            )
            for chunk, embedding in zip(chunks, embeddings)
        ]

        def insert(conn: sqlite3.Connection) -> None:
            conn.executemany(
                f"INSERT INTO documents (content, embedding, {_HEADER_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._index = None
        await self._run(insert)

    @override
    async def add_manual_section(self, ch: BlockChunk) -> None:
        def insert(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                f"INSERT INTO manual_sections (content, {_HEADER_COLUMNS}, part) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ch.chunk, *_padded_headers(ch.headers), ch.part),
            )
            section_id = cur.lastrowid
            conn.execute(
                "INSERT INTO manual_sections_fts (rowid, content) VALUES (?, ?)",
                (section_id, ch.chunk),
            )
            conn.executemany(
                "INSERT INTO manual_section_code_refs (section_id, id, code_body) VALUES (?, ?, ?)",
                [(section_id, i, code) for i, code in enumerate(ch.code_refs)],
            )
        await self._run(insert)

    def _load_index(self, conn: sqlite3.Connection) -> _VectorIndex:
        if self._index is None:
            rows = conn.execute(
                f"SELECT content, embedding, {_HEADER_COLUMNS} FROM documents ORDER BY id"
            ).fetchall()
            if rows:
                matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                matrix = matrix / np.where(norms == 0, 1, norms)
            else:
                matrix = np.zeros((0, 0), dtype=np.float32)
            self._index = _VectorIndex(
                matrix=matrix,
                contents=[r[0] for r in rows],
                headers=[cast(_Headers, tuple(r[2:8])) for r in rows],
            )
        return self._index

    @override
    async def find_refs(self, query: str, similarity_cutoff: float = 0.5,
                        top_k: int = 10, manual_section: list[str] = []) -> list[ManualRef]:
        query_vec = np.asarray(await self.embed_query(query), dtype=np.float32)
        norm = np.linalg.norm(query_vec)
        if norm != 0:
            query_vec = query_vec / norm
        index = await self._run(self._load_index)
        if not index.contents:
            return []

        similarities = index.matrix @ query_vec
        candidates = np.arange(len(index.contents))
        if manual_section:
            wanted = set(manual_section)
            candidates = np.array(
                [i for i in candidates if not wanted.isdisjoint(index.headers[i])], dtype=np.int64
            )
        ranked = candidates[np.argsort(-similarities[candidates], kind="stable")][:top_k]

        refs: list[ManualRef] = []
        for i in ranked:
            similarity = float(similarities[i])
            if similarity < similarity_cutoff:
                break
            refs.append(ManualRef(
                headers=_leading_headers(index.headers[i]),
                content=index.contents[i],
                similarity=similarity,
            ))
        return refs

    @override
    async def search_manual_keywords(self, query: str, *, min_depth: int = 0,
                                     limit: int = 10) -> list[ManualSectionHit]:
        if min_depth < 0 or min_depth > 6:
            raise ValueError("min_depth must be between 0 and 6")
        match = to_fts5_query(query)
        if match is None:
            return []
        depth_clause = f"AND s.h{min_depth} IS NOT NULL" if min_depth > 0 else ""

        def search(conn: sqlite3.Connection) -> list[ManualSectionHit]:
            rows = conn.execute(
                f"""
                SELECT bm25(manual_sections_fts) AS score,
                       s.h1, s.h2, s.h3, s.h4, s.h5, s.h6
                FROM manual_sections_fts
                JOIN manual_sections AS s ON s.id = manual_sections_fts.rowid
                WHERE manual_sections_fts MATCH ?
                {depth_clause}
                ORDER BY score
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
            # bm25() is lower-is-better; relevance is higher-is-better.
            return [
                ManualSectionHit(headers=[h for h in row[1:7] if h is not None], relevance=-row[0])
                for row in rows
            ]
        return await self._run(search)

    @override
    async def get_manual_section(self, headers: list[str]) -> str | None:
        padded = _padded_headers(headers)

        def lookup(conn: sqlite3.Connection) -> str | None:
            sections = conn.execute(
                """
                SELECT id, content FROM manual_sections
                WHERE h1 IS ? AND h2 IS ? AND h3 IS ? AND h4 IS ? AND h5 IS ? AND h6 IS ?
                ORDER BY part
                """,
                padded,
            ).fetchall()
            if not sections:
                return None
            parts: list[str] = []
            for section_id, content in sections:
                code_refs = [
                    code for (code,) in conn.execute(
                        "SELECT code_body FROM manual_section_code_refs "
                        "WHERE section_id = ? ORDER BY id",
                        (section_id,),
                    )
                ]
                parts.append(_expand_code_refs(content, code_refs))
            return "\n".join(parts)
        return await self._run(lookup)
