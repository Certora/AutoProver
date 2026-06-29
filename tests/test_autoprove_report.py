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
from typing import cast
import pathlib

import pytest
from prover_output_utility.models import NodeStatus
from prover_output_utility import ProverOutputAPI
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from composer.spec.types import PropertyFormulation, PropertyType
from composer.spec.cvl_generation import GeneratedCVL, PropertyRuleMapping, SkippedProperty

from composer.pipeline.core import Delivered

from composer.spec.source.artifacts import ProverArtifactStore
from composer.spec.source.report import build
from composer.spec.source.report.collect import ReportComponentInput, collect
from composer.spec.source.report.coverage import ValidationError, validate
from composer.spec.source.report.grouping import (
    FALLBACK_SLUG, GroupingResult, PropertyGroupDraft, aggregate_status,
    build_fallback_grouping, build_groups,
)
from composer.spec.source.report.render import render_html
from composer.spec.source.report.schema import (
    AutoProverReport, CoverageReport, FormalizedProperty, GaveUpComponent, GroupStatus,
    Outcome, PropertyGroup, RuleVerdict, SkippedClaim,
)
from composer.spec.source.report_prover import make_prover_fetcher


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
         skipped: dict[str, str] | None = None) -> GeneratedCVL:
    """A successful generation result: ``mapping`` is property_title -> [rule names];
    ``skipped`` is property_title -> reason."""
    return GeneratedCVL(
        commentary="", cvl="",
        property_rules=[PropertyRuleMapping(property_title=t, rules=rs)
                        for t, rs in (mapping or {}).items()],
        skipped=[SkippedProperty(property_title=t, reason=r)
                 for t, r in (skipped or {}).items()],
    )


def _input(name, unit_file, props, result: GeneratedCVL | None, link : str | None="L1") -> ReportComponentInput[GeneratedCVL]:
    """``link`` is the result's prover run link (``GeneratedCVL.final_link``); the prover fetcher
    keys its verdicts off it. ``None`` (or a ``None`` result) means no run link, so no verdicts."""
    return ReportComponentInput(
        name=name,
        props=props,
        formalized=Delivered(
            deliverable=pathlib.Path(unit_file),
            result=result.model_copy(update={"final_link": link})
        ) if result is not None else None
    )


def _fp(component, title, refs, desc="d", sort: PropertyType = "safety_property") -> FormalizedProperty:
    return FormalizedProperty(component=component, title=title,
                              sort=sort, description=desc, rule_refs=refs)


def _rv(spec, name, outcome=Outcome.GOOD) -> RuleVerdict:
    return RuleVerdict(name=name, spec_file=spec, outcome=outcome)


def _pg(slug, members, status=GroupStatus.GOOD) -> PropertyGroup:
    return PropertyGroup(slug=slug, title="T", description="d", status=status, members=members)


class _GroupingStubModel(BaseChatModel):
    """A `BaseChatModel` whose structured-output binding returns a preset `GroupingResult`.
    Lets the build tests drive the *real* `call_grouping_llm` — template rendering + the
    `isinstance` check — without a live model, only stubbing the model's output."""
    result: GroupingResult

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        result = self.result
        return RunnableLambda(lambda _messages: result)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("stub is structured-output only")

    @property
    def _llm_type(self) -> str:
        return "grouping-stub"


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

    properties, rules, skipped, gave_up, dropped = await collect(
        [_input("Increment", "autospec_Increment.spec", props, gen)], fetch_verdicts=fetch)

    assert [p.title for p in properties] == ["count_increases", "count_eq_sum"]
    assert properties[0].component == "Increment"
    assert properties[0].rule_refs == [("autospec_Increment.spec", "increment_increases_count")]
    by_ref = {r.ref: r for r in rules}
    r = by_ref[("autospec_Increment.spec", "increment_increases_count")]
    assert r.outcome == Outcome.GOOD and r.line == 12 and r.duration_seconds == 1.5
    assert r.prover_link == "L1"
    assert by_ref[("autospec_Increment.spec", "countEqualsSum")].outcome == Outcome.BAD
    assert skipped == [] and gave_up == [] and dropped == 0


@pytest.mark.asyncio
async def test_collect_splits_skipped_property_into_gap():
    props = [_prop("p_done", "formalized"), _prop("p_skip", "cannot express in CVL")]
    gen = _gen({"p_done": ["r1"]}, skipped={"p_skip": "needs a ghost"})
    fetch = _fetcher({"L1": [_fake_check("r1", NodeStatus.VERIFIED)]})

    properties, _rules, skipped, gave_up, _dropped = await collect(
        [_input("C", "autospec_C.spec", props, gen)], fetch_verdicts=fetch)

    assert [p.title for p in properties] == ["p_done"]
    assert [(s.component, s.title, s.reason) for s in skipped] == [("C", "p_skip", "needs a ghost")]
    assert gave_up == []


@pytest.mark.asyncio
async def test_collect_none_result_is_a_gap():
    """A component with no result (the caller maps both give-up and crash to ``None``) is a
    formalization gap — all its properties unimplemented, no per-property reason."""
    props = [_prop("p1", "d1")]
    properties, rules, skipped, gave_up, dropped = await collect(
        [_input("C", "autospec_C.spec", props, None, link=None)], fetch_verdicts=_fetcher({}))
    assert properties == [] and rules == [] and skipped == [] and dropped == 0
    assert [g.component for g in gave_up] == ["C"]
    assert [p.title for p in gave_up[0].properties] == ["p1"]


