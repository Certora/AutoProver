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

import argparse
import asyncio
import pathlib
import sys

composer_dir = str(pathlib.Path(__file__).parent.parent.absolute())
if composer_dir not in sys.path:
    sys.path.append(composer_dir)

from composer.rag.db import DEFAULT_CONNECTION
from composer.rag.wipe import RAG_TABLES, empty_corpus

_CONFIRMATION = "Yes, delete the rag"


def _confirm(conn_string: str) -> bool:
    print()
    print("=" * 72)
    print("  ABOUT TO WIPE THE RAG DATABASE")
    print("=" * 72)
    print(f"  Connection: {conn_string}")
    print(f"  Tables that will be TRUNCATEd (RESTART IDENTITY CASCADE):")
    for t in RAG_TABLES:
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
    tables = await empty_corpus(conn_string)
    print()
    if tables:
        print(f"Done. Truncated {', '.join(tables)}; the RAG tables are empty.")
    else:
        print("None of the RAG tables exist in this connection's schema; nothing to do.")


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
