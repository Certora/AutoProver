"""Tests for the autoprove report package (composer.spec.source.report).

Property-keyed (schema 3.0). Covers the pure pieces — in-memory collect against a
fake POU (driven through the real prover adapter, so the `NodeStatus -> Outcome`
translation is exercised), outcome aggregation, grouping + fallback, coverage's
property-partition, HTML render — plus the build orchestrator. No DB / no real LLM /
no real prover: POU is faked, the grouping LLM is a `BaseChatModel` stub whose
structured output is preset (so the real `call_grouping_llm` — templates + parsing —
still runs), and inputs are in-memory `GeneratedCVL` (or `None` for a give-up/crash,
which is how a caller hands a gap to the report layer).
"""
from types import SimpleNamespace
from typing import Any, cast
import pathlib

import pytest
from prover_output_utility.models import NodeStatus
from prover_output_utility import ProverOutputAPI
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from composer.spec.types import PropertyFormulation, PropertyType
from composer.authoring.state import SkippedProperty
from composer.spec.cvl_generation import GeneratedCVL, PropertyRuleMapping

from composer.pipeline.core import Curtailed, Delivered

from composer.spec.source.artifacts import ProverArtifactStore
from composer.spec.source.report import build
from composer.spec.source.report.collect import ReportComponentInput, collect
from composer.spec.source.report.coverage import ValidationError, validate
from composer.spec.source.prover_findings import prover_findings
from composer.spec.source.report.findings import AssessedFindingDraft, RuleEvidence
from composer.spec.source.report.grouping import (
    FALLBACK_SLUG, GroupingResult, PropertyGroupDraft, aggregate_status,
    build_fallback_grouping, build_groups,
)
from composer.spec.source.report.render import render_html
from composer.spec.source.report.schema import (
    AutoProverReport, CoverageReport, CurtailedComponent, CurtailedSkip, DraftedProperty, Finding, FindingProvenance, FormalizedProperty,
    GaveUpComponent, GroupStatus, ImpactLevel, IssueContent, LikelihoodLevel, Outcome, PropertyGroup,
    RuleVerdict, SeverityTier,
    SkippedClaim,
)
from composer.spec.source.report_prover import make_prover_fetcher
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.diagnostics.timing import RunSummary


# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------

def _fake_check(rule_name, status, line=None, duration=None, file: str | None = "autospec_Increment.spec"):
    """Stand-in CheckResult. ``status`` is a POU `NodeStatus` (the prover adapter maps it to an
    `Outcome`). ``file`` is the spec the rule is defined in (POU's source location); pass
    ``file=None`` to simulate POU not reporting one."""
    sl = SimpleNamespace(file=file, line=line)
    return SimpleNamespace(rule_name=rule_name, status=status, duration=duration, source_location=sl)


class _FakeAPI_Impl:
    """Stand-in for ProverOutputAPI: get_all_checks(link) -> list of checks."""
    def __init__(self, by_link: dict[str, list]):
        self.by_link = by_link

    def get_all_checks(self, link):
        return self.by_link.get(link, [])


def _FakeAPI(by_link: dict[str, list]) -> ProverOutputAPI:
    return cast(ProverOutputAPI, _FakeAPI_Impl(by_link))


def _fetcher(by_link: dict[str, list]):
    """The real prover `VerdictFetcher` over a fake POU — exercises the NodeStatus->Outcome map."""
    return make_prover_fetcher(_FakeAPI(by_link))


def _prop(title, desc, *, sort: PropertyType = "safety_property") -> PropertyFormulation:
    return PropertyFormulation(title=title, sort=sort, description=desc)


def _gen(mapping: dict[str, list[str]] | None = None,
         skipped: dict[str, str] | None = None,
         link: str | None = "L1") -> GeneratedCVL:
    """A successful generation result: ``mapping`` is property_title -> [rule names];
    ``skipped`` is property_title -> reason."""
    return GeneratedCVL(
        commentary="", cvl="",
        property_rules=[PropertyRuleMapping(property_title=t, rules=rs)
                        for t, rs in (mapping or {}).items()],
        skipped=[SkippedProperty(property_title=t, reason=r)
                 for t, r in (skipped or {}).items()],
        final_link=link
    )


def _input(
    name: str,
    unit_file: str,
    props: list[PropertyFormulation],
    result: GeneratedCVL | None
) -> ReportComponentInput[GeneratedCVL]:
    return ReportComponentInput(name=name, props=props,
                                formalized=Delivered(result, pathlib.Path(unit_file)) if result is not None else None)


def _curtailed_input(
    name, props, result: GeneratedCVL | None, link: str | None = None, detail: str | None = None,
) -> ReportComponentInput[GeneratedCVL]:
    """A budget-curtailed component: ``result`` is the partial the author published under lifted
    gates (quarantined on disk), or ``None`` when the run stopped before anything was published."""
    partial = (
        Delivered(
            deliverable=pathlib.Path(f"autospec_{name}.spec.unverified"),
            result=result.model_copy(update={"final_link": link}),
        )
        if result is not None else None
    )
    return ReportComponentInput(name=name, props=props, formalized=Curtailed(partial, detail))


