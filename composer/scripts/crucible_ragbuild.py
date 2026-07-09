"""Crucible knowledge-base RAG builder — ingests the Crucible **markdown** docs.

The Crucible docs (harness-guide, writing-tests, api-reference, cli-reference, …)
are plain markdown, unlike the foundry cheatcode HTML or the CVL manual. So the
section walker here is a small self-contained markdown reader (ATX headers +
fenced code blocks) rather than BeautifulSoup, but it drives the *same* shared
chunking machinery (`BlockBuilder`) and ingests via the *same* dual path
`ragbuild.py` uses — `add_chunks_batch` (embedded, for vector search) **and**
`add_manual_section` (full section, for keyword search / get-section).

Run under the ragbuild uv group (has sentence-transformers + spaCy)::

    uv run --isolated --group ragbuild python -m composer.scripts.crucible_ragbuild \
        /path/to/crucible/docs/*.md [--print] [--output <conn>]
"""

import argparse
import asyncio
import logging
import pathlib
import re
from dataclasses import dataclass, field

import spacy

from composer.rag.db import CRUCIBLE_DEFAULT_CONNECTION, get_rag_db
from composer.rag.models import get_model
from composer.rag.text import code_ref_tag
from composer.rag.types import BlockChunk
from composer.scripts.text_processors import BlockBuilder, BuilderConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_BATCH_SIZE = 50
_MAX_HEADERS = 6  # documents/manual_sections have h1..h6
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*```")


@dataclass
class _Block:
    kind: str  # "text" | "code"
    body: str


@dataclass
class _Section:
    headers: list[str]
    blocks: list[_Block] = field(default_factory=list)


def _doc_title(path: pathlib.Path, first_h1: str | None) -> str:
    return first_h1 or path.stem.replace("-", " ").replace("_", " ").title()


def parse_markdown(path: pathlib.Path) -> list[_Section]:
    """Split a markdown file into sections keyed by their header path (the doc
    title as h1, then nested `##`/`###`…). Fenced ```code``` blocks become code
    blocks; everything else is prose."""
    lines = path.read_text().splitlines()

    first_h1: str | None = None
    for ln in lines:
        m = _HEADER_RE.match(ln)
        if m and len(m.group(1)) == 1:
            first_h1 = m.group(2).strip()
            break
    title = _doc_title(path, first_h1)

    sections: list[_Section] = []
    # header stack: list of (level, text); the path is title + these below level 1.
    stack: list[tuple[int, str]] = []
    cur = _Section(headers=[title])
    text_buf: list[str] = []
    in_code = False
    code_buf: list[str] = []

    def flush_text():
        chunk = "\n".join(text_buf).strip()
        if chunk:
            cur.blocks.append(_Block("text", chunk))
        text_buf.clear()

    def path_for_stack() -> list[str]:
        return ([title] + [t for _, t in stack])[:_MAX_HEADERS]

    for ln in lines:
        if _FENCE_RE.match(ln):
            if in_code:
                cur.blocks.append(_Block("code", "\n".join(code_buf)))
                code_buf.clear()
                in_code = False
            else:
                flush_text()
                in_code = True
            continue
        if in_code:
            code_buf.append(ln)
            continue

        m = _HEADER_RE.match(ln)
        if m:
            level, htext = len(m.group(1)), m.group(2).strip()
            flush_text()
            if cur.blocks:
                sections.append(cur)
            if level == 1:
                stack = []  # a new top-level header restarts the sub-path
            else:
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, htext))
            cur = _Section(headers=path_for_stack())
        else:
            text_buf.append(ln)

    flush_text()
    if cur.blocks:
        sections.append(cur)
    return sections


def _manual_chunk(sec: _Section) -> BlockChunk:
    """One full-section chunk (code as code_ref tags) for keyword/get-section."""
    parts: list[str] = []
    code_refs: list[str] = []
    for b in sec.blocks:
        if b.kind == "code":
            parts.append(code_ref_tag(len(code_refs)))
            code_refs.append(b.body)
        else:
            parts.append(b.body)
    return BlockChunk(headers=sec.headers, part=0, code_refs=code_refs, chunk="\n\n".join(parts))


def _embedded_chunks(sec: _Section, config: BuilderConfig) -> list[BlockChunk]:
    """Length-bounded embedded chunks for vector search, via the shared builder."""
    builder = BlockBuilder(header=sec.headers, config=config)
    for b in sec.blocks:
        if b.kind == "code":
            builder.add_code(b.body)
        else:
            builder.append_text(b.body, is_structured_boundary=True, unbreakable=False)
    return list(builder.finish())


async def _async_main(args: argparse.Namespace) -> None:
    config = BuilderConfig(nlp=spacy.load("en_core_web_sm"), max_length=args.max_length)

    all_sections: list[_Section] = []
    for f in args.files:
        secs = parse_markdown(pathlib.Path(f))
        logger.info("%s -> %d section(s)", f, len(secs))
        all_sections.extend(secs)

    if args.print:
        for s in all_sections:
            print(f"\n#### {' / '.join(s.headers)}")
            print(_manual_chunk(s).chunk[:500])
        return

    db = await get_rag_db(args.output, get_model())
    buffer: list[BlockChunk] = []
    n_docs = n_manual = 0
    # manual_sections is unique on (headers, part); bump part for repeated paths.
    seen_paths: dict[tuple[str, ...], int] = {}
    for s in all_sections:
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
    logger.info("ingested %d embedded chunk(s) + %d manual section(s)", n_docs, n_manual)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Crucible markdown docs into the crucible_kb RAG.")
    parser.add_argument("files", nargs="+", type=pathlib.Path, help="Markdown files to ingest")
    parser.add_argument("--max-length", type=int, default=2000)
    parser.add_argument("--output", "-o", default=CRUCIBLE_DEFAULT_CONNECTION, help="RAG DB connection string")
    parser.add_argument("--print", action="store_true", help="Dry-run: print chunks, no DB writes")
    asyncio.run(_async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
