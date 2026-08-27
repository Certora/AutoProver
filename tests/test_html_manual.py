"""Sphinx HTML → section tree (``composer.rag.html_manual``).

This is the parsing half of the docs pipeline, and it is deliberately free of the chunking half's
dependencies (no spaCy, no DB), so these tests need nothing installed beyond bs4.

What matters here is that the tree preserves the two things its consumers depend on: the block
*kinds*, which are the chunker's whole instruction set, and document *order* — including where a
child section sits among its parent's blocks, since that is what decides which chunk a subsection
is linked from.
"""

from composer.rag.html_manual import HtmlBlock, Section, SphinxManual
from composer.rag.import_format import EmbeddedBlockKind as Kind


def _manual(body: str, label: str | None = "solana") -> SphinxManual:
    """Wrap section markup in the minimum sphinx singlehtml scaffolding the parser looks for."""
    return SphinxManual(f'<div itemprop="articleBody">{body}</div>', section_label=label)


def _kinds(section: Section) -> list[tuple[Kind, str]]:
    return [(b.kind, b.body.strip()) for b in section.blocks() if b.body.strip()]


def test_each_element_gets_the_kind_its_chunking_needs():
    manual = _manual("""
        <section id="s"><h1>Top</h1>
          <p>Prose.</p>
          <div class="highlight"><pre>cvlr_assert!(x);</pre></div>
          <ul><li>one</li><li>two</li></ul>
          <table><tr><th>a</th></tr><tr><td>1</td></tr></table>
          <div class="admonition warning">Careful.</div>
        </section>
    """)
    (top,) = manual.sections()
    assert _kinds(top) == [
        (Kind.PARAGRAPH, "Prose."),
        (Kind.CODE, "cvlr_assert!(x);"),
        (Kind.ATOMIC, "* one\n * two"),
        (Kind.ATOMIC, "| a |\n| --- |\n| 1 |"),
        (Kind.ATOMIC, "Careful."),
    ]


def test_a_child_section_keeps_its_position_among_the_parents_blocks():
    # Not a separate list of children: a consumer links the parent chunk that *precedes* a
    # subsection to it, so flattening blocks and children apart would lose the relationship.
    manual = _manual("""
        <section id="s"><h1>Top</h1>
          <p>Before.</p>
          <section id="c"><h2>Child</h2><p>Inside.</p></section>
          <p>After.</p>
        </section>
    """)
    (top,) = manual.sections()
    shapes = [type(i).__name__ for i in top.items if not (isinstance(i, HtmlBlock) and not i.body.strip())]
    assert shapes == ["HtmlBlock", "Section", "HtmlBlock"]
    (child,) = top.children()
    assert _kinds(child) == [(Kind.PARAGRAPH, "Inside.")]
    # The child's blocks are the child's alone — a parent's own content excludes them.
    assert _kinds(top) == [(Kind.PARAGRAPH, "Before."), (Kind.PARAGRAPH, "After.")]


def test_a_blockquote_is_kept_as_prose():
    # Regression: blockquotes were falling through the traversal unhandled, which silently dropped
    # the Solana manual's statement of the pre/post snapshot methodology.
    manual = _manual('<section id="s"><h1>Top</h1><blockquote><p>Before X. After Y.</p></blockquote></section>')
    (top,) = manual.sections()
    assert _kinds(top) == [(Kind.PARAGRAPH, "Before X. After Y.")]


def test_changelog_sections_are_dropped_before_any_walk():
    # Documentation *of changes* dates immediately and answers no question an agent asks; dropping
    # it at construction means no consumer has to know about it. The sphinx-generated id is the
    # signal that actually fires, because ids are slugified to lower case — heading text is matched
    # case-sensitively, so "Changes since 1.0" is caught by its id and not by its title.
    manual = _manual("""
        <section id="s"><h1>Top</h1><p>Keep.</p>
          <section id="changelog"><h2>Changelog</h2><p>Drop.</p></section>
          <section id="changes-since-1-0"><h2>Changes since 1.0</h2><p>Drop too.</p></section>
        </section>
    """)
    (top,) = manual.sections()
    assert list(top.children()) == []
    assert _kinds(top) == [(Kind.PARAGRAPH, "Keep.")]


def test_the_section_label_takes_h1_and_shifts_the_headings_down():
    # Several manuals share one corpus, so the manual's own name has to be part of the path.
    manual = _manual('<section id="s"><h1>Top</h1><section id="c"><h2>Child</h2><p>x</p></section></section>')
    (top,) = manual.sections()
    (child,) = top.children()
    assert top.headers[:3] == ["solana", "Top", ""]
    assert child.headers[:3] == ["solana", "Top", "Child"]


def test_without_a_label_the_headings_start_at_h1():
    manual = _manual('<section id="s"><h1>Top</h1><p>x</p></section>', label=None)
    (top,) = manual.sections()
    assert top.headers[0] == "Top"


def test_walk_yields_every_descendant_parents_first():
    manual = _manual("""
        <section id="a"><h1>A</h1>
          <section id="b"><h2>B</h2><section id="c"><h3>C</h3><p>x</p></section></section>
          <section id="d"><h2>D</h2><p>y</p></section>
        </section>
    """)
    (top,) = manual.sections()
    assert [next(h for h in s.headers[::-1] if h) for s in top.walk()] == ["A", "B", "C", "D"]


def test_an_unheaded_or_version_annotated_block_is_skipped():
    manual = _manual("""
        <section id="s"><h1>Top</h1>
          <div class="versionadded">New in 0.6.</div>
          <div class="math">\\(x\\)</div>
          <nav class="contents"><p>table of contents</p></nav>
          <p>Real.</p>
        </section>
    """)
    (top,) = manual.sections()
    assert _kinds(top) == [(Kind.PARAGRAPH, "Real.")]
