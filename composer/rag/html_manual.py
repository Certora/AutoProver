"""Sphinx/docutils HTML → a section tree of typed content blocks.

This is the *parsing* half of the documentation pipelines, split out from the *chunking* half so
the two can be used independently. It imports nothing but ``bs4`` and
:mod:`composer.rag.import_format`, and in particular no spaCy, no embedding model and no database —
so a corpus producer that only needs to emit a manifest can use it without the ``ragbuild``
dependency group (see ``docs/rag-import-format.md``).

Two consumers, with genuinely different needs, which is why the seam is a tree rather than a
stream:

* :mod:`composer.scripts.ragbuild` walks the tree driving ``BlockBuilder``/``TextStreamer`` to
  write embedded chunks and manual sections straight to a DB. It needs the *nesting* — a parent
  chunk is linked to its first child via ``push_child``.
* A manifest producer flattens the tree into ``embedded_groups`` / ``manual_sections``.

The block kinds are :class:`~composer.rag.import_format.EmbeddedBlockKind` rather than a private
enum: they are exactly the distinctions the chunker acts on, the manifest format already speaks
them, and a second vocabulary would have to be mapped onto that one anyway.
"""

import dataclasses
import logging
from typing import Iterator, cast

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from composer.rag.import_format import EmbeddedBlockKind

logger = logging.getLogger(__name__)

#: Sections whose id or heading matches one of these is dropped: documentation *of changes* dates
#: immediately and answers no question an agent asks.
_CHANGELOG_MARKERS = (
    "changelog", "release-note", "release note", "changes-since", "changes since",
    "changes-introduced", "changes introduced", "changes to",
)

#: The deepest header column the DB has (``h1``..``h6``); a deeper path is clamped into it.
_MAX_HEADER_DEPTH = 6


@dataclasses.dataclass(frozen=True)
class Header:
    head: str
    level: int


@dataclasses.dataclass
class HtmlBlock:
    """One piece of content, tagged with what it *is* — the chunker decides what that means."""

    kind: EmbeddedBlockKind
    body: str


@dataclasses.dataclass
class Section:
    """One ``<section>``: its header path, and its content in document order.

    ``items`` interleaves blocks and child sections exactly as they appear, because both consumers
    care about the order: a chunk's text follows the page, and a subsection's position decides
    which chunk it is linked from."""

    headers: list[str]
    items: list["HtmlBlock | Section"] = dataclasses.field(default_factory=list)

    def blocks(self) -> Iterator[HtmlBlock]:
        """This section's own blocks, excluding every child section's."""
        for item in self.items:
            if isinstance(item, HtmlBlock):
                yield item

    def children(self) -> Iterator["Section"]:
        for item in self.items:
            if isinstance(item, Section):
                yield item

    def walk(self) -> Iterator["Section"]:
        """This section and every descendant, parents before children."""
        yield self
        for child in self.children():
            yield from child.walk()


def get_section_header(s: Tag) -> Header | None:
    """The ``h1``..``h6`` heading a section opens with, or ``None`` if it does not open with one.

    Deliberately strict: anything before the heading other than empty markup means this is not a
    plain titled section, and guessing a title for it would mislabel its content."""
    head_tag: Tag | None = None
    for ch in s.children:
        match ch:
            case Tag():
                if ch.name == "span":
                    if ch.text.strip():
                        return None
                elif ch.name.startswith("h"):
                    head_tag = ch
                    break
                else:
                    return None
            case NavigableString():
                if ch.text.strip():
                    return None
            case _:
                return None
    if head_tag is None:
        return None
    return Header(head=head_tag.getText(), level=int(head_tag.name[1:]))


def extract_code(s: Tag) -> str:
    assert s.name == "pre"
    block = ""
    for ch in s.children:
        match ch:
            case Tag():
                assert ch.name == "span"
                block += ch.text
            case NavigableString():
                block += ch.text
            case _:
                # Other PageElement kinds (comments, etc.) contribute no code.
                pass
    return block.strip("\n")


def translate_text_block(s: Tag) -> str:
    assert s.name == "p"
    return s.get_text("")


def class_or_empty(s: Tag) -> list[str]:
    return cast(list[str], s.attrs.get("class", []))


def convert_li(s: Tag, depth: int) -> str:
    ident = (" " * depth) + " * "
    elem = ident
    for c in s.children:
        match c:
            case Tag(name="ul") | Tag(name="ol"):
                elem += "\n"
                elem += convert_ul(c, depth + 1) + "\n"
            case _:
                elem += c.getText("")
    return elem


def convert_ul(s: Tag, depth: int = 0) -> str:
    elems = []
    for li in s.find_all("li"):
        assert isinstance(li, Tag)
        elems.append(convert_li(li, depth))
    return "\n".join(elems)


def convert_table(s: Tag) -> str:
    """Render a docutils ``<table>`` as a markdown-style table so the row/column structure
    survives into the chunk text the LLM sees."""
    assert s.name == "table"
    rows: list[list[str]] = []
    header_row: int | None = None
    for tr in s.find_all("tr"):
        assert isinstance(tr, Tag)
        cells: list[str] = []
        is_header = False
        for cell in tr.find_all(["th", "td"], recursive=False):
            assert isinstance(cell, Tag)
            if cell.name == "th":
                is_header = True
            cells.append(" ".join(cell.get_text(" ").split()))
        if not cells:
            continue
        if is_header and header_row is None:
            header_row = len(rows)
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = []
    for i, r in enumerate(rows):
        lines.append("| " + " | ".join(r) + " |")
        if i == header_row:
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def convert_aside(s: Tag) -> str:
    """Render an ``<aside>`` (docutils footnotes) as plain prose. Strip the ``[n]`` label/backlink
    span, which is just noise once the cross-reference is gone."""
    assert s.name == "aside"
    for label in s.find_all("span", {"class": "label"}):
        assert isinstance(label, Tag)
        label.decompose()
    return " ".join(s.get_text(" ").split())


