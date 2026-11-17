"""The CVLR API search tools.

Two things are worth holding still here, and neither is about a model's judgement.

The **authority ordering** is the whole reason there are two corpora rather than one with a header
root: a retrieval hit carries no provenance, so what tells an agent whether a claim came from the
crates or from prose is which tool answered. That lives in tool names and tool docstrings, which
are editable text, so the tests assert the text says it, because nothing else can.

The **not-found contract** is the other. A vector search always returns its nearest rows, so
"nothing came back" cannot distinguish absence from an awkward query, and the corpus's answer to
"does X exist" is a closed listing. A tool that softened that into a plausible guess would fail
nothing else.
"""

import re

import pytest

from composer.rag.db import CVLR_API_DEFAULT_CONNECTION, CVLR_DEFAULT_CONNECTION, KNOWLEDGE_BASES
from composer.rag.cvlr_api import API_ROOT, SURFACE_HEADING, ApiPath
from composer.rag.types import ManualRef, ManualSectionHit
from composer.scripts.cvlr_api_docs import (
    DocumentedCrate,
    ExampleGate,
    Item,
    ItemKind,
    build_manifest,
)
from composer.tools.cvlr_api_rag import ApiCorpus, get_tools

_ASSERT_LE = Item(
    name="cvlr_assert_le", kind=ItemKind.MACRO, crate="cvlr-asserts", path="cvlr_asserts::cvlr_assert_le",
    signature="macro_rules! cvlr_assert_le {\n    ($lhs:expr, $rhs:expr) => { ... };\n}",
)
#: Re-exported by the facade under another name, as `cvlr::clog` is `cvlr_log`.
_ASSUME = Item(
    name="cvlr_assume", kind=ItemKind.MACRO, crate="cvlr-asserts", path="cvlr_asserts::cvlr_assume",
    signature="macro_rules! cvlr_assume { ... }", docs="Assume a condition holds.",
    aliases=("cvlr::asserts::cvt_assume",),
)
_SATISFY = Item(
    name="cvlr_satisfy", kind=ItemKind.MACRO, crate="cvlr-asserts", path="cvlr_asserts::cvlr_satisfy",
    signature="macro_rules! cvlr_satisfy { ... }", docs="Satisfy, and unlike an assume.",
    aliases=("cvlr::asserts::cvt_satisfy",),
)
_U64_MAX = Item(
    name="NativeIntU64::u64_max", kind=ItemKind.FUNCTION, crate="cvlr-mathint", path=None,
    signature="fn u64_max() -> Self",
)
_DESERIALIZE = Item(
    name="cvlr_deserialize_nondet_accounts", kind=ItemKind.FUNCTION, crate="cvlr-solana",
    path="cvlr_solana::cvlr_deserialize_nondet_accounts",
    signature="fn cvlr_deserialize_nondet_accounts() -> [AccountInfo; 16]",
    docs="Nondeterministic accounts for a rule.",
)


def _db(*crates: DocumentedCrate) -> "_FakeDB":
    gate = ExampleGate(verdicts={})
    return _FakeDB(build_manifest(list(crates), "test", gate).manual_sections)


def _solana(version: str) -> DocumentedCrate:
    return DocumentedCrate("cvlr-solana", version, (_DESERIALIZE,))


_ASSERTS = DocumentedCrate("cvlr-asserts", "0.6.1", (_ASSERT_LE, _ASSUME, _SATISFY))


# ---------------------------------------------------------------------------------------------
# the corpora


def test_the_two_corpora_do_not_share_a_connection():
    # A copy-pasted constant would silently merge them again, and the merge is invisible: both
    # tool sets would work, and answers would just quietly cross over.
    assert CVLR_DEFAULT_CONNECTION != CVLR_API_DEFAULT_CONNECTION


def test_the_api_manifest_tag_resolves_for_the_importer():
    """``cvlr_api_docs`` emits a manifest tagged ``cvlr_api_kb``, and ``rag_import`` has no other way
    to find the database it names. It must be the database the API tools search."""
    assert KNOWLEDGE_BASES["cvlr_api_kb"] == CVLR_API_DEFAULT_CONNECTION




# ---------------------------------------------------------------------------------------------
# the tools


