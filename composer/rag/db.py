from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import AsyncIterator, Any, LiteralString, cast, override, TYPE_CHECKING
from dataclasses import dataclass
import logging
import os
import pathlib

from psycopg_pool.pool_async import AsyncConnectionPool
from psycopg.cursor_async import AsyncCursor
from psycopg.connection_async import AsyncConnection
from psycopg.rows import TupleRow

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer
else:
    SentenceTransformer = "SentenceTransformer"

from composer.rag.base import ComposerRAGDB
from composer.rag.sqlite import SqliteRAGDatabase
from composer.rag.types import ManualRef, BlockChunk, ManualSectionHit, header_columns
from composer.rag.text import code_ref_tag

__all__ = [
    "ComposerRAGDB", "PostgreSQLRAGDatabase", "SqliteRAGDatabase", "BlockChunk",
    "PostgresRag", "SqliteRag", "RagLocation", "parse_rag_location",
    "open_rag_db", "rag_context", "rag_build_context", "get_rag_db",
]


logger = logging.getLogger(__name__)

# tqdm tries to create a multiprocessing.RLock on first use, which calls
# fork_exec to start a resource tracker process.  In an async event loop
# with open DB connections this fails with "bad value(s) in fds_to_keep"
# and eventually hangs.  Pre-set a threading lock so tqdm never attempts
# the fork.  This is the narrowest fix: no env-var side effects, and
# sentence_transformers (which uses tqdm internally) just works.
import threading
from tqdm import tqdm as _tqdm_cls
_tqdm_cls.set_lock(threading.RLock())


@dataclass(frozen=True)
class PostgresRag:
    """A corpus in a schema of a PostgreSQL database; the role in ``conninfo`` selects it."""
    conninfo: str


@dataclass(frozen=True)
class SqliteRag:
    """A corpus in a single file (see :mod:`composer.rag.sqlite`)."""
    path: pathlib.Path


type RagLocation = PostgresRag | SqliteRag


def parse_rag_location(spec: str) -> RagLocation:
    """The location a ``--rag-db``/``--output`` argument names: a PostgreSQL connection URI, or
    otherwise the path of a corpus file."""
    if spec.startswith(("postgresql://", "postgres://")):
        return PostgresRag(spec)
    return SqliteRag(pathlib.Path(spec))


def default_corpus_dir() -> pathlib.Path:
    configured = os.environ.get("CERTORA_AI_COMPOSER_RAG_DIR")
    if configured:
        return pathlib.Path(configured)
    data_home = os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share"
    return pathlib.Path(data_home) / "autoprover" / "rag"


#: Where the built-in corpora live: baked into the images (which set CERTORA_AI_COMPOSER_RAG_DIR),
#: and built on a dev host by the scripts/populate_*.sh scripts.
RAG_CORPUS_DIR = default_corpus_dir()
#: The CVL manual.
CVL_CORPUS = SqliteRag(RAG_CORPUS_DIR / "cvl.sqlite")
#: The CVL manual plus the prover and user-guide docs, for the sanity analyzer.
EXTENDED_CORPUS = SqliteRag(RAG_CORPUS_DIR / "extended.sqlite")
#: The Foundry cheatcode reference.
FOUNDRY_CORPUS = SqliteRag(RAG_CORPUS_DIR / "foundry.sqlite")
#: The CVLR (Solana) manual.
CVLR_CORPUS = SqliteRag(RAG_CORPUS_DIR / "cvlr.sqlite")

# Logical knowledge-base tag -> default corpus location, for corpora ingested by the generic
# importer (`composer.scripts.rag_import`). The tag is the one the manifest carries (== a wheel's
# `rag_db_default`), so the import target and the runtime search tools resolve by one name —
# `composer.tools.rag_env` requires both halves before a tag is usable. Empty until the first such
# corpus lands with the application that declares it; the CVL/Foundry builders use the constants
# above instead.
KNOWLEDGE_BASES: dict[str, RagLocation] = {}



type RagConnection = str | AsyncConnectionPool[AsyncConnection[TupleRow]]

