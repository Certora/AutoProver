"""Descriptor-driven RAG toolset selection for Rust applications.

A wheel declares ``rag_db_default`` in its :class:`AppDescriptor` (e.g. ``"crucible_kb"``); the
generic env builder looks that tag up here and binds the corresponding corpus's search tools onto
the author's env — the same wiring the old ``build_crucible_env`` did, now selected by tag rather
than hard-coded per application.

Like the ecosystem registry, this maps a declarative tag → a concrete toolset; it is not an
application fork (the tool classes live in ``composer/tools/<corpus>_rag.py``, shared, exactly as
``foundry_rag`` is). The tag's *connection* is not repeated here: it comes from
``composer.rag.db.KNOWLEDGE_BASES``, the same map the RAG importer targets, so a corpus is imported
and searched under one name.

Two failure modes, deliberately opposite:

* **An unregistered tag is a wheel bug.** Nothing will make that corpus appear, and degrading
  silently hides the typo behind a plausible-looking run. :func:`validate_rag_db` runs at descriptor
  load (:func:`composer.rustapp.host.build_application`) so it fails before the run spends anything
  — the same treatment an unknown ecosystem gets.
* **An unavailable corpus is an environment condition** — the DB isn't up, the embedding model
  isn't installed. A search aid must never fail a run over that, so :func:`build_rag_tools` degrades
  to *no RAG* (the static cheat-sheet in the prompt suffices).
"""

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from composer.rag.db import ComposerRAGDB

_log = logging.getLogger(__name__)

#: A corpus's search-tool factory. Every import a factory needs is local to it, so the generic host
#: pulls in a corpus module only when a descriptor actually selects that corpus.
type _ToolsFactory = Callable[["ComposerRAGDB"], "Iterable[BaseTool]"]


def _crucible_tools(db: "ComposerRAGDB") -> "Iterable[BaseTool]":
    from composer.tools.crucible_rag import get_tools

    return get_tools(db)


_FACTORIES: dict[str, _ToolsFactory] = {
    "crucible_kb": _crucible_tools,
}


def validate_rag_db(rag_db: str | None) -> None:
    """Raise if ``rag_db`` names no registered corpus; ``None`` (a wheel declaring none) is fine.

    A tag needs both halves to be usable — search tools here and a connection in
    ``KNOWLEDGE_BASES`` — so both are checked."""
    if rag_db is None:
        return
    from composer.rag.db import KNOWLEDGE_BASES

    if rag_db not in _FACTORIES or rag_db not in KNOWLEDGE_BASES:
        raise ValueError(
            f"the application declares rag_db_default={rag_db!r}, which is not a registered RAG "
            f"corpus (known: {sorted(_FACTORIES.keys() & KNOWLEDGE_BASES.keys())}). Register the "
            "tag in composer.rag.db.KNOWLEDGE_BASES (its connection) and composer.tools.rag_env "
            "(its search tools)."
        )


def build_rag_tools(rag_db: str) -> "tuple[BaseTool, ...]":
    """Search tools for the declared corpus, or ``()`` if it can't be opened (best-effort — the
    author still has the static cheat-sheet). An unregistered tag raises; see the module docstring
    for why the two are treated differently."""
    validate_rag_db(rag_db)
    try:
        from composer.rag.db import KNOWLEDGE_BASES, PostgreSQLRAGDatabase
        from composer.rag.models import get_model

        # Lazy pool — opens on first search; the DB must already be populated.
        db = PostgreSQLRAGDatabase(KNOWLEDGE_BASES[rag_db], get_model())
        return tuple(_FACTORIES[rag_db](db))
    except Exception as e:  # noqa: BLE001 — RAG is optional; the cheat-sheet suffices
        _log.warning("RAG %r unavailable (%s); using the static cheat-sheet only", rag_db, e)
        return ()
