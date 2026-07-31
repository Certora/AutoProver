"""Descriptor-driven RAG toolset selection for Rust applications.

A wheel declares ``rag_db_default`` in its :class:`AppDescriptor` (e.g. ``"crucible_kb"``); the
generic env builder looks that tag up here and binds the corresponding corpus's search tools onto
the author's env — the same wiring the old ``build_crucible_env`` did, now selected by tag rather
than hard-coded per application.

Like the ecosystem registry, this maps a declarative tag → a concrete toolset; it is not an
application fork (the tool classes live in ``composer/tools/<corpus>_rag.py``, shared, exactly as
``foundry_rag`` is). A search aid must never fail a run, so an unknown tag or an unavailable
DB / embedding model degrades to *no RAG* (the static cheat-sheet in the prompt suffices).
"""

import logging

_log = logging.getLogger(__name__)


def _resolve(rag_db: str):
    """(connection string, tools factory) for a declared RAG tag. Imports are local so the
    generic host never pulls a corpus module unless a descriptor actually selects it."""
    from composer.rag.db import CRUCIBLE_DEFAULT_CONNECTION
    from composer.tools.crucible_rag import get_tools as crucible_tools

    registry = {
        "crucible_kb": (CRUCIBLE_DEFAULT_CONNECTION, crucible_tools),
    }
    if rag_db not in registry:
        raise KeyError(f"no RAG toolset registered for {rag_db!r} (available: {sorted(registry)})")
    return registry[rag_db]


def build_rag_tools(rag_db: str) -> tuple:
    """Search tools for the declared corpus, or ``()`` if it can't be opened (best-effort — the
    author still has the static cheat-sheet)."""
    try:
        from composer.rag.db import PostgreSQLRAGDatabase
        from composer.rag.models import get_model

        conn, get_tools = _resolve(rag_db)
        # Lazy pool — opens on first search; the DB must already be populated.
        db = PostgreSQLRAGDatabase(conn, get_model())
        return tuple(get_tools(db))
    except Exception as e:  # noqa: BLE001 — RAG is optional; the cheat-sheet suffices
        _log.warning("RAG %r unavailable (%s); using the static cheat-sheet only", rag_db, e)
        return ()
