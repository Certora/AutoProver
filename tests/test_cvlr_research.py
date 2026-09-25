"""The CVLR research sub-agent and the API search tools it reads.

Two things are worth holding still here, and neither is about a model's judgement.

The **authority ordering** is the whole reason there are two corpora rather than one with a header
root: a retrieval hit carries no provenance, so what tells an agent whether a claim came from the
crates or from prose is which tool answered. That lives in tool names, tool docstrings and one
paragraph of the researcher's prompt, all of which are editable text — so the tests assert the
text says it, because nothing else can.

The **not-found contract** is the other. A vector search always returns its nearest rows, so
"nothing came back" cannot distinguish absence from an awkward query, and the corpus's answer to
"does X exist" is a closed listing. A tool that softened that into a plausible guess would fail
nothing else.
"""

import json
from pathlib import Path

import pytest

from composer.rag.db import CVLR_API_DEFAULT_CONNECTION, CVLR_DEFAULT_CONNECTION, KNOWLEDGE_BASES
from composer.rag.types import ManualRef, ManualSectionHit
from composer.scripts.cvlr_api_docs import CrateDocs, build_manifest
from composer.tools import rag_env
from composer.tools.cvlr_api_rag import API_ROOT, SURFACE_HEADING, get_tools
from composer.ui.tool_display import CommonTools

_DATA = Path(__file__).parent / "data" / "cvlr_rustdoc"


# ---------------------------------------------------------------------------------------------
# registration


def test_the_two_corpora_do_not_share_a_connection():
    # A copy-pasted constant would silently merge them again, and the merge is invisible: both
    # tool sets would work, and answers would just quietly cross over.
    assert CVLR_DEFAULT_CONNECTION != CVLR_API_DEFAULT_CONNECTION
    assert KNOWLEDGE_BASES["cvlr_kb"] != KNOWLEDGE_BASES["cvlr_api_kb"]


def test_the_api_corpus_is_registered_in_both_halves():
    """A tag needs a connection *and* a tools module.

    ``rag_env``'s docstring is explicit about why: a half-registration passes ``validate_rag_db``
    and is then swallowed by the degrade path, which produces a run with no search tools and no
    error — exactly the confusion the two failure modes are separated to avoid.
    """
    rag_env.validate_rag_db("cvlr_api_kb")
    assert "cvlr_api_kb" in KNOWLEDGE_BASES
    assert "cvlr_api_kb" in rag_env._FACTORIES


# ---------------------------------------------------------------------------------------------
# the tools


class _FakeDB:
    """A corpus that answers from a manifest, so the tools are tested over real emitted rows."""

    def __init__(self, sections):
        self._sections = {tuple(s.headers): s for s in sections}

    def _content(self, section) -> str:
        return "\n\n".join(b.body for b in section.blocks)

    async def search_manual_keywords(self, query, *, min_depth=0, limit=10):
        terms = [t.strip('"') for t in query.split() if t.strip('"')]
        hits = [
            ManualSectionHit(headers=list(h), relevance=1.0)
            for h, s in self._sections.items()
            if all(any(t in part for part in h) or t in self._content(s) for t in terms)
        ]
        return hits[:limit]

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
    crates = [
        CrateDocs("cvlr-asserts", "0.6.1", json.loads((_DATA / "cvlr_asserts.json").read_text())),
        CrateDocs("cvlr-mathint", "0.6.1", json.loads((_DATA / "cvlr_mathint.json").read_text())),
    ]
    return _FakeDB(build_manifest(crates, source="test").manual_sections)


@pytest.fixture
def tools(corpus):
    return {t.name: t for t in get_tools(corpus)}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lookup_returns_the_entry_in_one_call(tools):
    # Keyword-search-then-fetch is right when you are exploring a manual and wrong when you
    # already have an identifier, which the live run's census says is the dominant case.
    out = await tools["cvlr_api_lookup"].ainvoke({"name": "cvlr_assert_le"})
    assert "macro_rules! cvlr_assert_le" in out
    assert "cvlr-asserts" in out


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
    search = {t.name: t for t in get_tools(empty)}["cvlr_api_search"]  # type: ignore[arg-type]
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
    crates = [CrateDocs("cvlr-asserts", "0.6.1", json.loads((_DATA / "cvlr_asserts.json").read_text()))]
    headers = [list(s.headers) for s in build_manifest(crates, "t").manual_sections]
    assert [API_ROOT, "cvlr-asserts", "0.6.1", SURFACE_HEADING] in headers


# ---------------------------------------------------------------------------------------------
# the sub-agent


def test_the_prompt_states_which_corpus_wins():
    """The researcher is the one place the ordering is applied, so its prompt has to carry it.

    This replaces what the source mount did by simply being the code.
    """
    prompt = (
        Path(__file__).parent.parent
        / "composer" / "templates" / "cvlr_research_system_prompt.j2"
    ).read_text()
    assert "authoritative" in prompt
    assert "the API reference is right and" in prompt
    assert "stale" in prompt


def test_the_prompt_forbids_inferring_absence_from_an_empty_search():
    prompt = (
        Path(__file__).parent.parent
        / "composer" / "templates" / "cvlr_research_system_prompt.j2"
    ).read_text()
    assert "cvlr_api_surface" in prompt
    assert "Never infer absence" in prompt


def test_the_tool_doc_tells_the_caller_that_not_found_is_an_answer():
    """The caller needs this as much as the sub-agent does.

    An agent that reads "I could not establish that" as a failure will retry the same question, or
    worse, fall back to writing the helper it had in mind.
    """
    from composer.spec.cvlr_research import CVLR_RESEARCH_BASE_DOC

    assert "is a real answer" in CVLR_RESEARCH_BASE_DOC
    assert "not that the question should be retried" in CVLR_RESEARCH_BASE_DOC


def test_the_researcher_takes_nothing_run_specific():
    """Its answers are cached across runs, so a per-run input would be a correctness bug.

    The pin makes the version run-invariant and `--withhold-crate` is a statement about the
    target, so the question is the whole input. If that stops being true, the shared namespace has
    to go with it.
    """
    import inspect

    from composer.spec.cvlr_research import CvlrResearchSchemaBase, cvlr_research_tools

    assert set(CvlrResearchSchemaBase.model_fields) == {"question"}
    params = inspect.signature(cvlr_research_tools).parameters
    assert "reference" not in params and "preflight" not in params


def test_the_two_corpora_are_displayed_apart():
    # Which corpus answered is how a reader of a transcript knows what a claim rests on; a UI that
    # flattened both into "CVLR search" would erase exactly the distinction the split exists for.
    displays = CommonTools.cvlr_research_displays()
    assert {"cvlr_api_lookup", "cvlr_api_search", "cvlr_api_surface"} <= set(displays)
    assert {"cvlr_manual_search", "cvlr_keyword_search", "cvlr_get_section"} <= set(displays)
    assert "cvlr_research" in displays