def _fp(component, title, refs, desc="d", sort: PropertyType = "safety_property") -> FormalizedProperty:
    return FormalizedProperty(component=component, title=title,
                              sort=sort, description=desc, rule_refs=refs)


def _rv(spec, name, outcome=Outcome.GOOD) -> RuleVerdict:
    return RuleVerdict(name=name, spec_file=spec, outcome=outcome)


def _pg(slug, members, status=GroupStatus.GOOD) -> PropertyGroup:
    return PropertyGroup(slug=slug, title="T", description="d", status=status, members=members)


class _StructuredStubModel(BaseChatModel):
    """A `BaseChatModel` whose structured-output binding returns a preset object, so tests drive the
    real caller (template rendering + the `isinstance` check) without a live model."""
    output: Any

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        out = self.output
        return RunnableLambda(lambda _messages: out)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("stub is structured-output only")

    @property
    def _llm_type(self) -> str:
        return "structured-stub"


# ---------------------------------------------------------------------------
# collect (async, in-memory)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_joins_properties_to_rules_and_verdicts():
    props = [_prop("count_increases", "count up by one"),
             _prop("count_eq_sum", "count == sum", sort="invariant")]
    gen = _gen({"count_increases": ["increment_increases_count"], "count_eq_sum": ["countEqualsSum"]})
    fetch = _fetcher({"L1": [
        _fake_check("increment_increases_count", NodeStatus.VERIFIED, line=12, duration=1.5),
        _fake_check("countEqualsSum", NodeStatus.VIOLATED, line=40),
    ]})

    properties, rules, skipped, gave_up, curtailed, dropped = await collect(
        [_input("Increment", "autospec_Increment.spec", props, gen)], fetch_verdicts=fetch)

    assert [p.title for p in properties] == ["count_increases", "count_eq_sum"]
    assert properties[0].component == "Increment"
    assert properties[0].rule_refs == [("autospec_Increment.spec", "increment_increases_count")]
    by_ref = {r.ref: r for r in rules}
    r = by_ref[("autospec_Increment.spec", "increment_increases_count")]
    assert r.outcome == Outcome.GOOD and r.line == 12 and r.duration_seconds == 1.5
    assert r.prover_link == "L1"
    assert by_ref[("autospec_Increment.spec", "countEqualsSum")].outcome == Outcome.BAD
    assert skipped == [] and gave_up == [] and curtailed == [] and dropped == 0


@pytest.mark.asyncio
async def test_collect_splits_skipped_property_into_gap():
    props = [_prop("p_done", "formalized"), _prop("p_skip", "cannot express in CVL")]
    gen = _gen({"p_done": ["r1"]}, skipped={"p_skip": "needs a ghost"})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED)]})

    properties, _rules, skipped, gave_up, _curtailed, _dropped = await collect(
        [_input("C", "autospec_C.spec", props, gen)], fetch_verdicts=fetch)

    assert [p.title for p in properties] == ["p_done"]
    assert [(s.component, s.title, s.reason) for s in skipped] == [("C", "p_skip", "needs a ghost")]
    assert gave_up == []


@pytest.mark.asyncio
async def test_collect_none_result_is_a_gap():
    """A component with no result (the caller maps both give-up and crash to ``None``) is a
    formalization gap — all its properties unimplemented, no per-property reason."""
    props = [_prop("p1", "d1")]
    properties, rules, skipped, gave_up, curtailed, dropped = await collect(
        [_input("C", "autospec_C.spec", props, None)], fetch_verdicts=_fetcher({}))
    assert properties == [] and rules == [] and skipped == [] and curtailed == [] and dropped == 0
    assert [g.component for g in gave_up] == ["C"]
    assert [p.title for p in gave_up[0].properties] == ["p1"]


@pytest.mark.asyncio
async def test_collect_routes_curtailed_component_without_fetching():
    """A budget-curtailed component contributes nothing to properties/rules; its properties are
    partitioned by disposition into the appendix record, and its verdicts are never fetched (the
    fetcher spy would record the call — and its link deliberately maps to a verdict that would
    otherwise register a rule)."""
    good = _input("C", "autospec_C.spec", [_prop("p1", "d1")], _gen({"p1": ["r1"]}, link="Lc"))
    cut = _curtailed_input(
        "D",
        [_prop("q_drafted", "dq"), _prop("q_skip", "ds"), _prop("q_lost", "dl")],
        _gen({"q_drafted": ["draft_rule"]}, skipped={"q_skip": "budget exhausted"}),
        link="Ld",
    )
    base = _fetcher({
        "Lc": [_fake_check("r1", NodeStatus.VERIFIED)],
        "Ld": [_fake_check("draft_rule", NodeStatus.VERIFIED, file="autospec_D.spec.unverified")],
    })
    fetched: list[str] = []

    async def fetch(formalized):
        fetched.append(formalized.unit_file)
        return await base(formalized)

    properties, rules, skipped, gave_up, curtailed, dropped = await collect(
        [good, cut], fetch_verdicts=fetch)

    assert fetched == ["autospec_C.spec"]
    assert [p.title for p in properties] == ["p1"]
    assert [r.name for r in rules] == ["r1"]
    assert skipped == [] and gave_up == [] and dropped == 0
    (c,) = curtailed
    assert c.component == "D"
    assert c.artifact == "autospec_D.spec.unverified"
    assert c.run_link == "Ld"
    assert [(d.title, d.units) for d in c.drafted] == [("q_drafted", ["draft_rule"])]
    assert [(s.title, s.reason) for s in c.skipped] == [("q_skip", "budget exhausted")]
    assert [p.title for p in c.unattempted] == ["q_lost"]


