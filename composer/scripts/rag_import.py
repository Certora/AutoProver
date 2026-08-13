"""Generic RAG importer — ingest any corpus described by a common JSON manifest.

This is the shared back half of the RAG build (see ``docs/rag-import-format.md``): it reads one or
more :class:`~composer.rag.import_format.RagManifest` documents and owns everything downstream of
the manifest — length-bounded chunking (``BlockBuilder``), embedding, ``part`` numbering, and the
DB writes. A manifest declares the two retrieval products separately, and each feeds exactly its
own index:

* ``embedded_groups`` → ``add_chunks_batch`` — length-bounded embedded chunks for **vector**
  (semantic) search, cut according to each block's declared kind;
* ``manual_sections`` → ``add_manual_section`` — whole documents for **keyword** search + exact
  ``get_section``, never split.

A *producer* does the corpus-specific parsing and emits the manifest; this module is
corpus-agnostic — an application ships its corpus as a committed ``<knowledge_base>.rag.json`` and
nothing about it lives here.

Run under the ragbuild uv group (has spaCy + sentence-transformers)::

    uv run --isolated --group ragbuild python -m composer.scripts.rag_import \\
        corpus.rag.json [more.rag.json ...] [--output <conn>] [--max-length N] [--print]
"""

import argparse
import asyncio
import logging
import pathlib
from collections import defaultdict

import spacy

from composer.rag.db import KNOWLEDGE_BASES, get_rag_db
from composer.rag.import_format import (
    EmbeddedBlockKind,
    EmbeddedGroup,
    ManualBlockKind,
    ManualSection,
    RagManifest,
    SCHEMA_VERSION,
)
from composer.rag.models import get_model
from composer.rag.text import code_ref_tag
from composer.rag.types import BlockChunk
from composer.scripts.text_processors import BlockBuilder, BuilderConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_BATCH_SIZE = 50


def _manual_chunk(sec: ManualSection) -> BlockChunk:
    """The whole section as one chunk (code as ``<code-ref-N>`` tags) for keyword / get-section."""
    parts: list[str] = []
    code_refs: list[str] = []
    for b in sec.blocks:
        if b.kind is ManualBlockKind.CODE:
            parts.append(code_ref_tag(len(code_refs)))
            code_refs.append(b.body)
        else:
            parts.append(b.body)
    return BlockChunk(headers=list(sec.headers), part=0, code_refs=code_refs, chunk="\n\n".join(parts))


def _embedded_chunks(group: EmbeddedGroup, config: BuilderConfig) -> list[BlockChunk]:
    """Length-bounded embedded chunks for vector search, cut as each block's kind dictates."""
    builder = BlockBuilder(header=list(group.headers), config=config)
    for b in group.blocks:
        match b.kind:
            case EmbeddedBlockKind.CODE:
                builder.add_code(b.body)
            case EmbeddedBlockKind.PARAGRAPH:
                builder.append_text(b.body, is_structured_boundary=True, unbreakable=False)
            case EmbeddedBlockKind.ATOMIC:
                builder.append_text(b.body, is_structured_boundary=True, unbreakable=True)
            case EmbeddedBlockKind.CONTINUATION:
                builder.append_text(b.body, is_structured_boundary=False, unbreakable=False)
    return list(builder.finish())


def _load_manifest(path: pathlib.Path) -> RagManifest:
    manifest = RagManifest.model_validate_json(path.read_text())
    if manifest.version != SCHEMA_VERSION:
        raise SystemExit(
            f"{path}: unsupported manifest version {manifest.version} (this importer speaks "
            f"v{SCHEMA_VERSION}). Regenerate the manifest with a matching producer."
        )
    return manifest


def _resolve_output(manifest: RagManifest, override: str | None) -> str:
    if override:
        return override
    conn = KNOWLEDGE_BASES.get(manifest.knowledge_base)
    if conn is None:
        raise SystemExit(
            f"no connection registered for knowledge_base {manifest.knowledge_base!r} "
            f"(known: {sorted(KNOWLEDGE_BASES)}). Add it to composer.rag.db.KNOWLEDGE_BASES "
            f"or pass --output <conn>."
        )
    return conn


def _print_manifest(manifest: RagManifest) -> None:
    """Dry-run: render each manual section and name each embedded group, no DB writes."""
    print(f"=== knowledge_base: {manifest.knowledge_base}  (source: {manifest.source})")
    for s in manifest.manual_sections:
        print(f"\n#### {' / '.join(h for h in s.headers if h)}")
        print(_manual_chunk(s).chunk)
    print(f"\n=== {len(manifest.embedded_groups)} embedded group(s):")
    for g in manifest.embedded_groups:
        kinds = ", ".join(b.kind.value for b in g.blocks)
        print(f"  {' / '.join(h for h in g.headers if h)}  [{kinds}]")


async def _ingest(
    db, manifest: RagManifest, config: BuilderConfig, seen_paths: dict[tuple[str, ...], int]
) -> tuple[int, int]:
    """Ingest one manifest's two products into ``db``. ``seen_paths`` is shared across manifests
    targeting the same DB so the ``manual_sections`` ``(headers, part)`` unique key never
    collides."""
    buffer: list[BlockChunk] = []
    n_docs = 0
    for g in manifest.embedded_groups:
        buffer.extend(_embedded_chunks(g, config))
        if len(buffer) >= _BATCH_SIZE:
            await db.add_chunks_batch(buffer)
            n_docs += len(buffer)
            buffer = []
    if buffer:
        await db.add_chunks_batch(buffer)
        n_docs += len(buffer)

    for s in manifest.manual_sections:
        manual = _manual_chunk(s)
        key = tuple(manual.headers)
        manual.part = seen_paths.get(key, 0)
        seen_paths[key] = manual.part + 1
        await db.add_manual_section(manual)
    return n_docs, len(manifest.manual_sections)


async def _async_main(args: argparse.Namespace) -> None:
    manifests = [_load_manifest(f) for f in args.files]

    if args.print:
        for m in manifests:
            _print_manifest(m)
        return

    config = BuilderConfig(nlp=spacy.load("en_core_web_sm"), max_length=args.max_length)
    model = get_model()

    # Group by resolved target so manifests sharing a DB share one connection + one part counter.
    groups: dict[str, list[RagManifest]] = defaultdict(list)
    for m in manifests:
        groups[_resolve_output(m, args.output)].append(m)

    for output, group in groups.items():
        db = await get_rag_db(output, model)
        seen_paths: dict[tuple[str, ...], int] = {}
        n_docs = n_manual = 0
        for m in group:
            d, mn = await _ingest(db, m, config, seen_paths)
            n_docs += d
            n_manual += mn
        logger.info(
            "ingested %d embedded chunk(s) + %d manual section(s) from %d manifest(s) into %s",
            n_docs, n_manual, len(group), output,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a RAG corpus from one or more JSON manifests.")
    parser.add_argument("files", nargs="+", type=pathlib.Path, help="RAG manifest JSON files.")
    parser.add_argument("--max-length", type=int, default=2000, help="Soft cap on embedded-chunk length (chars).")
    parser.add_argument(
        "--output", "-o", default=None,
        help="RAG DB connection string. Overrides the manifest's knowledge_base -> connection lookup.",
    )
    parser.add_argument("--print", action="store_true", help="Dry-run: print both products, no DB writes.")
    asyncio.run(_async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
