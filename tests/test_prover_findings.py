"""Tests for the Certora Prover's findings write-up.

The LLM is a stub with preset structured output, so the real call path
(templates, isinstance check, severity matrix) still runs.
"""
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from composer.spec.types import PropertyType
from composer.spec.source.report.findings import (
    FindingDraft, RuleEvidence, build_findings, severity_for,
)
from composer.spec.source.prover_findings import prover_findings
from composer.spec.source.report.schema import (
    FormalizedProperty, GroupStatus, ImpactLevel, LikelihoodLevel, Outcome, PropertyGroup,
    RuleVerdict,
)


def _fp(component, title, refs, desc="d", sort: PropertyType = "safety_property") -> FormalizedProperty:
    return FormalizedProperty(component=component, title=title,
                              sort=sort, description=desc, rule_refs=refs)


def _rv(spec, name, outcome=Outcome.GOOD, prover_link=None) -> RuleVerdict:
    return RuleVerdict(name=name, spec_file=spec, outcome=outcome, prover_link=prover_link)


def _pg(slug, members, status=GroupStatus.GOOD) -> PropertyGroup:
    return PropertyGroup(slug=slug, title="T", description="d", status=status, members=members)


class _StructuredStubModel(BaseChatModel):
    """`BaseChatModel` stub: structured output is preset; no live model."""
    output: Any

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        out = self.output
        return RunnableLambda(lambda _messages: out)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("stub is structured-output only")

    @property
    def _llm_type(self) -> str:
        return "structured-stub"


def _draft(*, impact_level: ImpactLevel = "high", likelihood_level: LikelihoodLevel = "medium",
           title: str = "Reentrancy drains vault") -> FindingDraft:
    return FindingDraft(
        title=title, impact_level=impact_level, likelihood_level=likelihood_level,
        risk_reasoning="High impact (fund loss); medium likelihood (needs a specific state).",
        summary="s", description="d", impact="funds at risk", attack_path="1..2..3",
    )


def _policy(by_rule: dict[str, list[RuleEvidence]]):
    async def fetch(ref):
        return by_rule.get(ref[1], [])
    return prover_findings(fetch)


def test_severity_for_matrix():
    """Severity comes from the impact × likelihood matrix, not from the LLM."""
    assert severity_for("high", "high") == "critical"
    assert severity_for("high", "medium") == "high"
    assert severity_for("high", "low") == "medium"
    assert severity_for("medium", "high") == "high"
    assert severity_for("medium", "medium") == "medium"
    assert severity_for("low", "high") == "low"    # low impact caps at low regardless of likelihood
    assert severity_for("low", "low") == "low"
    assert severity_for("none", "high") == "informational"   # no exploit path -> informational
    assert severity_for("none", "low") == "informational"


@pytest.mark.asyncio
async def test_one_finding_per_violation():
    """One finding per BAD rule: computed severity, CEX as proof of concept, provenance back to the rule."""
    rules = [_rv("autospec_C.spec", "r_ok"),
             _rv("autospec_C.spec", "r_bad", Outcome.BAD, prover_link="L1")]
    props = [_fp("C", "p_good", [("autospec_C.spec", "r_ok")]),
             _fp("C", "p_bad", [("autospec_C.spec", "r_bad")])]
    groups = [_pg("g", [("C", "p_good"), ("C", "p_bad")], status=GroupStatus.BAD)]

    findings = await build_findings(
        contract_name="Vault", rules=rules, properties=props, groups=groups,
        policy=_policy({"r_bad": [RuleEvidence(analysis="root cause X",
                                                         counterexample="<cex/>")]}),
        llm=_StructuredStubModel(output=_draft()),
    )

    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "high"                            # computed: impact high × likelihood medium
    prov = f.provenance
    assert prov is not None
    assert prov.impact == "high" and prov.likelihood == "medium"
    assert prov.risk_reasoning                             # the axis justification is captured
    assert prov.rule_name == "r_bad" and prov.outcome == Outcome.BAD
    assert prov.group_slugs == ["g"] and prov.spec_file == "autospec_C.spec"
    assert not hasattr(f, "locations")  # locations are a submission-layer concern, not on the report
    assert f.content.proof_of_concept == "<cex/>"
    assert f.content.references == ["L1"]


@pytest.mark.asyncio
async def test_degrades_without_analysis():
    """A violation with no captured evidence still becomes a finding; proof of concept is absent."""
    findings = await build_findings(
        contract_name="Vault", rules=[_rv("c.spec", "r_bad", Outcome.BAD)],
        properties=[_fp("C", "p_bad", [("c.spec", "r_bad")], desc="balances stay solvent")],
        groups=[_pg("g", [("C", "p_bad")], status=GroupStatus.BAD)],
        policy=_policy({}),  # policy present, but no evidence recorded for r_bad
        llm=_StructuredStubModel(output=_draft(impact_level="medium", likelihood_level="medium")),
    )
    assert len(findings) == 1
    assert findings[0].severity == "medium"               # medium × medium
    assert findings[0].content.proof_of_concept is None
    prov = findings[0].provenance
    assert prov is not None and prov.spec_file == "c.spec"


@pytest.mark.asyncio
async def test_a_failed_synthesis_drops_only_that_finding():
    """One failed write-up must not drop the others."""
    class _HalfBroken(_StructuredStubModel):
        def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
            def _run(messages):
                if "r_boom" in messages[-1].content:
                    raise RuntimeError("model refused")
                return _draft()
            return RunnableLambda(_run)

    rules = [_rv("c.spec", "r_boom", Outcome.BAD), _rv("c.spec", "r_bad", Outcome.BAD)]
    props = [_fp("C", "p_boom", [("c.spec", "r_boom")]), _fp("C", "p_bad", [("c.spec", "r_bad")])]
    findings = await build_findings(
        contract_name="Vault", rules=rules, properties=props, groups=[],
        policy=_policy({}), llm=_HalfBroken(output=_draft()),
    )
    assert [f.provenance.rule_name for f in findings if f.provenance] == ["r_bad"]


def test_prompt_lists_all_properties_a_rule_formalizes():
    """The prompt lists every property a rule formalizes, not a pre-picked one."""
    from composer.templates.loader import load_jinja_template
    user = load_jinja_template(
        "autoprove_report_findings_prompt.j2",
        contract_name="Vault", rule_name="allowDeposits_revert_characteristic",
        properties=[_fp("C", "revert_on_non_admin", [], desc="reverts if caller is not an admin"),
                    _fp("C", "revert_on_zero_actor", [], desc="reverts if allowedActor is address(0)")],
        groups=[], instances=[RuleEvidence(analysis="the admin check is skipped", counterexample="<cex/>")],
    )
    assert "reverts if caller is not an admin" in user
    assert "reverts if allowedActor is address(0)" in user
