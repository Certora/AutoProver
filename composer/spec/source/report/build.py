"""Build the property-keyed report in memory: collect -> group -> validate.

`build_report` is the entry point a pipeline's final phase calls. It builds and *returns* the
`AutoProverReport`; persisting it is the caller's job (via the pipeline's `ArtifactStore`). It is
backend-agnostic: the caller supplies a `VerdictFetcher` (how to get per-unit `Outcome`s for this
backend) and a `backend` tag (used only to pick render labels). It is structured so that any single
failure (LLM, validation, an empty grouping) degrades to a single ``general`` bucket rather than
producing no high-level section; the caller additionally treats the whole phase as best-effort.
"""
import logging
from datetime import datetime, timezone

from langchain_core.language_models.chat_models import BaseChatModel

from composer.spec.source.report.collect import (
    EvidenceFetcher, ReportableResult, ReportComponentInput, VerdictFetcher, collect,
)
from composer.spec.source.report.coverage import ValidationError, validate
from composer.spec.source.report.findings import build_findings
from composer.spec.source.report.grouping import (
    build_fallback_grouping, build_groups, call_grouping_llm,
)
from composer.spec.source.report.schema import (
    AutoProverReport, Finding, Outcome, PropertyKey, ReportBackend, RuleRef,
)

_log = logging.getLogger(__name__)

#: Test escape hatch. When True, a report-phase failure re-raises instead of being
#: absorbed (the grouping fallback here, and the best-effort guard around the whole
#: phase in common_pipeline). Production/manual-harness runs leave this False so a
#: degraded grouping never fails the run; a harness test flips it on so a broken
#: tape (missing/mis-keyed ``report`` lane) fails loudly instead of silently
#: exercising the fallback path. Read as a live module attribute — set it via the
#: module, not a by-value import.
RERAISE_REPORT_FAILURES = False


async def build_report[R: ReportableResult](
    *,
    contract_name: str,
    backend: ReportBackend,
    components: list[ReportComponentInput[R]],
    llm: BaseChatModel,
    fetch_verdicts: VerdictFetcher[R],
    findings_llm: BaseChatModel | None = None,
    fetch_evidence: EvidenceFetcher | None = None,
) -> AutoProverReport:
    """Build and return the in-memory `AutoProverReport`. Persistence is the caller's job.

    When ``findings_llm`` is supplied, violated rules are additionally synthesized into
    Sherlock-``IssueIn``-shaped `Finding`s (best-effort; a synthesis failure yields no findings
    rather than failing the report). ``fetch_evidence`` supplies each violation's captured
    counterexample analysis; it is optional."""
    properties, rules, skipped, gave_up, dropped = await collect(
        components, fetch_verdicts=fetch_verdicts
    )
    rule_outcomes: dict[RuleRef, Outcome] = {r.ref: r.outcome for r in rules}
    props_by_key = {p.key: p for p in properties}

    # The grouping may fail three ways; each degrades to the single 'general' bucket so the report
    # always has a high-level section: (a) the LLM call raises, (b) validation rejects a
    # structurally-invalid grouping, (c) the grouping is valid but covers no properties. The
    # fallback bucket holds every property exactly once, so the re-validate below cannot raise.
    fallback_reason: str | None = None
    try:
        grouping = await call_grouping_llm(
            llm=llm, contract_name=contract_name, properties=properties,
        )
        groups = build_groups(grouping.groups, props_by_key, rule_outcomes)
        coverage = validate(
            properties=properties, rules=rules, groups=groups,
            skipped=skipped, gave_up=gave_up, dropped_orphan_rules=dropped,
        )
        grouped: set[PropertyKey] = {k for g in groups for k in g.members}
        if properties and not grouped:
            raise ValidationError("grouping produced no high-level properties")
    except Exception as e:  # noqa: BLE001 — any LLM/transport/validation error degrades
        if RERAISE_REPORT_FAILURES:
            raise
        fallback_reason = (
            f"validation rejected the grouping: {e}" if isinstance(e, ValidationError)
            else f"grouping failed: {e}"
        )
        _log.warning("report: %s; applying fallback grouping", fallback_reason)
        groups = build_groups(
            build_fallback_grouping(properties).groups, props_by_key, rule_outcomes
        )
        coverage = validate(
            properties=properties, rules=rules, groups=groups,
            skipped=skipped, gave_up=gave_up, dropped_orphan_rules=dropped,
        )
        coverage.warnings = ["FALLBACK GROUPING APPLIED"] + coverage.warnings

    # Violated rules -> findings. Its own guard: findings synthesis must never fail the report
    # (the whole phase is also best-effort in the caller, but this keeps a working report even when
    # only findings break).
    findings: list[Finding] = []
    if findings_llm is not None:
        try:
            findings = await build_findings(
                contract_name=contract_name, backend=backend, rules=rules,
                properties=properties, groups=groups, fetch_evidence=fetch_evidence,
                llm=findings_llm,
            )
        except Exception as e:  # noqa: BLE001
            if RERAISE_REPORT_FAILURES:
                raise
            _log.warning("report: findings synthesis failed (%s); continuing without findings", e)

    report = AutoProverReport(
        backend=backend,
        contract_name=contract_name,
        run_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        prover_links={c.name: c.formalized.run_link for c in components
                      if c.formalized and c.formalized.run_link},
        properties=properties,
        rules=rules,
        groups=groups,
        skipped=skipped,
        gave_up_components=gave_up,
        coverage=coverage,
        findings=findings,
    )
    return report
