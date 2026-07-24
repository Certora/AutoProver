"""Generic RAG importer — ingest any corpus described by a common JSON manifest.

This is the shared back half of the RAG build, factored out of the old per-corpus builders (see
``docs/rag-import-format.md``): it reads one or more :class:`~composer.rag.import_format.RagManifest`
documents and owns everything downstream — length-bounded chunking (``BlockBuilder``), embedding,
``part`` numbering, and the *dual-path* ingestion every corpus wants:

* ``add_chunks_batch`` — length-bounded embedded chunks for **vector** (semantic) search;
* ``add_manual_section`` — the full section for **keyword** search + exact ``get_section``.

Both paths are always populated — there is no per-section knob; "ingest a corpus" means feed both
indexes. A *producer* does the corpus-specific parsing and emits the manifest; this module is
corpus-agnostic. Crucible's corpus is a committed manifest (``rust/crucible-app/crucible_kb.rag.json``).

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
from composer.rag.import_format import RagManifest, Section, SCHEMA_VERSION
from composer.rag.models import get_model
from composer.rag.text import code_ref_tag
from composer.rag.types import BlockChunk
from composer.scripts.text_processors import BlockBuilder, BuilderConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_BATCH_SIZE = 50


def _manual_chunk(sec: Section) -> BlockChunk:
    """One full-section chunk (code as ``<code-ref-N>`` tags) for keyword / get-section."""
    parts: list[str] = []
    code_refs: list[str] = []
    for b in sec.blocks:
        if b.kind == "code":
            parts.append(code_ref_tag(len(code_refs)))
            code_refs.append(b.body)
        else:
            parts.append(b.body)
    return BlockChunk(headers=list(sec.headers), part=0, code_refs=code_refs, chunk="\n\n".join(parts))


def _embedded_chunks(sec: Section, config: BuilderConfig) -> list[BlockChunk]:
    """Length-bounded embedded chunks for vector search, via the shared builder."""
    builder = BlockBuilder(header=list(sec.headers), config=config)
    for b in sec.blocks:
        if b.kind == "code":
            builder.add_code(b.body)
        else:
            builder.append_text(b.body, is_structured_boundary=True, unbreakable=False)
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
    """Dry-run: render each section's full-section chunk to stdout, no DB writes."""
    print(f"=== knowledge_base: {manifest.knowledge_base}  (source: {manifest.source})")
    for s in manifest.sections:
        print(f"\n#### {' / '.join(h for h in s.headers if h)}")
        print(_manual_chunk(s).chunk[:500])


async def _ingest(
    db, manifest: RagManifest, config: BuilderConfig, seen_paths: dict[tuple[str, ...], int]
) -> tuple[int, int]:
    """Ingest one manifest's sections into ``db``, feeding both indexes. ``seen_paths`` is shared
    across manifests targeting the same DB so the ``manual_sections`` ``(headers, part)`` unique
    key never collides."""
    buffer: list[BlockChunk] = []
    n_docs = n_manual = 0
    for s in manifest.sections:
        buffer.extend(_embedded_chunks(s, config))
        if len(buffer) >= _BATCH_SIZE:
            await db.add_chunks_batch(buffer)
            n_docs += len(buffer)
            buffer = []
        manual = _manual_chunk(s)
        key = tuple(manual.headers)
        manual.part = seen_paths.get(key, 0)
        seen_paths[key] = manual.part + 1
        await db.add_manual_section(manual)
        n_manual += 1
    if buffer:
        await db.add_chunks_batch(buffer)
        n_docs += len(buffer)
    return n_docs, n_manual


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
    parser.add_argument("--print", action="store_true", help="Dry-run: print sections, no DB writes.")
    asyncio.run(_async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
