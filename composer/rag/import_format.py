"""The common JSON manifest format for RAG corpora (see ``docs/rag-import-format.md``).

A *producer* parses a corpus's native docs and emits one :class:`RagManifest` as JSON; the shared
importer (:mod:`composer.scripts.rag_import`) reads that manifest and owns everything downstream —
chunking, embedding, ``part`` numbering, and the dual-path DB ingestion. So these models are the
seam between the two halves, and they are deliberately free of any RAG-stack imports (no
``composer.rag.db``, no spaCy): a producer needs only these classes to emit a corpus.

The cut is *logical sections*, not pre-chunked rows — a producer decides section boundaries (the
one genuinely corpus-specific editorial choice) and the importer sub-splits each section by
length. See the design doc for why this is the right cut.
"""

import enum
from typing import Literal

from pydantic import BaseModel, Field

#: The schema version the importer understands. Bumped only on a breaking change; the importer
#: refuses a manifest whose ``version`` it doesn't recognize rather than mis-ingesting it.
SCHEMA_VERSION = 1


class BlockKind(str, enum.Enum):
    """The two content kinds a section is built from."""

    TEXT = "text"
    CODE = "code"


class Block(BaseModel):
    """One ordered piece of a section: prose (``text``) or a code sample (``code``).

    Maps 1:1 onto the two ``BlockBuilder`` operations the importer drives — ``text`` →
    ``append_text`` (spaCy-split prose), ``code`` → ``add_code`` (gets a ``<code-ref-N>`` tag
    assigned by the importer, so producers never touch the tag scheme)."""

    kind: Literal["text", "code"]
    body: str


class Section(BaseModel):
    """A logical documentation section: a header path plus its ordered content blocks.

    ``headers`` is the ``h1..h6`` path (the importer left-packs and truncates to 6, matching the
    DB's ``_normalize_head``). Both retrieval indexes are keyed off this path."""

    headers: list[str]
    blocks: list[Block] = Field(default_factory=list)


class RagManifest(BaseModel):
    """A whole corpus: metadata + an ordered list of sections, serialized as one JSON document."""

    #: Schema version; must match :data:`SCHEMA_VERSION`. Defaults so a hand-written manifest can
    #: omit it, but the importer still validates any value present.
    version: int = SCHEMA_VERSION
    #: Logical corpus tag — the *same* string a wheel declares as ``rag_db_default`` and that
    #: ``rag_env.py`` resolves to search tools. The importer resolves it to a DB connection via
    #: ``composer.rag.db.KNOWLEDGE_BASES`` (overridable by ``--output``).
    knowledge_base: str
    #: Free-text provenance (source repo/commit/glob). For logs only — not persisted per row
    #: (the DB schema is header-only).
    source: str | None = None
    sections: list[Section] = Field(default_factory=list)
