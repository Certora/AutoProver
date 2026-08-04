#      The Certora Prover
#      Copyright (C) 2025  Certora Ltd.
#
#      This program is free software: you can redistribute it and/or modify
#      it under the terms of the GNU General Public License as published by
#      the Free Software Foundation, version 3 of the License.
#
#      This program is distributed in the hope that it will be useful,
#      but WITHOUT ANY WARRANTY; without even the implied warranty of
#      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#      GNU General Public License for more details.
#
#      You should have received a copy of the GNU General Public License
#      along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Wipe every row from the RAG PostgreSQL tables.

Hardcodes the table list (rather than truncating every table in the database)
so a stray schema or co-located table won't get clobbered. Schema is preserved;
SERIAL counters are reset.

Run with `python scripts/wipe_rag.py [--conn-string ...]`. Interactively the
script will not proceed until you type the literal confirmation sentinel — there
is no unwipe. Automated callers that wipe-then-immediately-rebuild (e.g.
`refresh_rag.sh`) can pass `--skip-confirmation` to bypass the prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

composer_dir = str(pathlib.Path(__file__).parent.parent.absolute())
if composer_dir not in sys.path:
    sys.path.append(composer_dir)

from psycopg import AsyncConnection, sql

from composer.rag.db import DEFAULT_CONNECTION


# Hardcoded so the script cannot accidentally clobber a non-RAG table that
# happens to share the database. Order matters for CASCADE-free TRUNCATE,
# but we use CASCADE below so dependent tables get cleaned regardless;
# RESTART IDENTITY resets the SERIAL counters so the next ragbuild starts
# at id=1.
_TABLES = (
    "manual_section_code_refs",
    "manual_sections",
    "documents",
)

_CONFIRMATION = "Yes, delete the rag"


def _confirm(conn_string: str) -> bool:
    print()
    print("=" * 72)
    print("  ABOUT TO WIPE THE RAG DATABASE")
    print("=" * 72)
    print(f"  Connection: {conn_string}")
    print(f"  Tables that will be TRUNCATEd (RESTART IDENTITY CASCADE):")
    for t in _TABLES:
        print(f"    - {t}")
    print()
    print("  This is destructive and not recoverable. Schema stays; rows do not.")
    print("  Type EXACTLY the following confirmation to proceed:")
    print()
    print(f"      {_CONFIRMATION}")
    print()
    response = input("> ").strip()
    return response == _CONFIRMATION


async def _wipe(conn_string: str) -> None:
    async with await AsyncConnection.connect(conn_string, autocommit=False) as conn:
        async with conn.cursor() as cur:
            # Narrow the allowlist to what this connection can actually see before
            # naming anything in the statement. TRUNCATE takes no IF EXISTS, so a
            # single name the schema does not define fails the whole wipe — and the
            # set of RAG tables has changed before now. Intersecting keeps the
            # allowlist's guarantee (nothing outside it is ever truncated) without
            # coupling the script to one revision of the schema.
            await cur.execute(
                """
                SELECT c.relname FROM pg_catalog.pg_class c
                WHERE c.relkind = 'r'
                  AND pg_catalog.pg_table_is_visible(c.oid)
                  AND c.relname = ANY(%s)
                """,
                (list(_TABLES),),
            )
            present = {row[0] for row in await cur.fetchall()}
            missing = [t for t in _TABLES if t not in present]
            if missing:
                print(f"Not defined in this schema, skipping: {', '.join(missing)}")
            ordered = [t for t in _TABLES if t in present]
            if not ordered:
                print("No RAG tables found for this connection; nothing to wipe.")
                return
            # One statement for all of them — CASCADE handles the
            # manual_sections → manual_section_code_refs FK chain, and RESTART
            # IDENTITY resets the SERIAL counters so the next build starts at id=1.
            # Composed rather than interpolated: table names belong in the statement
            # as identifiers, and the resolved list is not a literal.
            statement = sql.SQL(
                "TRUNCATE TABLE {} RESTART IDENTITY CASCADE"
            ).format(sql.SQL(", ").join(sql.Identifier(t) for t in ordered))
            print(f"TRUNCATE TABLE {', '.join(ordered)} RESTART IDENTITY CASCADE")
            await cur.execute(statement)
        await conn.commit()
    print()
    print("Done. RAG tables are empty.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wipe all rows from the RAG PostgreSQL tables (schema preserved).",
    )
    parser.add_argument(
        "--conn-string",
        default=DEFAULT_CONNECTION,
        help=f"PostgreSQL connection string (default: {DEFAULT_CONNECTION})",
    )
    parser.add_argument(
        "--skip-confirmation",
        action="store_true",
        help="Skip the interactive confirmation prompt. For automated "
             "wipe-then-rebuild callers (e.g. refresh_rag.sh); use with care.",
    )
    args = parser.parse_args()

    if not args.skip_confirmation and not _confirm(args.conn_string):
        print()
        print("Confirmation did not match. Aborting; no changes made.")
        return 1

    asyncio.run(_wipe(args.conn_string))
    return 0


if __name__ == "__main__":
    sys.exit(main())
