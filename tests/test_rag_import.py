"""The generic manifest importer (``composer.scripts.rag_import``).

The importer owns everything a producer deliberately doesn't (see ``docs/rag-import-format.md``):
each of the manifest's two products feeds exactly its own index, ``<code-ref-N>`` tags are
assigned, embedded blocks are cut as their kind dictates, and ``part`` is numbered per header
path — across sections *and* across manifests that resolve to the same DB, because
``manual_sections`` is unique on ``(h1..h6, part)``. These pin that contract without a DB.

Skips where the ``ragbuild`` group isn't installed: the importer pulls in spaCy transitively
(``text_processors``), which the routine test env doesn't otherwise need.
"""

import asyncio
import json
import pathlib

import pytest

spacy = pytest.importorskip("spacy")

from composer.rag.import_format import (  # noqa: E402
    EmbeddedBlock,
    EmbeddedGroup,
    ManualBlock,
    ManualSection,
    RagManifest,
)
from composer.scripts import rag_import  # noqa: E402


def _config(max_length: int = 2000) -> "rag_import.BuilderConfig":
    """A real ``BuilderConfig`` without a downloaded model — a blank pipeline plus the rule-based
    sentencizer is all ``BlockBuilder`` asks of ``nlp`` (it splits on ``.sents``)."""
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    return rag_import.BuilderConfig(nlp=nlp, max_length=max_length)


class _RecordingDB:
    """Records the two ingestion paths instead of writing them."""

    def __init__(self) -> None:
        self.embedded: list[rag_import.BlockChunk] = []
        self.manual: list[rag_import.BlockChunk] = []

    async def add_chunks_batch(self, chunks: list[rag_import.BlockChunk]) -> None:
        self.embedded.extend(chunks)

    async def add_manual_section(self, chunk: rag_import.BlockChunk) -> None:
        self.manual.append(chunk)


_HEADERS = ["Guide", "Topic"]


def _section(*blocks: ManualBlock, headers: list[str] | None = None) -> ManualSection:
    return ManualSection(headers=headers if headers is not None else list(_HEADERS), blocks=list(blocks))


def _group(*blocks: EmbeddedBlock, headers: list[str] | None = None) -> EmbeddedGroup:
    return EmbeddedGroup(headers=headers if headers is not None else list(_HEADERS), blocks=list(blocks))


def _manifest(
    *,
    manual: list[ManualSection] | None = None,
    embedded: list[EmbeddedGroup] | None = None,
    kb: str = "stub_kb",
) -> RagManifest:
    return RagManifest(knowledge_base=kb, manual_sections=manual or [], embedded_groups=embedded or [])


def _ingest(*manifests: RagManifest, max_length: int = 2000) -> _RecordingDB:
    """Drive ``_ingest`` over manifests sharing one DB, as the CLI does per resolved target."""
    db = _RecordingDB()
    seen: dict[tuple[str, ...], int] = {}
    config = _config(max_length)

    async def run() -> None:
        for m in manifests:
            await rag_import._ingest(db, m, config, seen)

    asyncio.run(run())
    return db


def test_each_product_feeds_exactly_its_own_index():
    db = _ingest(
        _manifest(
            manual=[_section(ManualBlock(kind="text", body="Seeds are encoded first."))],
            embedded=[_group(EmbeddedBlock(kind="paragraph", body="Seeds are encoded first."))],
        )
    )
    assert len(db.manual) == 1
    assert len(db.embedded) == 1

    manual_only = _ingest(_manifest(manual=[_section(ManualBlock(kind="text", body="x"))]))
    assert manual_only.manual and not manual_only.embedded

    embedded_only = _ingest(_manifest(embedded=[_group(EmbeddedBlock(kind="paragraph", body="x"))]))
    assert embedded_only.embedded and not embedded_only.manual


def test_the_manual_chunk_holds_the_whole_section_with_code_as_refs():
    db = _ingest(
        _manifest(
            manual=[
                _section(
                    ManualBlock(kind="text", body="Derive the address."),
                    ManualBlock(kind="code", body="let (pda, bump) = find_program_address(...);"),
                    ManualBlock(kind="text", body="Then check the bump."),
                )
            ]
        )
    )
    (manual,) = db.manual
    assert manual.chunk == (
        "Derive the address.\n\n<code-ref-0>\n\nThen check the bump."
    )
    assert manual.code_refs == ["let (pda, bump) = find_program_address(...);"]