class _FakeDB:
    """A corpus that answers from a manifest, so the tools are tested over real emitted rows."""

    def __init__(self, sections):
        self._sections = {tuple(s.headers): s for s in sections}

    def _content(self, section) -> str:
        return "\n\n".join(b.body for b in section.blocks)

    async def manual_headers(self, prefix):
        return [list(h) for h in self._sections if h[: len(prefix)] == tuple(prefix)]

    async def search_manual_keywords(self, query, *, min_depth=0, limit=10):
        """Every word must occur in the section's text, where, as in Postgres's parser, `_` and
        `:` separate words. The headers are not searched."""
        words = re.findall(r"[A-Za-z0-9]+", query.lower())
        return [
            ManualSectionHit(headers=list(h), relevance=1.0)
            for h, s in self._sections.items()
            if all(w in re.findall(r"[A-Za-z0-9]+", self._content(s).lower()) for w in words)
        ][:limit]

    async def get_manual_section(self, headers):
        section = self._sections.get(tuple(headers))
        return self._content(section) if section is not None else None

    async def find_refs(self, query, similarity_cutoff=0.5, top_k=10, manual_section=()):
        return [
            ManualRef(headers=list(h), content=self._content(s), similarity=0.9)
            for h, s in list(self._sections.items())[:top_k]
        ]


@pytest.fixture
def corpus():
    return _db(_ASSERTS, DocumentedCrate("cvlr-mathint", "0.6.1", (_U64_MAX,)))


@pytest.fixture
def tools(corpus):
    return {t.name: t for t in get_tools(corpus, {})}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lookup_returns_the_entry_in_one_call(tools):
    # A caller with an identifier gets the entry itself, not a list of sections to fetch.
    out = await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_assert_le"})
    assert "macro_rules! cvlr_assert_le" in out
    assert "cvlr-asserts" in out


@pytest.mark.asyncio
async def test_lookup_is_by_name_not_by_the_words_in_it(tools):
    """Full-text search splits an identifier at `_`, so `cvlr_assert` there is `cvlr & assert`
    and matches every `cvlr_assert_*` entry and the surface listing. The corpus has no
    `cvlr_assert`, so the answer is that there is no such entry."""
    out = await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_assert"})
    assert out.startswith("No entry")
    assert "macro_rules!" not in out


@pytest.mark.asyncio
async def test_lookup_finds_a_method_by_its_bare_name(tools):
    out = await tools["cvlr_api_lookup"].ainvoke({"name": "u64_max"})
    assert "NativeIntU64::u64_max" in out


@pytest.mark.asyncio
async def test_lookup_finds_an_item_by_the_name_a_re_export_gives_it():
    """`cvlr::asserts::cvt_assume` is `cvlr_assume` under another name, as `cvlr::clog` is
    `cvlr_log`. The entry is filed under the defining name, so no header leaf spells it."""
    lookup = {t.name: t for t in get_tools(_db(_ASSERTS), {})}["cvlr_api_lookup"]  # type: ignore[arg-type]
    out = await lookup.ainvoke({"name": "cvt_assume"})
    assert "cvlr-asserts > 0.6.1 > cvlr_assume" in out
    assert "cvlr_satisfy" not in out


@pytest.mark.asyncio
async def test_lookup_that_finds_nothing_points_at_the_closed_listing(tools):
    """Not-found has to hand the caller the one tool that can settle existence.

    Otherwise the honest answer ('I did not find it') and the dangerous one ('it does not exist')
    are the same string, and an agent will pick whichever suits it.
    """
    out = await tools["cvlr_api_lookup"].ainvoke({"name": "nondet_option_of_nothing"})
    assert "cvlr_api_surface" in out
    assert "does not exist" not in out.lower()


@pytest.mark.asyncio
async def test_the_surface_listing_is_closed_and_says_so(tools):
    out = await tools["cvlr_api_surface"].ainvoke({"crate": "cvlr-asserts"})
    assert "closed" in out.lower()
    assert "`cvlr_assert_le`" in out
    assert "`NativeIntU64::u64_max`" not in out, "that belongs to another crate"


@pytest.mark.asyncio
async def test_an_unknown_crate_is_reported_as_unusable_rather_than_missing(tools):
    # A crate this build does not pin cannot be used whatever it offers, and saying so is more
    # useful than "no results".
    out = await tools["cvlr_api_surface"].ainvoke({"crate": "cvlr-imaginary"})
    assert "cannot use it" in out


@pytest.mark.asyncio
async def test_an_empty_vector_search_denies_being_evidence_of_absence(corpus):
    empty = _FakeDB([])
    search = {t.name: t for t in get_tools(empty, {})}["cvlr_api_search"]  # type: ignore[arg-type]
    out = await search.ainvoke({"query": "anything at all"})
    assert "does not mean the item does not exist" in out
    assert "cvlr_api_surface" in out


