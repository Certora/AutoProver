"""Unit tests for the analysis prompts' optional-design-document handling.

``run_component_analysis`` takes ``input: SystemDoc | None`` and renders the analysis prompts with
``has_doc``; the ``input_phrase`` macro (``composer/templates/shared/analysis_macros.j2``) is the
one place that decides how each ecosystem's prompts refer to their inputs in both cases.

These are rendering tests, not golden snapshots: they assert the *branch* each combination takes
(and that no branch promises a document that was never supplied), so the prose stays free to
change. The regression they exist for is silent: the macro used to be imported without
``with context``, which left ``has_doc``/``sort`` Undefined inside it and rendered the no-document
phrasing for every caller — including greenfield, where the document is the only input.
"""

import pytest

from composer.templates.loader import load_jinja_template

EVM_TEMPLATES = ["application_analysis_system.j2", "application_analysis_prompt.j2"]
SOLANA_TEMPLATES = ["solana/analysis_system.j2", "solana/analysis_prompt.j2"]
ALL_TEMPLATES = EVM_TEMPLATES + SOLANA_TEMPLATES


@pytest.mark.parametrize("template", ALL_TEMPLATES)
def test_with_a_document_the_prompt_names_both_inputs(template: str) -> None:
    rendered = load_jinja_template(template, sort="existing", has_doc=True)
    assert "system/design document and the" in rendered


@pytest.mark.parametrize("template", ALL_TEMPLATES)
def test_without_a_document_the_prompt_never_mentions_one(template: str) -> None:
    rendered = load_jinja_template(template, sort="existing", has_doc=False)
    # "No design document accompanies it" is the background's way of saying it is absent; the
    # claim under test is that nothing tells the agent it *was* given one.
    assert "provided a system/design document" not in rendered
    assert "analyzing the system/design document" not in rendered


@pytest.mark.parametrize("template", EVM_TEMPLATES)
def test_greenfield_names_only_the_document(template: str) -> None:
    # Greenfield is EVM-only (``Ecosystem.supports_greenfield``) and document-only: there is no
    # implementation yet, so the input phrase must not offer to read one.
    rendered = load_jinja_template(template, sort="greenfield", has_doc=True)
    assert "system/design document" in rendered
    assert "the system/design document and the implementation" not in rendered


@pytest.mark.parametrize("template", ALL_TEMPLATES)
@pytest.mark.parametrize("has_doc", [True, False])
def test_the_input_phrase_carries_no_article_of_its_own(template: str, has_doc: bool) -> None:
    # Call sites write "the {{ input_phrase() }}", so a branch that also leads with "the" doubles
    # it — which is exactly what the broken no-document branch used to produce.
    rendered = load_jinja_template(template, sort="existing", has_doc=has_doc)
    assert "the the" not in rendered
