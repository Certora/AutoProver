"""The CVLR knowledge bundle, and the line between it and an agent's own prompt.

The bundle is a cached prefix: its documents are read once per process and sit behind a cache
marker, in front of every CVLR agent. That is what makes it cheap to give the same facts to the
author, the judge and the munge editor, and it is also the constraint — a document that varies per
run cannot live here, and one that names a tool only the author holds should not.
"""

import re
from pathlib import Path

from composer.kb.kb_context import CVLR_BUNDLE, context_documents

_FACTS = Path(__file__).parent.parent / "composer" / "kb" / "resources" / "cvlr_baseline_facts.md"
_AUTHOR_PROMPT = (
    Path(__file__).parent.parent
    / "composer"
    / "templates"
    / "cvlr_property_generation_system_prompt.j2"
)


def _headings(text: str) -> set[str]:
    return set(re.findall(r"^#{2,3} (.+)$", text, re.MULTILINE))


def test_the_bundle_carries_no_per_run_material():
    """A jinja expression that reaches a resource file is not rendered — it is handed to the model
    verbatim, as `{{ example.handler }}`. The failure is silent and reads as a corrupted prompt, so
    the guard is worth more than the thing it checks."""
    assert "{{" not in _FACTS.read_text() and "{%" not in _FACTS.read_text()


def test_no_section_is_in_both_the_bundle_and_the_author_prompt():
    """Both are in the author's context at once, so a section in both is the same text twice —
    which is what the factoring was for."""
    assert not (_headings(_FACTS.read_text()) & _headings(_AUTHOR_PROMPT.read_text()))


def test_the_author_prompt_keeps_what_only_the_author_can_act_on():
    """The split is by *audience*, not by topic: the tools, the publish contract and the run's own
    conf mean nothing to a judge or an editor, and the author cannot work without them."""
    kept = _headings(_AUTHOR_PROMPT.read_text())
    assert {"Output shape", "Tools", "Workflow tools"} <= kept
    assert any("prover configuration" in h for h in kept)


def test_the_bundle_keeps_what_every_cvlr_agent_needs():
    facts = _headings(_FACTS.read_text())
    assert any("Nondeterminism" in h for h in facts)
    assert any("Assumptions" in h for h in facts)
    assert any("nonlinear" in h for h in facts)


def test_the_bundle_is_one_document_until_it_has_recipes():
    """Recipes arrive with the material to fill them. Until then the tool would answer every id
    with a miss and the index would advertise it."""
    assert len(context_documents(CVLR_BUNDLE)) == 1