@pytest.mark.asyncio
async def test_collect_curtailed_without_partial_is_all_unattempted():
    """A hard budget stop published nothing: no artifact, no run link, every inferred property
    lands in ``unattempted``."""
    props = [_prop("p1", "d1"), _prop("p2", "d2")]
    properties, rules, _s, gave_up, curtailed, _d = await collect(
        [_curtailed_input("C", props, None, detail="Token cost budget exhausted")],
        fetch_verdicts=_fetcher({}),
    )
    assert properties == [] and rules == [] and gave_up == []
    (c,) = curtailed
    assert c.artifact is None and c.run_link is None
    assert c.detail == "Token cost budget exhausted"
    assert c.drafted == [] and c.skipped == []
    assert [p.title for p in c.unattempted] == ["p1", "p2"]


@pytest.mark.asyncio
async def test_collect_drops_and_counts_orphan_rules():
    """A rule the prover reported but no property maps to is dropped and counted."""
    gen = _gen({"p1": ["r1"]})
    fetch = _fetcher({"L1": [
        _fake_check("r1", NodeStatus.VERIFIED),
        _fake_check("sanity_helper", NodeStatus.VERIFIED),  # referenced by nothing
    ]})
    _props, rules, _skipped, _gave_up, _curtailed, dropped = await collect(
        [_input("C", "autospec_C.spec", [_prop("p1", "d1")], gen)], fetch_verdicts=fetch)
    assert [r.name for r in rules] == ["r1"]
    assert dropped == 1


@pytest.mark.asyncio
async def test_collect_backfills_unknown_for_unproven_referenced_rule():
    gen = _gen({"p1": ["r1"]})
    fetch = _fetcher({"L1": []})  # prover reported no checks
    properties, rules, _s, _g, _c, dropped = await collect(
        [_input("C", "autospec_C.spec", [_prop("p1", "d1")], gen)], fetch_verdicts=fetch)
    assert [(r.name, r.outcome, r.spec_file) for r in rules] == [("r1", Outcome.UNKNOWN, "autospec_C.spec")]
    assert properties[0].rule_refs == [("autospec_C.spec", "r1")]
    assert dropped == 0


@pytest.mark.asyncio
async def test_collect_falls_back_to_input_spec_when_verdict_has_no_source():
    """A verdict without a source location is attributed to the component's own spec
    (no raise — the report is best-effort and every input carries a unit_file)."""
    gen = _gen({"p1": ["r1"]})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED, file=None)]})
    properties, rules, *_ = await collect(
        [_input("C", "autospec_C.spec", [_prop("p1", "d1")], gen)], fetch_verdicts=fetch)
    assert rules[0].ref == ("autospec_C.spec", "r1")
    assert properties[0].rule_refs == [("autospec_C.spec", "r1")]


@pytest.mark.asyncio
async def test_collect_shared_rule_dedupes_and_is_referenced_by_both():
    """An invariant imported into a component spec reports the same source file from
    both runs, so it collapses to one rule that both components' properties reference."""
    comp = _input("Increment", "autospec_Increment.spec", [_prop("c", "component view", sort="invariant")],
                  _gen({"c": ["countEqualsSum"]}, link="Lc"))
    inv = _input("Structural Invariants", "invariants.spec", [_prop("i", "structural", sort="invariant")],
                 _gen({"i": ["countEqualsSum"]}, link="Li"))
    fetch = _fetcher({
        "Lc": [_fake_check("countEqualsSum", NodeStatus.VERIFIED, file="invariants.spec")],
        "Li": [_fake_check("countEqualsSum", NodeStatus.VERIFIED, file="invariants.spec")],
    })
    properties, rules, *_ = await collect([comp, inv], fetch_verdicts=fetch)
    ces = [r for r in rules if r.name == "countEqualsSum"]
    assert len(ces) == 1 and ces[0].spec_file == "invariants.spec"
    assert all(p.rule_refs == [("invariants.spec", "countEqualsSum")] for p in properties)


@pytest.mark.asyncio
async def test_collect_same_name_different_spec_stays_distinct():
    a = _input("A", "autospec_A.spec", [_prop("pa", "a")], _gen({"pa": ["transferIsSafe"]}, link="La"))
    b = _input("B", "autospec_B.spec", [_prop("pb", "b")], _gen({"pb": ["transferIsSafe"]}, link="Lb"))
    fetch = _fetcher({
        "La": [_fake_check("transferIsSafe", NodeStatus.VERIFIED, file="autospec_A.spec")],
        "Lb": [_fake_check("transferIsSafe", NodeStatus.VIOLATED, file="autospec_B.spec")],
    })
    _props, rules, *_ = await collect([a, b], fetch_verdicts=fetch)
    safe = sorted((r for r in rules if r.name == "transferIsSafe"), key=lambda r: r.spec_file)
    assert [(r.spec_file, r.outcome) for r in safe] == [
        ("autospec_A.spec", Outcome.GOOD),
        ("autospec_B.spec", Outcome.BAD),
    ]


