"""Render an `AutoProverReport` (report.json) as a standalone HTML report.

Single self-contained page (inline CSS, no external assets): a header with outcome counts, one
section per high-level `PropertyGroup` (status badge + description + a rule table whose per-rule
descriptions are the in-group property claims that pull each rule in), a formalization-gaps section
(declined properties + components that gave up), and a coverage footer. The HTML is built by
``autoprove_report.html.j2``; this module only assembles the render context — no markup here. The
template's parameters are typed by `ReportTemplateParams` and rendered through the `TypedTemplate`
infra, so a context/template drift is a type error.

Outcome **colour** is backend-independent (a GOOD outcome is green whether it was proven or merely
tested); the outcome **label** ("Verified" vs "Successful test") and the prose **nouns** ("CVL
rules" vs "tests") are chosen from the report's ``backend`` tag. The link column is rendered only
when some rule actually carries a run link.

HTML is opt-in — the pipeline writes report.json; render it on demand:

    autoprove-report-render certora/ap_report/report.json [--out report.html]
"""
import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import TypedDict

from composer.spec.gen_types import TypedTemplate
from composer.templates.loader import load_jinja_template
from composer.spec.source.report.schema import (
    AutoProverReport, CoverageReport, FormalizedProperty, GaveUpComponent, GroupStatus, Outcome,
    PropertyGroup, PropertyKey, ReportBackend, RuleRef, RuleVerdict, SkippedClaim,
)


# Outcome / GroupStatus -> CSS kind. Backend-independent: only the label varies by backend.
_OUTCOME_KIND: dict[Outcome, str] = {
    Outcome.GOOD: "ok",
    Outcome.BAD: "bad",
    Outcome.ERROR: "bad",
    Outcome.TIMEOUT: "warn",
    Outcome.UNKNOWN: "muted",
}
_GROUP_KIND: dict[GroupStatus, str] = {
    GroupStatus.GOOD: "ok",
    GroupStatus.BAD: "bad",
    GroupStatus.PARTIAL: "warn",
    GroupStatus.UNKNOWN: "muted",
}

# Per-backend human labels: the data carries the neutral `Outcome`; these turn it into the words an
# auditor reads ("Verified" for a proof, "Successful test" for a forge run).
_OUTCOME_LABELS: dict[ReportBackend, dict[Outcome, str]] = {
    "prover": {
        Outcome.GOOD: "Verified", Outcome.BAD: "Violated", Outcome.ERROR: "Error",
        Outcome.TIMEOUT: "Timeout", Outcome.UNKNOWN: "Unknown",
    },
    "foundry": {
        Outcome.GOOD: "Successful test", Outcome.BAD: "Failing test", Outcome.ERROR: "Error",
        Outcome.TIMEOUT: "Timeout", Outcome.UNKNOWN: "Unknown",
    },
}
_GROUP_LABELS: dict[ReportBackend, dict[GroupStatus, str]] = {
    "prover": {
        GroupStatus.GOOD: "Verified", GroupStatus.BAD: "Violated",
        GroupStatus.PARTIAL: "Partial", GroupStatus.UNKNOWN: "No results",
    },
    "foundry": {
        GroupStatus.GOOD: "All tests passing", GroupStatus.BAD: "Has failing test",
        GroupStatus.PARTIAL: "Partial", GroupStatus.UNKNOWN: "No results",
    },
}


class ReportTerms(TypedDict):
    """Backend-specific prose nouns for the report chrome (title, the word for a verification unit,
    etc.). Keeps the data model neutral while the rendered page reads correctly for each backend."""
    title: str           # page <title> / <h1>
    unit_singular: str   # "rule" / "test" — footer counts
    unit_plural: str     # "CVL rules" / "tests" — subtitle + footer
    unit_cap: str        # "Rule" / "Test" — the verdict-table column header
    outcomes_label: str  # "Rule outcomes" / "Test outcomes" — the header chip label


_TERMS: dict[ReportBackend, ReportTerms] = {
    "prover": ReportTerms(
        title="Formal verification report", unit_singular="rule", unit_plural="CVL rules",
        unit_cap="Rule", outcomes_label="Rule outcomes",
    ),
    "foundry": ReportTerms(
        title="Foundry test report", unit_singular="test", unit_plural="tests",
        unit_cap="Test", outcomes_label="Test outcomes",
    ),
}

# Chip display order for the header outcome counts.
_OUTCOME_ORDER = [Outcome.GOOD, Outcome.BAD, Outcome.TIMEOUT, Outcome.ERROR, Outcome.UNKNOWN]
_GROUP_ORDER = [GroupStatus.GOOD, GroupStatus.BAD, GroupStatus.PARTIAL, GroupStatus.UNKNOWN]


# ---------------------------------------------------------------------------
# Typed view-models — the exact shapes the template consumes.
# ---------------------------------------------------------------------------

class LinkView(TypedDict):
    href: str | None
    label: str


class RunView(TypedDict):
    slug: str
    href: str | None


class ChipView(TypedDict):
    label: str
    kind: str
    n: int


class RowView(TypedDict):
    name: str
    label: str
    kind: str
    line: int | None
    link: LinkView
    descriptions: list[str]


class GroupView(TypedDict):
    slug: str
    title: str
    description: str
    label: str
    kind: str
    rows: list[RowView]


