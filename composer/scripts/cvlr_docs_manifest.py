"""Producer: published Solana/CVLR documentation HTML → a ``cvlr_kb`` RAG manifest.

The public half of the CVLR corpus (``docs/cvlr-capture-plan.md`` §4.7 and §8.2). It reads the
sphinx ``singlehtml`` output that ``scripts/gen_docs.sh`` builds from the public
``Certora/Documentation`` repo and writes one manifest per
``composer.rag.import_format``; ``composer.scripts.rag_import`` ingests it.

Two properties are the point of doing it this way:

* **No private input.** Anyone with an AutoProver checkout can build this manifest, so a plain
  install still gets a CVLR corpus — API coverage without the practice coverage that the private
  half adds. Both manifests share the ``cvlr_kb`` tag and the importer numbers their sections
  apart.
* **No RAG dependencies.** Parsing lives in :mod:`composer.rag.html_manual` (bs4 only) and
  chunking/embedding belongs to the importer, so this script runs without spaCy, the embedding
  model, or a database.

Which manuals to feed it is a judgement call, so it is an argument rather than a default:
``solana.html`` is the on-topic one. ``prover.html`` documents diagnostics, TAC reports and
timeouts — directly useful for reading Solana results — but also a large EVM-specific CLI surface
that a Solana authoring agent should not be retrieving, so it is opt-in.
"""

import argparse
import json
import logging
import pathlib
from typing import Iterable

from composer.rag.html_manual import HtmlBlock, Section, SphinxManual
from composer.rag.import_format import (
    EmbeddedBlock,
    EmbeddedBlockKind,
    EmbeddedGroup,
    ManualBlock,
    ManualBlockKind,
    ManualSection,
    RagManifest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

#: The corpus tag both CVLR manifests share.
KNOWLEDGE_BASE = "cvlr_kb"

#: A manual section is emitted only for a header path deeper than this. A top-level path is the
#: whole manual, and returning an entire manual from ``get_section`` is not a retrieval.
_MIN_MANUAL_DEPTH = 1

#: Written by ``gen_docs.sh`` beside the HTML: which revision of the docs repo these came from.
PROVENANCE_FILE = "PROVENANCE"


def _content_blocks(section: Section) -> list[HtmlBlock]:
    """The section's own blocks, minus the whitespace-only ones.

    Whitespace between markup matters while parsing (it separates words), but a block that is only
    whitespace carries nothing into either retrieval product."""
    return [b for b in section.blocks() if b.body.strip()]


def _embedded_group(section: Section) -> EmbeddedGroup | None:
    """One group per section, holding that section's own content. Children get their own groups —
    the header path is what relates them, so nothing is lost by not nesting."""
    blocks = _content_blocks(section)
    if not blocks:
        return None
    return EmbeddedGroup(
        headers=list(section.headers),
        blocks=[EmbeddedBlock(kind=b.kind, body=b.body) for b in blocks],
    )


def _manual_blocks(section: Section) -> list[ManualBlock]:
    """A section's whole subtree, flattened, with the subsection boundaries named.

    A manual section is what ``get_section`` returns verbatim, so it holds the descendants too: a
    reader asking for a section wants the part of the manual under that heading, not just the prose
    before its first subheading. The markers keep the structure legible once the nesting is gone."""
    blocks: list[ManualBlock] = []
    for item in section.items:
        match item:
            case HtmlBlock(kind=EmbeddedBlockKind.CODE, body=body):
                blocks.append(ManualBlock(kind=ManualBlockKind.CODE, body=body))
            case HtmlBlock(body=body) if body.strip():
                blocks.append(ManualBlock(kind=ManualBlockKind.TEXT, body=body))
            case Section() as child:
                path = " / ".join(h for h in child.headers if h)
                child_blocks = _manual_blocks(child)
                if not child_blocks:
                    continue
                blocks.append(ManualBlock(kind=ManualBlockKind.TEXT, body=f"Section: {path}"))
                blocks.extend(child_blocks)
                blocks.append(
                    ManualBlock(kind=ManualBlockKind.TEXT, body=f"(End of Section {path})")
                )
            case _:
                continue
    return blocks


def _manual_section(section: Section) -> ManualSection | None:
    path = [h for h in section.headers if h]
    if len(path) <= _MIN_MANUAL_DEPTH:
        return None
    # An "Example" subsection is not its own document: an example retrieved away from the thing it
    # illustrates is close to useless. It still appears inside its parent's section.
    if "Example" in path[-1]:
        return None
    blocks = _manual_blocks(section)
    if not blocks:
        return None
    return ManualSection(headers=list(section.headers), blocks=blocks)


def manifest_from_manuals(manuals: Iterable[SphinxManual], source: str) -> RagManifest:
    """Lay out both retrieval products for already-parsed manuals.

    Separate from :func:`build_manifest` so the layout can be exercised without files: parsing and
    product layout are independent decisions and fail for unrelated reasons."""
    manifest = RagManifest(knowledge_base=KNOWLEDGE_BASE, source=source)
    for manual in manuals:
        for top in manual.sections():
            for section in top.walk():
                group = _embedded_group(section)
                if group is not None:
                    manifest.embedded_groups.append(group)
                doc = _manual_section(section)
                if doc is not None:
                    manifest.manual_sections.append(doc)
    return manifest


def build_manifest(paths: list[pathlib.Path], source: str) -> RagManifest:
    """Parse each manual under its own file stem — the label that keeps several manuals apart in
    one corpus — and lay out the products."""
    manuals = [SphinxManual(p.read_text(), section_label=p.stem) for p in paths]
    return manifest_from_manuals(manuals, source)


def describe_source(paths: list[pathlib.Path]) -> str:
    """Provenance for the manifest: the docs revision if ``gen_docs.sh`` recorded one, plus the
    manuals used. A corpus that cannot say which docs it came from cannot be audited when the docs
    move on."""
    names = ", ".join(p.name for p in paths)
    provenance = paths[0].parent / PROVENANCE_FILE
    if provenance.is_file():
        return f"{provenance.read_text().strip()} :: {names}"
    return f"{names} (no recorded docs revision; regenerate with scripts/gen_docs.sh)"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the public cvlr_kb RAG manifest from documentation HTML."
    )
    parser.add_argument(
        "files", nargs="+", type=pathlib.Path, metavar="HTML_FILE",
        help="sphinx singlehtml manuals, e.g. scripts/prover-docs/solana.html",
    )
    parser.add_argument(
        "--output", "-o", type=pathlib.Path,
        default=pathlib.Path("scripts/cvlr-docs/cvlr-docs.rag.json"),
        help="Where to write the manifest (default: %(default)s, where populate_cvlr_rag.sh looks).",
    )
    parser.add_argument("--source", help="Override the recorded provenance string.")
    args = parser.parse_args()

    missing = [p for p in args.files if not p.is_file()]
    if missing:
        raise SystemExit(
            f"no such file: {', '.join(str(p) for p in missing)}. Run scripts/gen_docs.sh to build "
            f"the documentation HTML."
        )

    manifest = build_manifest(args.files, args.source or describe_source(args.files))
    if not manifest.embedded_groups:
        raise SystemExit(
            "produced an empty manifest — the input parsed but yielded no content. Check that the "
            "HTML is sphinx singlehtml output with an itemprop=articleBody container."
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest.model_dump(mode="json"), indent=1) + "\n")
    logger.info(
        "wrote %s: %d embedded group(s), %d manual section(s), source=%s",
        args.output, len(manifest.embedded_groups), len(manifest.manual_sections), manifest.source,
    )


if __name__ == "__main__":
    main()
