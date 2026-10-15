"""Emptying a RAG corpus before it is rebuilt (``composer.rag.wipe``).

Ingest only inserts, so a corpus refilled without being emptied holds every row twice. The CVLR
corpora's schemas have no ``code_refs`` table, and a truncate that named one failed outright, leaving
every row in place, and the rebuild after it tripled them.
"""

import asyncio
import uuid

import psycopg
import pytest

from composer.rag.wipe import empty_corpus

_CVLR_SCHEMA = """
CREATE TABLE documents (id SERIAL PRIMARY KEY, content TEXT);
CREATE TABLE manual_sections (id SERIAL PRIMARY KEY, content TEXT);
CREATE TABLE manual_section_code_refs (
    id INT, code_body TEXT, section_id INT REFERENCES manual_sections(id)
);
CREATE TABLE unrelated (note TEXT);
INSERT INTO documents (content) VALUES ('a'), ('b');
INSERT INTO manual_sections (content) VALUES ('s');
INSERT INTO manual_section_code_refs VALUES (0, 'code', 1);
INSERT INTO unrelated VALUES ('kept');
"""


@pytest.fixture
def corpus_url(pg_container):
    if pg_container is None:
        pytest.skip("no Postgres available")
    name = "wipe_" + uuid.uuid4().hex[:12]
    admin = pg_container.get_connection_url(driver=None)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    base = admin.rsplit("/", 1)[0]
    url = f"{base}/{name}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(_CVLR_SCHEMA)
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


def test_a_schema_without_code_refs_is_emptied_and_nothing_else_is(corpus_url):
    emptied = asyncio.run(empty_corpus(corpus_url))
    assert emptied == ("manual_section_code_refs", "manual_sections", "documents")
    for table in emptied:
        assert _count(corpus_url, table) == 0
    assert _count(corpus_url, "unrelated") == 1


def test_identity_restarts_so_a_rebuild_numbers_from_one(corpus_url):
    asyncio.run(empty_corpus(corpus_url))
    with psycopg.connect(corpus_url, autocommit=True) as conn:
        row = conn.execute("INSERT INTO documents (content) VALUES ('c') RETURNING id").fetchone()
    assert row == (1,)