# ---------------------------------------------------------------------------
# aggregate_status
# ---------------------------------------------------------------------------

def test_aggregate_status_table():
    assert aggregate_status([]) == GroupStatus.UNKNOWN
    assert aggregate_status([Outcome.GOOD, Outcome.GOOD]) == GroupStatus.GOOD
    assert aggregate_status([Outcome.GOOD, Outcome.BAD]) == GroupStatus.BAD
    assert aggregate_status([Outcome.GOOD, Outcome.TIMEOUT]) == GroupStatus.PARTIAL
    assert aggregate_status([Outcome.TIMEOUT, Outcome.UNKNOWN]) == GroupStatus.UNKNOWN


def test_aggregate_status_idempotent_under_duplicates():
    once = aggregate_status([Outcome.GOOD, Outcome.TIMEOUT])
    twice = aggregate_status([Outcome.GOOD, Outcome.GOOD, Outcome.TIMEOUT])
    assert once == twice == GroupStatus.PARTIAL


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

def test_build_groups_rolls_up_status_over_member_rule_verdicts():
    p1 = _fp("C", "p1", [("s.spec", "a")])
    p2 = _fp("C", "p2", [("s.spec", "b")])
    props_by_key = {p.key: p for p in (p1, p2)}
    rule_outcomes = {("s.spec", "a"): Outcome.GOOD, ("s.spec", "b"): Outcome.BAD}
    draft = PropertyGroupDraft(slug="g", title="G", description="d", members=[("C", "p1"), ("C", "p2")])

    groups = build_groups([draft], props_by_key, rule_outcomes)

    assert len(groups) == 1
    assert groups[0].status == GroupStatus.BAD  # one member rule is BAD
    assert groups[0].members == [("C", "p1"), ("C", "p2")]


def test_build_fallback_grouping_covers_all_properties_once():
    out = build_fallback_grouping([_fp("C", "p1", [("s.spec", "a")]), _fp("D", "p2", [("s.spec", "b")])])
    assert len(out.groups) == 1
    g = out.groups[0]
    assert g.slug == FALLBACK_SLUG
    assert g.members == [("C", "p1"), ("D", "p2")]


# ---------------------------------------------------------------------------
# coverage (property partition; rule repetition is a stat, not an error)
# ---------------------------------------------------------------------------

def test_validate_property_in_two_groups_raises():
    props = [_fp("C", "p1", [("s.spec", "a")])]
    groups = [_pg("g1", [("C", "p1")]), _pg("g2", [("C", "p1")])]
    with pytest.raises(ValidationError, match="multiple groups"):
        validate(properties=props, rules=[_rv("s.spec", "a")], groups=groups,
                 skipped=[], gave_up=[], curtailed=[], dropped_orphan_rules=0)


def test_validate_unknown_property_member_raises():
    props = [_fp("C", "p1", [("s.spec", "a")])]
    groups = [_pg("g", [("C", "ghost")])]
    with pytest.raises(ValidationError, match="don't exist"):
        validate(properties=props, rules=[_rv("s.spec", "a")], groups=groups,
                 skipped=[], gave_up=[], curtailed=[], dropped_orphan_rules=0)


def test_validate_property_in_no_group_is_soft():
    props = [_fp("C", "p1", [("s.spec", "a")]), _fp("C", "p2", [("s.spec", "b")])]
    groups = [_pg("g", [("C", "p1")])]
    cov = validate(properties=props, rules=[_rv("s.spec", "a"), _rv("s.spec", "b")],
                   groups=groups, skipped=[], gave_up=[], curtailed=[], dropped_orphan_rules=0)
    assert cov.property_coverage_complete is False
    assert cov.properties_in_no_group == [("C", "p2")]


def test_validate_reports_rules_spanning_groups_as_stat():
    """A rule formalizing properties that land in different groups is expected
    (rules repeat) — reported as an informational stat, not an error."""
    p1 = _fp("C", "p1", [("s.spec", "shared")])
    p2 = _fp("C", "p2", [("s.spec", "shared")])
    groups = [_pg("g1", [("C", "p1")]), _pg("g2", [("C", "p2")])]
    cov = validate(properties=[p1, p2], rules=[_rv("s.spec", "shared")], groups=groups,
                   skipped=[], gave_up=[], curtailed=[], dropped_orphan_rules=2)
    assert cov.rules_spanning_multiple_groups == ["shared"]
    assert cov.dropped_orphan_rules == 2


