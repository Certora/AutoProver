"""Empty a RAG corpus before it is rebuilt.

Ingest is a plain ``INSERT``, with no ``ON CONFLICT`` and no truncate (``docs/cvlr-api-docs-plan.md``),
so re-ingesting into a populated corpus duplicates every row, and an exact lookup then returns each
entry once per copy. A corpus is therefore emptied and rebuilt, never ingested on top of.
"""

from psycopg.connection_async import AsyncConnection
from psycopg.sql import SQL, Identifier

#: Hardcoded so a stray table that happens to share the database cannot be clobbered. CASCADE
#: covers the foreign keys between them, and RESTART IDENTITY resets the SERIAL counters so the
#: next build starts at id 1.
RAG_TABLES = (
    "manual_section_code_refs",
    "manual_sections",
    "code_refs",
    "documents",
)


async def empty_corpus(conn_string: str) -> tuple[str, ...]:
    """Truncate the RAG tables in the connection's schema, and return the ones truncated.

    Only the tables that schema has: the CVLR corpora have no ``code_refs``, and naming it would
    fail the statement, whose rollback would leave every row in place.
    """
    async with await AsyncConnection.connect(conn_string, autocommit=False) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema() AND tablename = ANY(%s)",
                (list(RAG_TABLES),),
            )
            present = {row[0] for row in await cur.fetchall()}
            tables = tuple(t for t in RAG_TABLES if t in present)
            if tables:
                await cur.execute(
                    SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                        SQL(", ").join(Identifier(t) for t in tables)
                    )
                )
        await conn.commit()
    return tables
