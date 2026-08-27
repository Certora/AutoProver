"""The public ``cvlr_kb`` manifest producer (``composer.scripts.cvlr_docs_manifest``).

Turns published documentation HTML into the manifest ``rag_import`` ingests — the half of the CVLR
corpus that needs no private input (``docs/cvlr-capture-plan.md`` §4.7/§8.2).

The two products are laid out differently on purpose, and that is what these pin: an embedded
group is one section's *own* content (subsections retrieve on their own header paths), while a
manual section is a whole subtree, because ``get_section`` is asked for "the part of the manual
under this heading".

No spaCy and no DB here — that is the point of the producer/importer split, so these tests run in
the routine env.
"""

from composer.rag.html_manual import SphinxManual
from composer.rag.import_format import EmbeddedBlockKind, ManualBlockKind, SCHEMA_VERSION
from composer.scripts import cvlr_docs_manifest as producer


def _build(body: str, label: str | None = "solana") -> "producer.RagManifest":
    """Build a manifest straight from markup, bypassing the file plumbing."""
    manual = SphinxManual(f'<div itemprop="articleBody">{body}</div>', section_label=label)
    return producer.manifest_from_manuals([manual], source="test")


def _paths(items) -> list[str]:
    return [" / ".join(h for h in i.headers if h) for i in items]


NESTED = """
    <section id="a"><h1>Top</h1>
      <p>Intro.</p>
      <section id="b"><h2>Child</h2>
        <p>Child prose.</p>
        <div class="highlight"><pre>cvlr_assert!(x);</pre></div>
      </section>
    </section>
"""


def test_the_manifest_declares_the_shared_corpus_tag_and_current_schema():
    # Both CVLR manifests — this one and the private practice half — resolve through one tag.
    m = _build(NESTED)
    assert m.knowledge_base == "cvlr_kb"
    assert m.version == SCHEMA_VERSION


def test_an_embedded_group_holds_only_its_own_section():
    # A subsection is retrievable under its own header path, so duplicating its blocks into the
    # parent group would return the same text twice for one query.
    m = _build(NESTED)
    assert _paths(m.embedded_groups) == ["solana / Top", "solana / Top / Child"]
    top, child = m.embedded_groups
    assert [b.body for b in top.blocks] == ["Intro."]
    assert [(b.kind, b.body) for b in child.blocks] == [
        (EmbeddedBlockKind.PARAGRAPH, "Child prose."),
        (EmbeddedBlockKind.CODE, "cvlr_assert!(x);"),
    ]


def test_a_manual_section_holds_the_whole_subtree_with_the_boundaries_named():
    # get_section on a heading should return the manual under that heading, not just the prose
    # before its first subheading. The markers keep the structure readable once nesting is gone.
    m = _build(NESTED)
    (doc,) = [s for s in m.manual_sections if s.headers[1] == "Top" and not s.headers[2]]
    assert [(b.kind, b.body) for b in doc.blocks] == [
        (ManualBlockKind.TEXT, "Intro."),
        (ManualBlockKind.TEXT, "Section: solana / Top / Child"),
        (ManualBlockKind.TEXT, "Child prose."),
        (ManualBlockKind.CODE, "cvlr_assert!(x);"),
        (ManualBlockKind.TEXT, "(End of Section solana / Top / Child)"),
    ]


def test_code_stays_out_of_the_searchable_text():
    # The importer swaps a CODE block for a <code-ref-N> tag; mislabelling code as text would
    # embed it as prose and pollute keyword search.
    m = _build(NESTED)
    kinds = {b.kind for s in m.manual_sections for b in s.blocks if "cvlr_assert" in b.body}
    assert kinds == {ManualBlockKind.CODE}


def test_an_example_subsection_is_not_its_own_document():
    # An example retrieved away from the thing it illustrates is close to useless, so it ships
    # inside its parent's section instead of standing alone.
    m = _build("""
        <section id="a"><h1>Top</h1><p>Intro.</p>
          <section id="e"><h2>Example</h2><p>Illustration.</p></section>
        </section>
    """)
    assert _paths(m.manual_sections) == ["solana / Top"]
    assert any("Illustration." == b.body for b in m.manual_sections[0].blocks)
    # It is still independently *embedded* — vector search may legitimately match the example.
    assert "solana / Top / Example" in _paths(m.embedded_groups)


def test_a_whole_manual_is_not_offered_as_a_section():
    # A top-level path names the manual itself; returning all of it is not a retrieval. With no
    # section label the sole heading *is* the top level, so it yields no document...
    body = '<section id="a"><h1>Top</h1><p>x</p></section>'
    assert _paths(_build(body, label=None).manual_sections) == []
    # ...while the label shifts it to depth 2, which is a real section.
    assert _paths(_build(body).manual_sections) == ["solana / Top"]


def test_blocks_that_are_only_whitespace_are_dropped():
    # Whitespace between markup matters while parsing (it separates words) but carries nothing
    # into either product, and empty blocks bloat the manifest.
    m = _build('<section id="a"><h1>Top</h1>\n\n  <p>Only real content.</p>\n\n</section>')
    (group,) = m.embedded_groups
    assert [b.body for b in group.blocks] == ["Only real content."]


def test_a_section_with_no_content_of_its_own_gets_no_group():
    # A pure container heading would otherwise produce an empty chunk that can still match a query.
    m = _build('<section id="a"><h1>Top</h1><section id="b"><h2>Child</h2><p>x</p></section></section>')
    assert _paths(m.embedded_groups) == ["solana / Top / Child"]


def test_the_source_string_records_the_docs_revision_when_one_was_captured(tmp_path):
    # A corpus that cannot say which docs it came from cannot be audited when the docs move on.
    html = tmp_path / "solana.html"
    html.write_text("<div itemprop='articleBody'></div>")
    assert "no recorded docs revision" in producer.describe_source([html])
    (tmp_path / producer.PROVENANCE_FILE).write_text("Certora/Documentation deadbeef\n")
    assert producer.describe_source([html]) == "Certora/Documentation deadbeef :: solana.html"