def test_validate_carries_gap_counts():
    p1 = _fp("C", "p1", [("s.spec", "a")])
    sk = [SkippedClaim(component="C", title="s1", sort="safety_property",
                       description="d", reason="r")]
    gu = [GaveUpComponent(component="D", properties=[_prop("x", "d")])]
    cu = [CurtailedComponent(component="E", unattempted=[_prop("y", "d")])]
    cov = validate(properties=[p1], rules=[_rv("s.spec", "a")], groups=[_pg("g", [("C", "p1")])],
                   skipped=sk, gave_up=gu, curtailed=cu, dropped_orphan_rules=3)
    assert (cov.skipped_count, cov.gave_up_component_count, cov.dropped_orphan_rules) == (1, 1, 3)
    assert cov.curtailed_component_count == 1
    assert any("cut short by the run budget" in w for w in cov.warnings)
    assert cov.property_coverage_complete is True


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _mini_report() -> AutoProverReport:
    # Two properties in one group share a single rule -> the rule row should carry
    # both in-group descriptions as a bullet list (the edge-label projection).
    p1 = _fp("C", "p_pay", [("c.spec", "revert_char")], desc="must accept ETH when value > 0")
    p2 = _fp("C", "p_open", [("c.spec", "revert_char")], desc="callable by any address")
    rules = [RuleVerdict(name="revert_char", spec_file="c.spec", outcome=Outcome.GOOD,
                         line=7, prover_link="https://prover.example/run/abc")]
    groups = [PropertyGroup(slug="deposit-openness", title="Deposit is open", description="d",
                            status=GroupStatus.GOOD, members=[("C", "p_pay"), ("C", "p_open")])]
    skipped = [SkippedClaim(component="C", title="atomic_on_revert",
                            sort="safety_property", description="revert rolls back state",
                            reason="tautological under EVM semantics")]
    cov = CoverageReport(total_properties=2, total_rules=1, total_groups=1,
                         properties_per_group_min=2, properties_per_group_max=2,
                         property_coverage_complete=True)
    return AutoProverReport(contract_name="Counter", backend="prover",
                            prover_links={"C": "https://prover.example/run/abc"},
                            properties=[p1, p2], rules=rules, groups=groups,
                            skipped=skipped, coverage=cov)


def test_render_html_shows_finding_message():
    # A backend diagnostic (a counterexample / failed-assertion message, for a backend that
    # supplies one) is surfaced on the rule row, so a non-GOOD verdict explains itself.
    p1 = _fp("C", "p_dep", [("c.spec", "c_deposit")], desc="deposit increases balance")
    rules = [RuleVerdict(name="c_deposit", spec_file="c.spec", outcome=Outcome.BAD,
                         message="crash abc: deposit(5) - expected 105 got 100")]
    groups = [PropertyGroup(slug="deposits", title="Deposits", description="d",
                            status=GroupStatus.BAD, members=[("C", "p_dep")])]
    cov = CoverageReport(total_properties=1, total_rules=1, total_groups=1,
                         properties_per_group_min=1, properties_per_group_max=1,
                         property_coverage_complete=True)
    h = render_html(AutoProverReport(contract_name="Vault", backend="foundry",
                                     properties=[p1], rules=rules, groups=groups,
                                     skipped=[], coverage=cov))
    assert 'class="finding"' in h
    assert "crash abc: deposit(5) - expected 105 got 100" in h


def test_render_html_group_rows_and_edge_labels():
    h = render_html(_mini_report())
    assert "deposit-openness" in h and "Deposit is open" in h
    assert 'href="https://prover.example/run/abc"' in h
    # the shared rule row lists BOTH in-group property descriptions
    assert '<ul class="claims">' in h
    assert "must accept ETH" in h and "callable by any address" in h


def test_render_html_uses_backend_labels():
    """Each backend renders a GOOD outcome in its own vocabulary, not another backend's."""
    prover_html = render_html(_mini_report())
    assert "Verified" in prover_html and "Successful test" not in prover_html

    foundry = _mini_report().model_copy(update={"backend": "foundry"})
    foundry_html = render_html(foundry)
    assert "Successful test" in foundry_html and "Verified" not in foundry_html

    crucible = _mini_report().model_copy(update={"backend": "crucible"})
    crucible_html = render_html(crucible)
    assert "No counterexample" in crucible_html and "Verified" not in crucible_html


def test_render_html_uses_backend_nouns():
    """Chrome prose follows the backend: a prover report says 'Formal verification report' / 'CVL
    rules'; a foundry report says 'Foundry test report' / 'tests' and leaks neither prover noun."""
    prover_html = render_html(_mini_report())
    assert "Formal verification report" in prover_html and "CVL rules" in prover_html

    foundry_html = render_html(_mini_report().model_copy(update={"backend": "foundry"}))
    assert "Foundry test report" in foundry_html and "Test outcomes" in foundry_html
    assert "Formal verification report" not in foundry_html
    assert "CVL rules" not in foundry_html


def test_render_html_autoescapes_descriptions():
    h = render_html(_mini_report())
    assert "value &gt; 0" in h  # the ">" in the description is escaped, not raw


def test_render_html_gaps_section_and_footer_bool():
    h = render_html(_mini_report())
    assert "Formalization gaps" in h
    assert "revert rolls back state" in h and "tautological under EVM semantics" in h
    assert "Coverage complete: <strong>Yes</strong>" in h  # no raw Python bool


def test_render_html_omits_link_column_without_links():
    """A report whose rules carry no run link (e.g. foundry) renders no link column / runs header."""
    report = _mini_report().model_copy(update={
        "backend": "foundry",
        "prover_links": {},
        "rules": [RuleVerdict(name="revert_char", spec_file="c.spec", outcome=Outcome.GOOD, line=7)],
    })
    h = render_html(report)
    assert "prover.example" not in h
    assert "Prover runs" not in h