def test_every_api_tool_states_the_authority_ordering(tools):
    """The ordering is only ever carried by text, so the text is what there is to test.

    An agent decides whether to trust a row by which tool returned it; a docstring that omitted
    this would leave that decision to the model's priors.
    """
    for name, tool in tools.items():
        assert "authoritative" in tool.description, name
        assert "manual" in tool.description, name


def test_the_surface_heading_is_shared_by_the_producer_and_the_tool():
    # The producer writes this heading and `cvlr_api_surface` fetches by it. Two spellings would
    # make the closed listing unreachable, and nothing else would fail.
    headers = [
        list(s.headers)
        for s in build_manifest([_ASSERTS], "t", ExampleGate(verdicts={})).manual_sections
    ]
    assert [API_ROOT, "cvlr-asserts", "0.6.1", SURFACE_HEADING] in headers


# ---------------------------------------------------------------------------------------------
# one corpus, two platform generations


@pytest.fixture
def two_generations():
    """``cvlr-solana`` at both release lines, and a core crate the generations share."""
    return _db(_solana("0.5.1"), _solana("0.6.0"), _ASSERTS)


def _scoped(db, pinned):
    return {t.name: t for t in get_tools(db, pinned)}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_run_is_shown_only_the_release_it_builds(two_generations):
    """An SDK 3 run told about 0.5.1 is told, with full authority, about an API its build lacks."""
    tools = _scoped(two_generations, {"cvlr-solana": "0.6.0", "cvlr": "0.6.1"})
    for out in (
        await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_deserialize_nondet_accounts"}),
        await tools["cvlr_api_surface"].ainvoke({"crate": "cvlr-solana"}),
    ):
        assert "cvlr-solana > 0.6.0" in out
        assert "0.5.1" not in out
    # The fake's vector search ignores the query, so what it returns says nothing about ranking;
    # what it must not return is the other generation.
    out = await tools["cvlr_api_search"].ainvoke({"query": "nondeterministic accounts"})
    assert "0.5.1" not in out


@pytest.mark.asyncio
async def test_a_crate_the_run_does_not_pin_is_shown_at_the_version_the_corpus_holds(
    two_generations,
):
    """The core's sub-crates are shared by the generations, and pinned through the core only."""
    tools = _scoped(two_generations, {"cvlr-solana": "0.5.1"})
    out = await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_assert_le"})
    assert "cvlr-asserts > 0.6.1" in out


@pytest.mark.asyncio
async def test_a_patch_release_reads_the_corpus_of_an_earlier_patch_on_its_line(two_generations):
    """A corpus seeded at 0.6.0 serves a run on 0.6.1, and says so: a patch release may add
    items, so the listing is closed only for the release it was generated from."""
    tools = _scoped(two_generations, {"cvlr-solana": "0.6.1"})
    for out in (
        await tools["cvlr_api_surface"].ainvoke({"crate": "cvlr-solana"}),
        await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_deserialize_nondet_accounts"}),
    ):
        assert "cvlr-solana > 0.6.0" in out
        assert "This build pins 0.6.1" in out
        assert "0.5.1" not in out


@pytest.mark.asyncio
async def test_the_newest_admitted_release_wins():
    tools = _scoped(_db(_solana("0.6.0"), _solana("0.6.1")), {"cvlr-solana": "0.6.1"})
    for out in (
        await tools["cvlr_api_surface"].ainvoke({"crate": "cvlr-solana"}),
        await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_deserialize_nondet_accounts"}),
    ):
        assert "cvlr-solana > 0.6.1" in out
        assert "0.6.0" not in out


@pytest.mark.parametrize(
    ("row", "pin", "admitted"),
    [
        ("0.5.1", "0.5.1", True),
        ("0.5.1", "0.5.2", True),
        # A later release may describe items this build lacks.
        ("0.5.2", "0.5.1", False),
        # Another line is another generation.
        ("0.5.1", "0.6.1", False),
        ("0.6.0", "0.5.2", False),
        ("1.2.0", "1.4.1", True),
        ("1.2.0", "2.0.0", False),
        # 0.0.x has no compatible releases.
        ("0.0.3", "0.0.4", False),
        # Anything that is not plain major.minor.patch matches only itself.
        ("0.5.1-rc.1", "0.5.2", False),
        ("0.5.1-rc.1", "0.5.1-rc.1", True),
    ],
)
def test_which_releases_a_pin_admits(row, pin, admitted):
    corpus = ApiCorpus(db=object(), pinned={"cvlr-solana": pin})  # type: ignore[arg-type]
    assert corpus.admits(ApiPath("cvlr-solana", row, "item")) is admitted
    # A crate the run does not pin is admitted whatever its version.
    assert corpus.admits(ApiPath("cvlr-asserts", row, "item"))