class ReportTemplateParams(TypedDict):
    """The full, typed context of ``autoprove_report.html.j2``."""
    contract_name: str
    run_timestamp_utc: str | None
    coverage: CoverageReport
    terms: ReportTerms
    has_links: bool
    prover_runs: list[RunView]
    rule_counts: list[ChipView]
    group_counts: list[ChipView]
    groups: list[GroupView]
    skipped: list[SkippedClaim]
    gave_up: list[GaveUpComponent]


_REPORT_TEMPLATE = TypedTemplate[ReportTemplateParams]("autoprove_report.html.j2")


def _is_url(link: str) -> bool:
    return link.startswith("http://") or link.startswith("https://")


def _link_view(link: str | None) -> LinkView:
    """How a run link renders: a clickable URL, a plain 'local run' label, or an em-dash."""
    if link and _is_url(link):
        return {"href": link, "label": "prover run"}
    if link:
        return {"href": None, "label": "local run"}
    return {"href": None, "label": "—"}


def _outcome_counts(outcomes: list[Outcome], labels: dict[Outcome, str]) -> list[ChipView]:
    """Per-outcome chip data, in display order, omitting outcomes with no occurrences."""
    c = Counter(outcomes)
    return [
        {"label": labels[o], "kind": _OUTCOME_KIND[o], "n": c[o]}
        for o in _OUTCOME_ORDER if c.get(o)
    ]


def _group_counts(statuses: list[GroupStatus], labels: dict[GroupStatus, str]) -> list[ChipView]:
    """Per-group-status chip data, in display order, omitting statuses with no occurrences."""
    c = Counter(statuses)
    return [
        {"label": labels[s], "kind": _GROUP_KIND[s], "n": c[s]}
        for s in _GROUP_ORDER if c.get(s)
    ]


def _group_view(
    group: PropertyGroup,
    props_by_key: dict[PropertyKey, FormalizedProperty],
    rules_by_ref: dict[RuleRef, RuleVerdict],
    unit_labels: dict[Outcome, str],
    group_labels: dict[GroupStatus, str],
) -> GroupView:
    """Invert the group's members into rule rows: each rule the group's properties formalize, labelled
    with the descriptions of the in-group properties that pull it in (the edge labels). The same rule
    can label differently under another group, which is why this is computed per group, not stored."""
    descriptions: dict[RuleRef, list[str]] = {}
    order: list[RuleRef] = []
    for k in group.members:
        p = props_by_key.get(k)
        if p is None:
            continue
        for ref in p.rule_refs:
            if ref not in descriptions:
                descriptions[ref] = []
                order.append(ref)
            if p.description not in descriptions[ref]:
                descriptions[ref].append(p.description)

    rows: list[RowView] = []
    for ref in order:
        rule = rules_by_ref.get(ref)
        outcome = rule.outcome if rule else Outcome.UNKNOWN
        rows.append({
            "name": ref[1],
            "label": unit_labels[outcome],
            "kind": _OUTCOME_KIND[outcome],
            "line": rule.line if rule else None,
            "link": _link_view(rule.prover_link if rule else None),
            "descriptions": descriptions[ref],
        })
    return {
        "slug": group.slug,
        "title": group.title,
        "description": group.description,
        "label": group_labels[group.status],
        "kind": _GROUP_KIND[group.status],
        "rows": rows,
    }


def _build_context(report: AutoProverReport) -> ReportTemplateParams:
    props_by_key = {p.key: p for p in report.properties}
    rules_by_ref = {r.ref: r for r in report.rules}
    unit_labels = _OUTCOME_LABELS[report.backend]
    group_labels = _GROUP_LABELS[report.backend]
    return {
        "contract_name": report.contract_name,
        "run_timestamp_utc": report.run_timestamp_utc,
        "coverage": report.coverage,
        "terms": _TERMS[report.backend],
        # The link column / runs header only make sense when a backend actually produced run links.
        "has_links": any(r.prover_link for r in report.rules),
        "prover_runs": [
            {"slug": slug, "href": link if _is_url(link) else None}
            for slug, link in sorted(report.prover_links.items())
        ],
        "rule_counts": _outcome_counts([r.outcome for r in report.rules], unit_labels),
        "group_counts": _group_counts([g.status for g in report.groups], group_labels),
        "groups": [
            _group_view(g, props_by_key, rules_by_ref, unit_labels, group_labels)
            for g in report.groups
        ],
        "skipped": report.skipped,
        "gave_up": report.gave_up_components,
    }


def render_html(report: AutoProverReport) -> str:
    return _REPORT_TEMPLATE.bind(_build_context(report)).render_to(load_jinja_template)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="autoprove-report-render",
        description="Render a report.json as a standalone HTML report.",
    )
    p.add_argument("input", type=Path, help="Path to a report.json produced by the report phase.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output HTML path (default: alongside the input as .html).")
    args = p.parse_args(argv)

    if not args.input.is_file():
        print(f"[autoprove-report-render] no such file: {args.input}", file=sys.stderr)
        return 1

    report = AutoProverReport.model_validate_json(args.input.read_text())
    out_path = args.out or args.input.with_suffix(".html")
    out_path.write_text(render_html(report))
    print(f"[autoprove-report-render] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