def test_render_html_budget_appendix():
    """A curtailed component renders as the budget appendix: status chip, counts-first summary,
    quarantined artifact path, per-property dispositions, and the footer count."""
    base = _mini_report()
    report = base.model_copy(update={
        "curtailed_components": [CurtailedComponent(
            component="D",
            artifact="autospec_D.spec.unverified",
            drafted=[DraftedProperty(title="q1", sort="safety_property",
                                     description="drafted claim", units=["rq"])],
            skipped=[CurtailedSkip(title="q2", sort="safety_property",
                                   description="skipped claim", reason="budget exhausted")],
            unattempted=[_prop("q3", "never reached")],
        )],
        "coverage": base.coverage.model_copy(update={"curtailed_component_count": 1}),
    })
    h = render_html(report)
    assert "Appendix: cut short by the run budget" in h
    assert "partial draft published" in h
    assert "autospec_D.spec.unverified" in h
    assert "Of 3 inferred properties: 1 drafted but never verified, 1 skipped, 1 never attempted." in h
    assert "Drafted — unverified" in h and "Not attempted" in h
    assert "budget exhausted" in h and "never reached" in h
    assert "1 component(s) were cut short by the run budget" in h  # footer


def test_render_html_without_curtailed_has_no_appendix():
    assert "Appendix: cut short" not in render_html(_mini_report())


# ---------------------------------------------------------------------------
# build orchestrator (async)
# ---------------------------------------------------------------------------

def test_artifact_store_write_report_round_trips(tmp_path):
    report = _mini_report()
    ProverArtifactStore(str(tmp_path), "Counter").write_report(report)

    out = tmp_path / "certora" / "ap_report" / "report.json"
    assert out.is_file()
    reloaded = AutoProverReport.model_validate_json(out.read_text())
    assert reloaded.contract_name == "Counter"


@pytest.mark.asyncio
async def test_build_groups_properties(tmp_path):
    gen = _gen({"p1": ["r1"], "p2": ["r2"]})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED), _fake_check("r2", NodeStatus.VERIFIED)]})
    llm = _StructuredStubModel(output=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="d", members=[("C", "p1"), ("C", "p2")])]))

    report = await build.build_report(
        contract_name="Counter",
        backend="prover",
        components=[_input("C", "autospec_C.spec", [_prop("p1", "d1"), _prop("p2", "d2")], gen)],
        llm=llm, fetch_verdicts=fetch,
    )

    assert [g.slug for g in report.groups] == ["g"]
    assert {p.title for p in report.properties} == {"p1", "p2"}
    assert report.coverage.property_coverage_complete is True


@pytest.mark.asyncio
async def test_build_empty_grouping_falls_back(tmp_path):
    gen = _gen({"p1": ["r1"], "p2": ["r2"]})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED), _fake_check("r2", NodeStatus.VIOLATED)]})
    llm = _StructuredStubModel(output=GroupingResult(groups=[]))  # empty grouping -> fallback

    report = await build.build_report(
        contract_name="C",
        backend="prover",
        components=[_input("C", "autospec_C.spec", [_prop("p1", "d1"), _prop("p2", "d2")], gen)],
        llm=llm, fetch_verdicts=fetch,
    )

    assert [g.slug for g in report.groups] == [FALLBACK_SLUG]
    g = report.groups[0]
    assert set(g.members) == {("C", "p1"), ("C", "p2")}
    assert g.status == GroupStatus.BAD  # r2 violated
    assert any("FALLBACK GROUPING APPLIED" in w for w in report.coverage.warnings)


@pytest.mark.asyncio
async def test_build_surfaces_skipped_and_gave_up_gaps(tmp_path):
    gen = _gen({"p_ok": ["r1"]}, skipped={"p_skip": "needs a ghost"})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED)]})
    llm = _StructuredStubModel(output=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="d", members=[("C", "p_ok")])]))

    report = await build.build_report(
        contract_name="C",
        backend="prover",
        components=[
            _input("C", "autospec_C.spec", [_prop("p_ok", "d"), _prop("p_skip", "d")], gen),
            _input("D", "autospec_D.spec", [_prop("q", "d")], None),
        ],
        llm=llm, fetch_verdicts=fetch,
    )

    assert [(s.component, s.title) for s in report.skipped] == [("C", "p_skip")]
    assert [g.component for g in report.gave_up_components] == ["D"]
    assert report.coverage.skipped_count == 1 and report.coverage.gave_up_component_count == 1


@pytest.mark.asyncio
async def test_build_curtailed_is_appendixed_not_grouped(tmp_path):
    """A curtailed component stays out of properties/groups/prover_links and lands in the
    appendix + coverage count/warning; the delivered component grounds the report as usual."""
    gen = _gen({"p1": ["r1"]})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED)]})
    llm = _StructuredStubModel(output=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="d", members=[("C", "p1")])]))

    report = await build.build_report(
        contract_name="C",
        backend="prover",
        components=[
            _input("C", "autospec_C.spec", [_prop("p1", "d1")], gen),
            _curtailed_input("D", [_prop("q", "d")], _gen({"q": ["rq"]}), link="Lq"),
        ],
        llm=llm, fetch_verdicts=fetch,
    )

    assert {p.key for p in report.properties} == {("C", "p1")}
    assert all(("D", "q") not in g.members for g in report.groups)
    (c,) = report.curtailed_components
    assert c.component == "D" and [d.title for d in c.drafted] == ["q"]
    assert report.coverage.curtailed_component_count == 1
    assert any("cut short by the run budget" in w for w in report.coverage.warnings)
    # The curtailed partial's stale run link stays out of the header map.
    assert "D" not in report.prover_links


