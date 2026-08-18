"""The common JSON manifest format for RAG corpora (see ``docs/rag-import-format.md``).

A *producer* parses a corpus's native docs and emits one :class:`RagManifest` as JSON; the shared
importer (:mod:`composer.scripts.rag_import`) reads that manifest and owns everything downstream —
chunking, embedding, ``part`` numbering, and the DB ingestion. So these models are the seam
between the two halves, and they are deliberately free of any RAG-stack imports (no
``composer.rag.db``, no spaCy): a producer needs only these classes to emit a corpus.

A manifest carries **two independent products**, one per retrieval index:

* :attr:`RagManifest.manual_sections` — whole documents for keyword search + exact
  ``get_section``. Never split; a section is returned in full.
* :attr:`RagManifest.embedded_groups` — input for the vector index. The importer sub-splits each
  group into length-bounded embedded chunks, guided by the per-block kinds.

The two indexes store different units (documents vs. length-bounded passages), so a producer lays
out each product explicitly — often as two views of the same source, but nothing derives one from
the other. See the design doc for why the products are separate.
"""

import enum

from pydantic import BaseModel, Field

#: The schema version the importer understands. Bumped only on a breaking change; the importer
#: refuses a manifest whose ``version`` it doesn't recognize rather than mis-ingesting it.
SCHEMA_VERSION = 1


class ManualBlockKind(str, enum.Enum):
    """The two content kinds a manual section is built from.

    Manual sections are never split, so the only distinction that matters is whether the body is
    prose or code — code must stay out of the searchable text."""

    #: Prose. Lands in the section's content verbatim.
    TEXT = "text"
    #: A code sample. Held aside in the section's ``code_refs`` with a ``<code-ref-N>``
    #: placeholder in the content, for retrieval to substitute back.
    CODE = "code"


class ManualBlock(BaseModel):
    """One ordered piece of a manual section."""

    kind: ManualBlockKind
    body: str


class EmbeddedBlockKind(str, enum.Enum):
    """The content kinds an embedded group is built from.

    These carry the chunking semantics the vector index needs: each kind maps 1:1 onto one way of
    driving the importer's ``BlockBuilder``, which decides where a length-bounded chunk may be
    cut. Producers state what a block *is*; the importer owns how it chunks."""

    #: A self-contained prose unit (a paragraph). Chunk cuts prefer its boundaries; an overlong
    #: body is split at sentence boundaries rather than mid-sentence.
    PARAGRAPH = "paragraph"
    #: Structure that must survive intact — tables, lists, anything whose lines are not
    #: sentences. Never sentence-split: an overlong body becomes one oversized chunk rather than
    #: being cut.
    ATOMIC = "atomic"
    #: Prose that resumes the stream an earlier block interrupted (e.g. the tail of a sentence
    #: around an inline code sample). No boundary preference; may be cut at any sentence end.
    CONTINUATION = "continuation"
    #: A code sample. Stays atomic and is never embedded as prose: the body is held aside in the
    #: chunk's ``code_refs`` and a ``<code-ref-N>`` placeholder takes its place in the chunk text.
    CODE = "code"


class EmbeddedBlock(BaseModel):
    """One ordered piece of an embedded group."""

    kind: EmbeddedBlockKind
    body: str


class _HeaderPath(BaseModel):
    """Shared shape of both products: a header path labelling the content.

    ``headers`` is the ``h1..h6`` path: entry *i* lands in column ``h(i+1)``, and a falsy or
    absent level stays ``NULL`` in its own column (the DB's ``_normalize_head`` packs nothing).
    At most 6 — a deeper path raises there rather than losing its deepest level. Both retrieval
    indexes are keyed off this path."""

    headers: list[str]


class ManualSection(_HeaderPath):
    """A whole document for the manual index: keyword search hits it, ``get_section`` returns it
    in full under its header path. Any size — the importer never splits it."""

    blocks: list[ManualBlock] = Field(default_factory=list)


class EmbeddedGroup(_HeaderPath):
    """A run of blocks the importer chunks together for the vector index. Every resulting
    length-bounded chunk is labelled with the group's header path."""

    blocks: list[EmbeddedBlock] = Field(default_factory=list)


class RagManifest(BaseModel):
    """A whole corpus: metadata + the two retrieval products, serialized as one JSON document."""

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
    #: Documents for keyword search + ``get_section``.
    manual_sections: list[ManualSection] = Field(default_factory=list)
    #: Input for the vector index, chunked by the importer.
    embedded_groups: list[EmbeddedGroup] = Field(default_factory=list)