def test_code_refs_are_numbered_per_section_not_per_manifest():
    db = _ingest(
        _manifest(
            manual=[
                _section(ManualBlock(kind="code", body="first")),
                _section(ManualBlock(kind="code", body="second"), headers=["Guide", "Other"]),
            ]
        )
    )
    assert [m.chunk for m in db.manual] == ["<code-ref-0>", "<code-ref-0>"]
    assert [m.code_refs for m in db.manual] == [["first"], ["second"]]


def test_a_repeated_header_path_bumps_part():
    db = _ingest(
        _manifest(
            manual=[
                _section(ManualBlock(kind="text", body="one")),
                _section(ManualBlock(kind="text", body="two")),
                _section(ManualBlock(kind="text", body="three"), headers=["Guide", "Elsewhere"]),
            ]
        )
    )
    assert [(tuple(m.headers), m.part) for m in db.manual] == [
        (("Guide", "Topic"), 0),
        (("Guide", "Topic"), 1),
        (("Guide", "Elsewhere"), 0),
    ]


def test_part_numbering_continues_across_manifests_sharing_a_db():
    # Two manifests, one target: the (headers, part) unique key spans both, so the counter must too.
    db = _ingest(
        _manifest(manual=[_section(ManualBlock(kind="text", body="one"))]),
        _manifest(manual=[_section(ManualBlock(kind="text", body="two"))]),
    )
    assert [m.part for m in db.manual] == [0, 1]


def test_an_overlong_paragraph_is_split_at_sentence_boundaries():
    body = " ".join(f"Sentence number {i} says something." for i in range(40))
    db = _ingest(
        _manifest(embedded=[_group(EmbeddedBlock(kind="paragraph", body=body))]), max_length=120
    )
    assert len(db.embedded) > 1
    assert all(len(c.chunk) < 240 for c in db.embedded)


def test_an_overlong_atomic_block_stays_whole():
    body = "\n".join(f"| row {i} | value {i} |" for i in range(40))
    db = _ingest(
        _manifest(embedded=[_group(EmbeddedBlock(kind="atomic", body=body))]), max_length=120
    )
    (chunk,) = db.embedded
    assert body in chunk.chunk


def test_a_manual_section_is_never_split():
    body = " ".join(f"Sentence number {i} says something." for i in range(40))
    db = _ingest(_manifest(manual=[_section(ManualBlock(kind="text", body=body))]), max_length=120)
    assert len(db.manual) == 1 and db.manual[0].chunk == body


def test_an_unknown_manifest_version_is_refused_before_any_write(tmp_path: pathlib.Path):
    path = tmp_path / "corpus.rag.json"
    payload = _manifest().model_dump()
    payload["version"] = rag_import.SCHEMA_VERSION + 1
    path.write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match="unsupported manifest version"):
        rag_import._load_manifest(path)


def test_a_valid_manifest_round_trips_through_the_loader(tmp_path: pathlib.Path):
    path = tmp_path / "corpus.rag.json"
    manifest = _manifest(
        manual=[_section(ManualBlock(kind="code", body="x"))],
        embedded=[_group(EmbeddedBlock(kind="atomic", body="| a | b |"))],
    )
    path.write_text(manifest.model_dump_json())
    loaded = rag_import._load_manifest(path)
    assert loaded.manual_sections[0].blocks[0].kind == "code"
    assert loaded.embedded_groups[0].blocks[0].kind == "atomic"


def test_an_unregistered_knowledge_base_says_where_to_register_it():
    with pytest.raises(SystemExit, match="no connection registered for knowledge_base"):
        rag_import._resolve_output(_manifest(kb="no_such_kb"), None)


def test_output_overrides_the_registry_lookup():
    # …which is what makes the importer usable before a corpus is registered at all.
    conn = "postgresql://elsewhere/rag_db"
    assert rag_import._resolve_output(_manifest(kb="no_such_kb"), conn) == conn