@pytest.mark.asyncio
async def test_build_all_curtailed_skips_the_grouping_call(monkeypatch):
    """With zero formalized properties there is nothing to group: the grouping LLM must not be
    consulted (the exploding stub + re-raise mode make a stray call fail loudly), and the report
    degrades to no groups + a populated appendix."""
    monkeypatch.setattr(build, "RERAISE_REPORT_FAILURES", True)

    class _ExplodingModel(_StructuredStubModel):
        def with_structured_output(self, schema, **kwargs):  # type: ignore[override]
            raise AssertionError("grouping LLM consulted despite no formalized properties")

    report = await build.build_report(
        contract_name="C",
        backend="prover",
        components=[_curtailed_input("C", [_prop("p1", "d1")], None, detail="budget exhausted")],
        llm=_ExplodingModel(output=GroupingResult(groups=[])),
        fetch_verdicts=_fetcher({}),
    )

    assert report.properties == [] and report.groups == [] and report.rules == []
    (c,) = report.curtailed_components
    assert c.artifact is None and [p.title for p in c.unattempted] == ["p1"]
    assert report.coverage.curtailed_component_count == 1
    assert report.prover_links == {}
# ---------------------------------------------------------------------------
# findings (violated rules -> audit-issue findings)
# ---------------------------------------------------------------------------


def _finding(severity: SeverityTier = "high") -> Finding:
    return Finding(
        title="Reentrancy drains vault", severity=severity,
        content=IssueContent(summary="s", description="d", impact="funds at risk",
                             assumptions_and_uncertainties="assumes attacker deploys a contract",
                             proof_of_concept="<cex/>"),
        provenance=FindingProvenance(rule_name="r_bad", spec_file="c.spec", outcome=Outcome.BAD,
                                     group_slugs=["g"], prover_link="https://prover.example/run/abc",
                                     impact="high", likelihood="medium",
                                     risk_reasoning="High impact; medium likelihood."),
    )


def _draft(impact_level: ImpactLevel = "high",
           likelihood_level: LikelihoodLevel = "medium") -> AssessedFindingDraft:
    return AssessedFindingDraft(
        title="Reentrancy drains vault",
        impact_level=impact_level, likelihood_level=likelihood_level,
        risk_reasoning="High impact; medium likelihood.",
        summary="s", description="d", impact="funds at risk", attack_path="1..2..3",
    )


def _synthesis(by_rule: dict[str, list[RuleEvidence]]):
    """The prover's synthesis over a canned evidence store, keyed as the report keys rows."""
    async def fetch(ref):
        return by_rule.get(ref[1], [])
    return prover_findings(fetch)


@pytest.mark.asyncio
async def test_build_report_synthesizes_one_finding_per_violation():
    """Only the violated rule becomes a finding; severity is computed from the model's impact/
    likelihood, the counterexample rides proof_of_concept, the run link is a reference, and provenance
    traces back to the rule."""
    gen = _gen({"p_good": ["r_ok"], "p_bad": ["r_bad"]})
    fetch = _fetcher({"L1": [
        _fake_check("r_ok", NodeStatus.VERIFIED, file="autospec_C.spec"),
        _fake_check("r_bad", NodeStatus.VIOLATED, line=42, file="autospec_C.spec"),
    ]})
    grouping = _StructuredStubModel(output=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="gd", members=[("C", "p_good"), ("C", "p_bad")])]))
    synthesis = _synthesis({"r_bad": [RuleEvidence(analysis="root cause X", counterexample="<cex/>")]})

    report = await build.build_report(
        contract_name="Vault", backend="prover",
        components=[_input("C", "autospec_C.spec",
                           [_prop("p_good", "d"), _prop("p_bad", "d")], gen)],
        llm=grouping, fetch_verdicts=fetch,
        findings_llm=_StructuredStubModel(output=_draft()), findings=synthesis,
    )

    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.severity == "high"                            # computed: impact high × likelihood medium
    prov = f.provenance
    assert prov is not None
    assert prov.impact == "high" and prov.likelihood == "medium"
    assert prov.rule_name == "r_bad" and prov.outcome == Outcome.BAD
    assert prov.group_slugs == ["g"] and prov.spec_file == "autospec_C.spec"
    assert f.content.proof_of_concept == "<cex/>"
    assert f.content.references == ["L1"]


@pytest.mark.asyncio
async def test_build_report_no_findings_without_findings_llm():
    """The findings pass is opt-in: omitting ``findings_llm`` leaves findings empty."""
    gen = _gen({"p_bad": ["r_bad"]})
    fetch = _fetcher({"L1": [_fake_check("r_bad", NodeStatus.VIOLATED)]})
    grouping = _StructuredStubModel(output=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="d", members=[("C", "p_bad")])]))
    report = await build.build_report(
        contract_name="C", backend="prover",
        components=[_input("C", "autospec_C.spec", [_prop("p_bad", "d")], gen)],
        llm=grouping, fetch_verdicts=fetch,
    )
    assert report.findings == []