def skip_class(s: Tag) -> bool:
    cl = class_or_empty(s)
    return "versionchanged" in cl or "versionadded" in cl or "math" in cl


class SphinxManual:
    """A parsed sphinx manual, ready to walk.

    Construction does the whole-document preparation — drop header anchors, drop changelog
    sections, locate the article body — so a caller cannot walk an unprepared document by
    accident. ``section_label`` is prepended to every header path (the manual's own name, e.g.
    ``solana``), which is what keeps several manuals distinguishable in one corpus."""

    def __init__(self, html: str, section_label: str | None = None) -> None:
        self.section_label = section_label
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", {"class": "headerlink"}):
            anchor.decompose()

        for section in soup.find_all("section"):
            if not isinstance(section, Tag):
                continue
            sid = (section.attrs or {}).get("id", "")
            h = get_section_header(section)
            heading = h.head if h else ""
            if any(kw in sid or kw in heading for kw in _CHANGELOG_MARKERS):
                section.decompose()

        main_body = soup.find("div", {"itemprop": "articleBody"})
        assert isinstance(main_body, Tag), str(main_body)
        self._main_body = main_body

    def header_path(self, section: Tag) -> list[str]:
        """The ``h1``..``h6`` path of ``section``: entry *i* belongs in column ``h(i+1)``, and an
        unused level stays empty. A ``section_label`` shifts everything down one level and takes
        ``h1``, so a path can be clamped at the deepest column rather than lose its own heading."""
        h = get_section_header(section)
        assert h is not None
        offset = 1 if self.section_label else 0
        headers = [""] * _MAX_HEADER_DEPTH
        if self.section_label:
            headers[0] = self.section_label
        headers[min(h.level - 1 + offset, _MAX_HEADER_DEPTH - 1)] = h.head
        for p in section.parents:
            if p == self._main_body:
                break
            if p.name == "section":
                head = get_section_header(p)
                assert head is not None
                idx = min(head.level - 1 + offset, _MAX_HEADER_DEPTH - 1)
                if not headers[idx]:
                    headers[idx] = head.head
        return headers

    def top_sections(self) -> list[Tag]:
        # singlehtml output wraps page sections in div.compound; individual html pages do not.
        if self._main_body.find("div", class_="compound"):
            found = self._main_body.select("div.compound > section")
        else:
            found = self._main_body.find_all("section", recursive=False)
        return [s for s in found if isinstance(s, Tag)]

    def sections(self) -> Iterator[Section]:
        """Every top-level section, parsed into a tree."""
        for tag in self.top_sections():
            yield self._parse_section(tag)

    def _parse_section(self, tag: Tag) -> Section:
        assert tag.name == "section"
        headers = self.header_path(tag)
        section = Section(headers=headers)
        for ch in tag.children:
            match ch:
                case Tag(name="nav"):
                    continue
                case Tag(name="div") if skip_class(ch):
                    continue
                case Tag(name="p"):
                    section.items.append(
                        HtmlBlock(EmbeddedBlockKind.PARAGRAPH, translate_text_block(ch))
                    )
                case Tag(name="div") if "admonition" in class_or_empty(ch):
                    section.items.append(HtmlBlock(EmbeddedBlockKind.ATOMIC, ch.getText("")))
                case Tag(name="div") if isinstance(ch.find("pre"), Tag):
                    code = extract_code(cast(Tag, ch.find("pre")))
                    section.items.append(HtmlBlock(EmbeddedBlockKind.CODE, code))
                case Tag(name="ul") | Tag(name="ol"):
                    section.items.append(HtmlBlock(EmbeddedBlockKind.ATOMIC, convert_ul(ch)))
                case Tag(name="blockquote"):
                    # Prose, so it chunks like a paragraph rather than as an atomic unit. The CVL
                    # and Prover manuals contain none; the Solana manual states the pre/post
                    # snapshot methodology in one, which was being dropped.
                    quote = " ".join(ch.get_text(" ").split())
                    if quote:
                        section.items.append(HtmlBlock(EmbeddedBlockKind.PARAGRAPH, quote))
                case Tag(name="table"):
                    tbl = convert_table(ch)
                    if tbl:
                        section.items.append(HtmlBlock(EmbeddedBlockKind.ATOMIC, tbl))
                case Tag(name="aside"):
                    aside = convert_aside(ch)
                    if aside:
                        section.items.append(HtmlBlock(EmbeddedBlockKind.ATOMIC, aside))
                case Tag(name="span") if ch.getText() == "":
                    continue
                case NavigableString():
                    # Kept even when it is only whitespace: it is the text *between* markup, and
                    # dropping it silently joins words that the page separates.
                    section.items.append(HtmlBlock(EmbeddedBlockKind.CONTINUATION, ch.text))
                case Tag(name="section"):
                    section.items.append(self._parse_section(ch))
                case Tag(name=nm) if nm.startswith("h"):
                    continue
                case _:
                    name = ch.name if isinstance(ch, Tag) else str(type(ch))
                    logger.warning(
                        "unhandled element %s in %s", name, " ".join(h for h in headers if h)
                    )
        return section