class PostgreSQLRAGDatabase(ComposerRAGDB):
    """Handle PostgreSQL database operations for RAG"""

    def __init__(self, conn_string: RagConnection, model: SentenceTransformer):
        super().__init__(model)
        if isinstance(conn_string, str):
            self._pool: AsyncConnectionPool[AsyncConnection[TupleRow]] = AsyncConnectionPool(
                conninfo=conn_string,
                kwargs={"autocommit": True},
                connection_class=AsyncConnection[TupleRow],
                open=False,
                min_size=1,
                max_size=1,
            )
            self._owns_pool = True
        else:
            self._pool = conn_string
            self._owns_pool = False
        self._opened = False

    async def _ensure_open(self) -> None:
        if self._owns_pool and not self._opened:
            await self._pool.open()
            self._opened = True

    @asynccontextmanager
    async def _get_connection(self) -> AsyncIterator[AsyncConnection[TupleRow]]:
        await self._ensure_open()
        async with self._pool.connection() as conn:
            yield conn

    @override
    async def aclose(self) -> None:
        if self._owns_pool and self._opened:
            await self._pool.close()
            self._opened = False

    @override
    async def open(self) -> None:
        await self.test_connection()

    async def test_connection(self) -> None:
        """Test database connection and setup"""
        try:
            async with self._get_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    logger.info("✅ Database connection successful")

                    # Check if documents table exists
                    await cur.execute("""
                        SELECT 1 FROM information_schema.tables
                        WHERE table_name = 'documents'
                    """)
                    if not await cur.fetchone():
                        logger.warning("❌ Documents table not found, creating...")
                        async with conn.transaction():
                            await self._create_schema(cur)
                    else:
                        logger.info("✅ Documents table found")

        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise

    async def _create_schema(self, cur: AsyncCursor) -> None:
        """Create database schema"""
        # create vector extension
        await cur.execute("""
            CREATE EXTENSION IF NOT EXISTS vector;
        """)

        # Create documents table
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                embedding vector(768) NOT NULL,
                h1 TEXT,
                h2 TEXT,
                h3 TEXT,
                h4 TEXT,
                h5 TEXT,
                h6 TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # Create indexes
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS documents_embedding_idx
            ON documents USING hnsw (embedding vector_cosine_ops);
        """)

        await cur.execute("""
            CREATE INDEX IF NOT EXISTS section_h1 ON documents (h1);
            CREATE INDEX IF NOT EXISTS section_h2 ON documents (h2);
            CREATE INDEX IF NOT EXISTS section_h3 ON documents (h3);
            CREATE INDEX IF NOT EXISTS section_h4 ON documents (h4);
        """)

        await cur.execute("""
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            CREATE TABLE IF NOT EXISTS manual_sections(
                id SERIAL PRIMARY KEY,
                content TEXT,
                h1 TEXT,
                h2 TEXT,
                h3 TEXT,
                h4 TEXT,
                h5 TEXT,
                h6 TEXT,
                part INTEGER,
                created_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT parts_unique UNIQUE (h1, h2, h3, h4, h5, h6, part)
            );

            CREATE INDEX IF NOT EXISTS manual_ts_idx ON manual_sections USING gin(
                to_tsvector('english', content)
            );
            CREATE INDEX IF NOT EXISTS manual_trgm_idx ON manual_sections USING gin(
                content gin_trgm_ops
            );

            CREATE TABLE IF NOT EXISTS manual_section_code_refs(
                id INTEGER,
                code_body TEXT,
                section_id INTEGER,
                CONSTRAINT id_section_id_pk PRIMARY KEY(id, section_id),
                CONSTRAINT section_id_manual_section_fk FOREIGN KEY(section_id) REFERENCES manual_sections(id)
            );
        """)

        await cur.execute("CREATE INDEX IF NOT EXISTS documents_content_idx ON documents USING gin(to_tsvector('english', content));")

        logger.info("✅ Database schema created successfully")

    @override
    async def add_manual_section(self, ch: BlockChunk):
        async with self._get_connection() as conn:
            async with conn.transaction():
                headers = header_columns(ch.headers)
                data = (ch.chunk,) + headers + (ch.part,)
                cur = await conn.execute("""
                    INSERT INTO manual_sections(
                        content, h1, h2, h3, h4, h5, h6, part
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, data)
                insert_res = await cur.fetchone()
                if insert_res is None:
                    raise Exception("Insertion didn't return ID")
                payloads = []
                for (i, code) in enumerate(ch.code_refs):
                    payloads.append((i, code, insert_res[0]))
                async with conn.cursor() as cur:
                    await cur.executemany("""
                        INSERT INTO manual_section_code_refs(
                            id, code_body, section_id
                        ) VALUES (%s, %s, %s)
                    """, payloads)

    @override
    async def add_chunks_batch(self, chunks: list[BlockChunk]) -> None:
        """Add chunks to database in batches"""
        if not chunks:
            return
        # SentenceTransformer.encode_document is CPU-bound (runs the embedding model);
        # offload to a thread to avoid blocking the event loop.
        embeddings = await self.embed_docs(chunks)

        logger.info(f"Adding {len(chunks)} chunks to database...")
        # Insert batch
        async with self._get_connection() as conn, conn.transaction(), conn.cursor() as cur:
            for chunk, embedding in zip(chunks, embeddings):
                try:
                    headers = header_columns(chunk.headers)
                    to_store_text = chunk.chunk
                    for (i, cr) in enumerate(chunk.code_refs):
                        to_store_text = to_store_text.replace(code_ref_tag(i), cr)
                    payload = (to_store_text, embedding.tolist()) + headers
                    await cur.execute("""
                        INSERT INTO documents
                        (content, embedding, h1, h2, h3, h4, h5, h6)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, payload)
                    insert_res = await cur.fetchone()
                    if insert_res is None:
                        raise Exception("Insertion didn't return any data")
                except Exception as e:
                    logger.error(f"Failed to insert chunk {chunk}: {e}")
                    continue

    @override
    async def find_refs(self, query: str, similarity_cutoff: float = 0.5, top_k: int = 10, manual_section: Sequence[str] | None = None) -> list[ManualRef]:
        # SentenceTransformer.encode_query is CPU-bound (runs the embedding model);
        # offload to a thread to avoid blocking the event loop.
        question_embedding = await self.embed_query(query)

        params: tuple[Any, ...] = (question_embedding.tolist(),)
        where_clause = ""
        if manual_section:
            clauses = []
            for i in range(1, 7):
                params = params + (list(manual_section),)
                clauses.append(f"h{i} = ANY(%s)")
            where_clause = "WHERE (" + " OR ".join(clauses) + ")"
        params += (question_embedding.tolist(), top_k)
        async with self._get_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"""
                    SELECT id, content, 1 - (embedding <=> %s::vector) AS cosine_similarity, h1, h2, h3, h4, h5, h6
                    FROM documents
                    {where_clause}
                    ORDER BY embedding <=> %s::vector ASC
                    LIMIT %s
                """, params)

                res = await cur.fetchall()
                to_ret = []

                for row in res:
                    body : str = row[1]
                    similarity = row[2]
                    if similarity < similarity_cutoff:
                        break
                    header: list[str] = []
                    for i in row[3:]:
                        if i is None:
                            break
                        assert isinstance(i, str)
                        header.append(i)
                    to_ret.append(ManualRef(headers=header, content=body, similarity=similarity))
                return to_ret

    async def _replace_manual_code_refs(self, cur: AsyncCursor[TupleRow], content: str, section_id: int) -> str:
        await cur.execute(
            "SELECT id, code_body FROM manual_section_code_refs WHERE section_id = %s",
            (section_id,)
        )
        async for row in cur:
            content = content.replace(code_ref_tag(row[0]), row[1])
        return content

    @override
    async def search_manual_keywords(self, query: str, *, min_depth: int = 0, limit: int = 10) -> list[ManualSectionHit]:
        if min_depth < 0 or min_depth > 6:
            raise ValueError("min_depth must be between 0 and 6")
        depth_clause = f"AND h{min_depth} IS NOT NULL" if min_depth > 0 else ""
        depth_clause = cast(LiteralString, depth_clause)
        async with self._get_connection() as conn:
            cur = await conn.execute(f"""
                SELECT ts_rank(to_tsvector('english', content), websearch_to_tsquery('english', %s)) AS relevance,
                        h1, h2, h3, h4, h5, h6
                FROM manual_sections
                WHERE to_tsvector('english', content) @@ websearch_to_tsquery('english', %s)
                {depth_clause}
                ORDER BY relevance DESC
                LIMIT %s
            """, (query, query, limit))
            results = []
            for row in await cur.fetchall():
                headers = [h for h in row[1:7] if h is not None]
                results.append(ManualSectionHit(headers=headers, relevance=row[0]))
            return results

    @override
    async def get_manual_section(self, headers: list[str]) -> str | None:
        async with self._get_connection() as conn:
            cur = await conn.execute("""
                SELECT id, content, part
                FROM manual_sections
                WHERE h1 IS NOT DISTINCT FROM %s
                    AND h2 IS NOT DISTINCT FROM %s
                    AND h3 IS NOT DISTINCT FROM %s
                    AND h4 IS NOT DISTINCT FROM %s
                    AND h5 IS NOT DISTINCT FROM %s
                    AND h6 IS NOT DISTINCT FROM %s
                ORDER BY part ASC
            """, header_columns(headers))
            rows = await cur.fetchall()
            if not rows:
                return None
            parts = []
            for row in rows:
                parts.append(await self._replace_manual_code_refs(cur, row[1], row[0]))
            return "\n".join(parts)


def open_rag_db(location: RagLocation, model: SentenceTransformer) -> ComposerRAGDB:
    """A handle on the corpus at ``location`` that connects on first use. The caller owns it and
    must ``aclose`` it; prefer :func:`rag_context`, which also fails fast on an unusable corpus."""
    match location:
        case PostgresRag(conninfo):
            return PostgreSQLRAGDatabase(conninfo, model)
        case SqliteRag(path):
            return SqliteRAGDatabase.reader(path, model)


@asynccontextmanager
async def rag_context(
    location: RagLocation, model: SentenceTransformer
) -> AsyncIterator[ComposerRAGDB]:
    """The corpus at ``location``, opened (so an unusable corpus fails here) and closed on exit."""
    db = open_rag_db(location, model)
    try:
        await db.open()
        yield db
    finally:
        await db.aclose()


@asynccontextmanager
async def rag_build_context(
    location: RagLocation, model: SentenceTransformer
) -> AsyncIterator[ComposerRAGDB]:
    """The corpus at ``location``, for ingesting into. A PostgreSQL corpus is appended to in place
    (creating its tables if missing); a corpus file is rebuilt from scratch and replaces the
    existing file only once the block exits normally."""
    match location:
        case PostgresRag():
            async with rag_context(location, model) as db:
                yield db
        case SqliteRag(path):
            async with SqliteRAGDatabase.build(path, model) as db:
                yield db


async def get_rag_db(location: RagLocation, model: SentenceTransformer) -> ComposerRAGDB:
    """Like :func:`rag_context`, for a process that holds the corpus open until it exits."""
    db = open_rag_db(location, model)
    await db.open()
    return db