@pytest.mark.asyncio
async def test_a_failing_findings_pass_still_yields_a_report():
    """A failing synthesis must not take the report down with it."""
    gen = _gen({"p_bad": ["r_bad"]})
    fetch = _fetcher({"L1": [_fake_check("r_bad", NodeStatus.VIOLATED)]})
    grouping = _StructuredStubModel(output=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="d", members=[("C", "p_bad")])]))

    async def _boom(_ref):
        raise RuntimeError("evidence store exploded")

    report = await build.build_report(
        contract_name="C", backend="prover",
        components=[_input("C", "autospec_C.spec", [_prop("p_bad", "d")], gen)],
        llm=grouping, fetch_verdicts=fetch,
        findings_llm=_StructuredStubModel(output=_draft()), findings=prover_findings(_boom),
    )
    assert report.findings == []
    assert report.rules  # the rest of the report is intact


def test_render_html_findings_section():
    report = _mini_report().model_copy(update={"findings": [_finding()]})
    h = render_html(report)
    assert '<section class="finding">' in h
    assert "Reentrancy drains vault" in h
    assert "badge-bad" in h                       # high severity
    assert "r_bad" in h and "c.spec" in h         # provenance locator (rule in spec file)
    assert "Severity rationale" in h
    assert "Impact high" in h and "Likelihood medium" in h
    assert "Assumptions" in h and "assumes attacker deploys a contract" in h


def test_render_html_omits_findings_section_when_empty():
    h = render_html(_mini_report())               # _mini_report has no findings
    assert '<section class="finding">' not in h
    assert '<section class="findings-head">' not in h


def test_report_round_trips_with_findings(tmp_path):
    report = _mini_report().model_copy(update={"findings": [_finding()]})
    ProverArtifactStore(str(tmp_path), "Counter").write_report(report)

    out = tmp_path / "certora" / "ap_report" / "report.json"
    reloaded = AutoProverReport.model_validate_json(out.read_text())
    assert len(reloaded.findings) == 1
    rf = reloaded.findings[0]
    assert rf.content.impact == "funds at risk"
    prov = rf.provenance
    assert prov is not None
    assert prov.rule_name == "r_bad"
    assert prov.impact == "high"
    assert prov.risk_reasoning == "High impact; medium likelihood."
    assert not hasattr(rf, "locations")


@pytest.mark.asyncio
def _capture(events: list | None = None):
    """(store, callbacks) over an in-memory store, as the prover tool wires them."""
    from langgraph.store.memory import InMemoryStore
    from composer.spec.source.prover import _SpecCallbacks
    store = CexAnalysisStore(store=InMemoryStore(), namespace=("cex_analyses", "t"))
    summary = SimpleNamespace(add_prover_call=lambda _elapsed: None)
    cb = _SpecCallbacks((events if events is not None else []).append, "tc1",
                        cast(RunSummary, summary), {}, analysis_store=store)
    return store, cb


def _violated(rule: str, method: str | None = None, cex: str = "<cex/>"):
    from composer.prover.ptypes import RulePath, RuleResult
    return RuleResult(path=RulePath(rule=rule, method=method), cex_dump=cex, status="VIOLATED")


@pytest.mark.asyncio
async def test_spec_callbacks_captures_cex_analysis():
    """The callback records a violated rule's analysis, readable back under the bare rule name (what
    the report's RuleVerdict.name carries), while still emitting the stream event."""
    events: list = []
    store, cb = _capture(events)
    await cb.on_analysis_complete(_violated("no_reentrancy", "withdraw"), "root cause: CEI")

    recs = await store.for_rule("no_reentrancy")
    assert [(r.label, r.analysis, r.counterexample) for r in recs] == [
        ("withdraw", "root cause: CEI", "<cex/>")]
    assert await store.for_rule("no_reentrancy for withdraw") == []   # not the pretty-printed form
    assert any(e.get("type") == "rule_analysis" for e in events)


@pytest.mark.asyncio
async def test_parametric_instantiations_are_all_kept_and_purged_when_stale():
    """A parametric rule is analyzed once per binding: every instantiation survives (keying by rule
    alone would overwrite all but one), re-analysis replaces just that binding, and a fresh run on the
    rule drops bindings that no longer fail."""
    store, cb = _capture()
    await cb.on_analysis_complete(_violated("r", "foo", "<cex-foo/>"), "foo breaks")
    await cb.on_analysis_complete(_violated("r", "bar", "<cex-bar/>"), "bar breaks")
    assert [(r.label, r.analysis) for r in await store.for_rule("r")] == [
        ("bar", "bar breaks"), ("foo", "foo breaks")]      # both kept, stable order

    await cb.on_analysis_complete(_violated("r", "foo"), "foo breaks differently")
    assert [r.analysis for r in await store.for_rule("r")] == ["bar breaks", "foo breaks differently"]

    # A later run covers "r" again and only foo fails now -> bar's stale analysis must not linger.
    await cb.on_prover_result({"r": _violated("r", "foo")})
    await cb.on_analysis_complete(_violated("r", "foo"), "foo still breaks")
    assert [(r.label, r.analysis) for r in await store.for_rule("r")] == [("foo", "foo still breaks")]