@pytest.mark.asyncio
async def test_collect_drops_and_counts_orphan_rules():
    """A rule the prover reported but no property maps to is dropped and counted."""
    gen = _gen({"p1": ["r1"]})
    fetch = _fetcher({"L1": [
        _fake_check("r1", NodeStatus.VERIFIED),
        _fake_check("sanity_helper", NodeStatus.VERIFIED),  # referenced by nothing
    ]})
    _props, rules, _skipped, _gave_up, dropped = await collect(
        [_input("C", "autospec_C.spec", [_prop("p1", "d1")], gen)], fetch_verdicts=fetch)
    assert [r.name for r in rules] == ["r1"]
    assert dropped == 1


@pytest.mark.asyncio
async def test_collect_backfills_unknown_for_unproven_referenced_rule():
    gen = _gen({"p1": ["r1"]})
    fetch = _fetcher({"L1": []})  # prover reported no checks
    properties, rules, _s, _g, dropped = await collect(
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
                  _gen({"c": ["countEqualsSum"]}), link="Lc")
    inv = _input("Structural Invariants", "invariants.spec", [_prop("i", "structural", sort="invariant")],
                 _gen({"i": ["countEqualsSum"]}), link="Li")
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
    a = _input("A", "autospec_A.spec", [_prop("pa", "a")], _gen({"pa": ["transferIsSafe"]}), link="La")
    b = _input("B", "autospec_B.spec", [_prop("pb", "b")], _gen({"pb": ["transferIsSafe"]}), link="Lb")
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
                 skipped=[], gave_up=[], dropped_orphan_rules=0)


def test_validate_unknown_property_member_raises():
    props = [_fp("C", "p1", [("s.spec", "a")])]
    groups = [_pg("g", [("C", "ghost")])]
    with pytest.raises(ValidationError, match="don't exist"):
        validate(properties=props, rules=[_rv("s.spec", "a")], groups=groups,
                 skipped=[], gave_up=[], dropped_orphan_rules=0)


def test_validate_property_in_no_group_is_soft():
    props = [_fp("C", "p1", [("s.spec", "a")]), _fp("C", "p2", [("s.spec", "b")])]
    groups = [_pg("g", [("C", "p1")])]
    cov = validate(properties=props, rules=[_rv("s.spec", "a"), _rv("s.spec", "b")],
                   groups=groups, skipped=[], gave_up=[], dropped_orphan_rules=0)
    assert cov.property_coverage_complete is False
    assert cov.properties_in_no_group == [("C", "p2")]


def test_validate_reports_rules_spanning_groups_as_stat():
    """A rule formalizing properties that land in different groups is expected
    (rules repeat) — reported as an informational stat, not an error."""
    p1 = _fp("C", "p1", [("s.spec", "shared")])
    p2 = _fp("C", "p2", [("s.spec", "shared")])
    groups = [_pg("g1", [("C", "p1")]), _pg("g2", [("C", "p2")])]
    cov = validate(properties=[p1, p2], rules=[_rv("s.spec", "shared")], groups=groups,
                   skipped=[], gave_up=[], dropped_orphan_rules=2)
    assert cov.rules_spanning_multiple_groups == ["shared"]
    assert cov.dropped_orphan_rules == 2


def test_validate_carries_gap_counts():
    p1 = _fp("C", "p1", [("s.spec", "a")])
    sk = [SkippedClaim(component="C", title="s1", sort="safety_property",
                       description="d", reason="r")]
    gu = [GaveUpComponent(component="D", properties=[_prop("x", "d")])]
    cov = validate(properties=[p1], rules=[_rv("s.spec", "a")], groups=[_pg("g", [("C", "p1")])],
                   skipped=sk, gave_up=gu, dropped_orphan_rules=3)
    assert (cov.skipped_count, cov.gave_up_component_count, cov.dropped_orphan_rules) == (1, 1, 3)
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


def test_render_html_group_rows_and_edge_labels():
    h = render_html(_mini_report())
    assert "deposit-openness" in h and "Deposit is open" in h
    assert 'href="https://prover.example/run/abc"' in h
    # the shared rule row lists BOTH in-group property descriptions
    assert '<ul class="claims">' in h
    assert "must accept ETH" in h and "callable by any address" in h


def test_render_html_uses_backend_labels():
    """The prover backend renders a GOOD outcome as 'Verified'; foundry renders it 'Successful test'."""
    prover_html = render_html(_mini_report())
    assert "Verified" in prover_html and "Successful test" not in prover_html

    foundry = _mini_report().model_copy(update={"backend": "foundry"})
    foundry_html = render_html(foundry)
    assert "Successful test" in foundry_html and "Verified" not in foundry_html


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
    llm = _GroupingStubModel(result=GroupingResult(groups=[PropertyGroupDraft(
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
    llm = _GroupingStubModel(result=GroupingResult(groups=[]))  # empty grouping -> fallback

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
    llm = _GroupingStubModel(result=GroupingResult(groups=[PropertyGroupDraft(
        slug="g", title="G", description="d", members=[("C", "p_ok")])]))

    report = await build.build_report(
        contract_name="C",
        backend="prover",
        components=[
            _input("C", "autospec_C.spec", [_prop("p_ok", "d"), _prop("p_skip", "d")], gen),
            _input("D", "autospec_D.spec", [_prop("q", "d")], None, link=None),
        ],
        llm=llm, fetch_verdicts=fetch,
    )

    assert [(s.component, s.title) for s in report.skipped] == [("C", "p_skip")]
    assert [g.component for g in report.gave_up_components] == ["D"]
    assert report.coverage.skipped_count == 1 and report.coverage.gave_up_component_count == 1
