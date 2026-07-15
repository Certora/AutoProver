"""Render smoke tests for prompt templates.

These templates are plain-jinja prompt fragments consumed by the property analysis /
generation / judge agents. The tests render them with minimal stand-in objects (jinja
only does attribute lookups, so `SimpleNamespace` suffices) and assert the load-bearing
content is present — a missing template variable or a broken include fails fast here
instead of mid-agent-run.
"""
from types import SimpleNamespace

from composer.templates.loader import load_jinja_template


def test_cvl_guidelines_render():
    out = load_jinja_template("cvl_guidelines.j2")
    # Guideline 24: requireInvariant discipline over bare state requires.
    assert "requireInvariant" in out
    assert "spec smell" in out
    # Guideline 24 carve-out: solver-capacity bounds need no invariant attempt.
    assert "keep the solver tractable" in out
    # Guideline 24 defers preserved blocks to the stricter (judge Criteria 4) standard.
    assert "stricter standard" in out
    # Guideline 20 carve-out: persistent counter ghosts are legitimate (judge-visible
    # counterpart of the ghost-counter advice in cvl_additions.j2).
    assert "never stored in contract state" in out
    # Guideline 23 carve-out: hooks/ghosts for information never stored in contract state.
    assert "not stored in" in out
    assert out.strip().endswith("</cvl_guidelines>")


def test_cvl_additions_render():
    out = load_jinja_template("cvl_additions.j2")
    # Storage snapshot / additivity idiom.
    assert "lastStorage" in out
    assert "at init" in out
    # satisfy-witness companion rules, mapped under their parent property.
    assert "satisfy" in out
    assert "property_rules" in out
    # Ghost counters via expression summaries.
    assert "countDeposit() expect void" in out
    assert out.strip().endswith("</cvl_advice>")


def _fake_component_context() -> SimpleNamespace:
    """Minimal stand-in for ContractComponentInstance as accessed by the template."""
    contract = SimpleNamespace(name="Vault", solidity_identifier="Vault")
    component = SimpleNamespace(
        name="Deposits",
        description="Handles user deposits",
        requirements=["Users receive shares proportional to deposits"],
        interactions=[],
    )
    app = SimpleNamespace(application_type="an ERC4626 vault")
    return SimpleNamespace(component=component, contract=contract, app=app,
                           ommer_contracts=[])


def test_property_analysis_prompt_render():
    out = load_jinja_template(
        "property_analysis_prompt.j2",
        context=_fake_component_context(),
        backend_guidance="BACKEND_GUIDANCE_SENTINEL",
        # A valid Sort value (see composer/spec/service_host.py) exercising the
        # non-greenfield template branch.
        sort="existing",
        prior_properties=[],
    )
    # The context and backend guidance are threaded through.
    assert "Deposits" in out
    assert "BACKEND_GUIDANCE_SENTINEL" in out
    # 6-lens coverage checklist, framed as brainstorming discipline, not a quota.
    assert "Unit behavior" in out
    assert "Variable transition" in out
    assert "Multi-call / high-level" in out
    assert "NOT a quota" in out
    # Task steps ask for a record of swept lenses.
    assert "which of the six coverage" in out


def test_property_analysis_prompt_render_with_prior_rounds():
    prior = [SimpleNamespace(
        items=[SimpleNamespace(sort="invariant", title="solvency",
                               description="assets cover shares")],
        reasoning="looked at deposit accounting",
    )]
    out = load_jinja_template(
        "property_analysis_prompt.j2",
        context=_fake_component_context(),
        backend_guidance="",
        sort="existing",
        prior_properties=prior,
    )
    assert "solvency" in out
    assert "looked at deposit accounting" in out
