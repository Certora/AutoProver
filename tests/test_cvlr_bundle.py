"""The CVLR knowledge bundle, and the line between it and an agent's own prompt.

The bundle is a cached prefix: its documents are read once per process and sit behind a cache
marker, in front of the CVLR agents. That is what makes it cheap to give the same facts to the
author, the judge, the munge reviewer and the researcher, and it is also the constraint — a document
that varies per run cannot live here, and one that names a tool only the author holds should not.
"""

import re
from pathlib import Path

from composer.kb.kb_context import (
    CVLR_BUNDLE,
    CVLR_FACTS_BUNDLE,
    CVLR_RECIPES,
    _kb_model,
    context_documents,
    kb_loader,
)

_RESOURCES = Path(__file__).parent.parent / "composer" / "kb" / "resources"
_FACTS = _RESOURCES / "cvlr_baseline_facts.md"


def _headings(text: str) -> set[str]:
    return set(re.findall(r"^#{2,3} (.+)$", text, re.MULTILINE))


def test_the_bundle_carries_no_per_run_material():
    """A jinja expression that reaches a resource file is not rendered — it is handed to the model
    verbatim, as `{{ example.handler }}`. The failure is silent and reads as a corrupted prompt, so
    the guard is worth more than the thing it checks."""
    assert "{{" not in _FACTS.read_text() and "{%" not in _FACTS.read_text()


def test_the_bundle_keeps_what_every_cvlr_agent_needs():
    facts = _headings(_FACTS.read_text())
    assert any("Nondeterminism" in h for h in facts)
    assert any("Assumptions" in h for h in facts)
    assert any("nonlinear" in h for h in facts)


def test_the_recipe_index_is_a_document_and_the_facts_are_another():
    """Two delivery shapes, on purpose: the facts are what every CVLR agent should already know, and
    the index is a list of situations it should recognise and then go read about."""
    docs = context_documents(CVLR_BUNDLE)
    assert len(docs) == 2
    assert "CVLR and the Certora Solana Prover" in docs[0]
    assert "get_cvlr_recipe" in docs[1]


def test_the_index_says_what_each_channel_means():
    """A channel tells a stuck agent whether the fix is in its action space, which it cannot do if
    only the index file on disk says what the name stands for."""
    index = context_documents(CVLR_BUNDLE)[1]
    assert "`SKIP`" in index and "record_skip" in index
    assert "`CONF`" in index and "loop bound" in index


def test_every_recipe_points_at_a_file_that_exists():
    for recipe in _kb_model(CVLR_RECIPES).recipes:
        assert (_RESOURCES / recipe.file).is_file(), f"{recipe.id} points at {recipe.file}"


def test_the_facts_bundle_is_the_same_facts_without_the_index():
    """For the readers with no `get_cvlr_recipe`. The index tells its reader to retrieve a file and
    names the author's tools in its channels, so it is only for an agent that can act on it; the
    facts are the same document either way, so the two cannot drift."""
    assert context_documents(CVLR_FACTS_BUNDLE) == context_documents(CVLR_BUNDLE)[:1]
    assert "get_cvlr_recipe" not in "".join(context_documents(CVLR_FACTS_BUNDLE))


def test_the_author_is_told_sysvars_are_read_directly():
    """With `Sysvar::from_account_info` analyzed rather than external, an author no longer needs to
    split a handler to pass `Clock` or `Rent` in as a parameter. The recipe is what keeps an author
    from reaching for that workaround out of habit."""
    (k3,) = [r for r in _kb_model(CVLR_RECIPES).recipes if r.id == "K3"]
    assert "from_account_info" in k3.search_terms
    text = kb_loader(CVLR_RECIPES)("K3")
    assert text is not None and "cvlr_assume!(*clock_info.key == sysvar::clock::id());" in text
