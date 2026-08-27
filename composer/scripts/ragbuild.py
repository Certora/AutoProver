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
"""Build the CVL-manual RAG database from sphinx HTML.

The HTML parsing lives in :mod:`composer.rag.html_manual`, which turns a manual into a tree of
typed blocks; this module owns the chunking half — driving ``BlockBuilder`` for the vector index
and ``TextStreamer`` for the manual sections, then writing both to the DB.
"""

from typing import Generator

import logging
import argparse
import pathlib

from composer.rag.db import get_rag_db, DEFAULT_CONNECTION
from composer.rag.html_manual import HtmlBlock, Section, SphinxManual
from composer.rag.import_format import EmbeddedBlockKind
from composer.rag.types import BlockChunk
from composer.rag.text import get_code_refs
from composer.rag.models import get_model
from composer.scripts.text_processors import (
    BlockBuilder, BuilderConfig, TextCollector, TextStreamer,
)

import spacy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


max_length = 2000
nlp = spacy.load("en_core_web_sm")

# Shared text-processing config; passed into every ``BlockBuilder`` so the
# chunker has access to the spaCy model and the soft length cap. Kept as a
# module-level singleton because ``spacy.load`` is expensive and we don't
# want to re-load the model per call.
_builder_config = BuilderConfig(nlp=nlp, max_length=max_length)


def translate_block(streamer: TextStreamer, section: Section) -> Generator[BlockChunk, None, None]:
    """Chunk one section and its descendants, streaming the same content into ``streamer``.

    Each block kind maps onto exactly one way of driving the builder; the two products are built
    in one pass because they must see the content in the same order."""
    builder = BlockBuilder(header=section.headers, config=_builder_config)
    for item in section.items:
        match item:
            case HtmlBlock(kind=EmbeddedBlockKind.CODE, body=code):
                streamer.stream_code(code)
                builder.add_code(code)
            case HtmlBlock(kind=EmbeddedBlockKind.PARAGRAPH, body=body):
                streamer.stream_text(body)
                builder.append_text(body, is_structured_boundary=True, unbreakable=False)
            case HtmlBlock(kind=EmbeddedBlockKind.ATOMIC, body=body):
                streamer.stream_text(body)
                builder.append_text(body, is_structured_boundary=True, unbreakable=True)
            case HtmlBlock(kind=EmbeddedBlockKind.CONTINUATION, body=body):
                streamer.stream_text(body)
                builder.append_text(body, is_structured_boundary=False, unbreakable=False)
            case Section() as child:
                sec = [h for h in child.headers if h]
                streamer.section_start(sec)
                # An "Example" subsection is streamed into its parent's document rather than its
                # own: the example is only meaningful next to what it illustrates.
                child_streamer = streamer if "Example" in sec[-1] else streamer.child(sec)
                first = True
                for child_block in translate_block(child_streamer, child):
                    if first:
                        builder.push_child(child_block)
                        first = False
                    yield child_block
                streamer.section_end()
            case _:
                # A block kind added to the parser but not handled here would otherwise vanish from
                # the corpus silently, which is indistinguishable from documentation that never
                # covered the topic.
                raise AssertionError(f"unhandled block {item!r} under {section.headers}")
    for x in builder.finish():
        yield x


def sanity_checker(s: BlockChunk) -> None:
    seen = set()
    for (_, ref) in get_code_refs(s.chunk):
        if ref in seen:
            print(f"Duplicated code-ref {ref} in {s.chunk}")
        seen.add(ref)
        if ref >= len(s.code_refs):
            print(f"Orphan ref {ref} in {s.chunk}")


async def main() -> None:
    parser = argparse.ArgumentParser(description='Build RAG database from HTML documentation')
    parser.add_argument('files', nargs='+', metavar='HTML_FILE',
                        help='One or more HTML files to process directly')
    parser.add_argument('--output', '-o',
        help='Output directory for ChromaDB, or PostgreSQL connection string. '
             f'Defaults to PostgreSQL ({DEFAULT_CONNECTION})')
    args = parser.parse_args()

    output = args.output or DEFAULT_CONNECTION
    db = await get_rag_db(output, get_model())

    buffer: list[BlockChunk] = []

    for f in args.files:
        path = pathlib.Path(f)
        manual = SphinxManual(path.read_text(), section_label=path.stem)

        sink = TextCollector()
        root_streamer = TextStreamer(sink, 1, None, [])

        for section in manual.sections():
            trunc_head = [h for h in section.headers if h]
            section_streamer = root_streamer.child(trunc_head)
            for t in translate_block(section_streamer, section):
                sanity_checker(t)
                buffer.append(t)
                if len(buffer) == 50:
                    await db.add_chunks_batch(buffer)
                    buffer = []

        for i in sink.chunks():
            await db.add_manual_section(i)

    if buffer:
        await db.add_chunks_batch(buffer)

    logger.info(f"RAG database created at {output}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
