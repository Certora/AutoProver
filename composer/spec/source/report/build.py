"""Build the property-keyed report in memory: collect -> group -> validate.

`build_report` is the entry point a pipeline's final phase calls. It builds and *returns* the
`AutoProverReport`; persisting it is the caller's job (via the pipeline's `ArtifactStore`). It is
backend-agnostic: the caller supplies a `VerdictFetcher` (how to get per-unit `Outcome`s for this
backend) and a `backend` tag (used only to pick render labels). It is structured so that a grouping
failure (LLM, validation, an empty grouping) is first retried on a second model when the caller
supplies one, and otherwise degrades to a single ``general`` bucket rather than producing no
high-level section; the caller additionally treats the whole phase as best-effort.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from composer.spec.context import SourceCode
from composer.spec.types import Curtailed
from composer.spec.source.report.collect import (
    EvidenceFetcher, ReportableResult, ReportComponentInput, VerdictFetcher, collect,
)
from composer.spec.source.report.coverage import ValidationError, validate
from composer.spec.source.report.findings import build_findings
from composer.spec.source.report.grouping import (
    build_fallback_grouping, build_groups, call_grouping_llm, PropertyGroup
)
from composer.spec.source.report.schema import (
    AutoProverReport, CoverageReport, DeprioritizedProperty, DesignDocRecord, Finding, Outcome, PropertyKey, ReportBackend,
    RuleRef, SourceEditRecord,
    VerificationArtifactRecord,
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


def design_doc_record(source: SourceCode) -> DesignDocRecord | None:
    """The report's record of the design document ``source`` was analysed against, or None for a
    source-only run. The path is made project-relative when the document lies inside the project,
    so the record does not depend on where the project was checked out."""
    doc = source.design_doc
    if doc is None:
        return None
    root = Path(source.project_root).resolve()
    resolved = doc.path.resolve()
    path = str(resolved.relative_to(root)) if resolved.is_relative_to(root) else str(doc.path)
    return DesignDocRecord(path=path, origin=doc.origin, reason=doc.reason)


async def build_report[R: ReportableResult](
    *,
    contract_name: str,
    backend: ReportBackend,
    components: list[ReportComponentInput[R]],
    llm: BaseChatModel,
    fetch_verdicts: VerdictFetcher[R],
    grouping_retry_llm: BaseChatModel | None = None,
    source_edits: list[SourceEditRecord] | None = None,
    verification_artifacts: list[VerificationArtifactRecord] | None = None,
    findings_llm: BaseChatModel | None = None,
    fetch_evidence: EvidenceFetcher | None = None,
    run_mode: str | None = None,
    deprioritized: list[DeprioritizedProperty] | None = None,
    active_plugins: list[str] | None = None,
    design_doc: DesignDocRecord | None = None,
) -> AutoProverReport:
    """Build and return the in-memory `AutoProverReport`. Persistence is the caller's job.

    The grouping is asked of ``llm`` first. When that fails, ``grouping_retry_llm`` (when given, a
    different model) is asked the same question before the single-bucket fallback applies.

    When ``findings_llm`` is supplied, violated rules are additionally synthesized into
    audit-issue `Finding`s (best-effort; a synthesis failure yields no findings
    rather than failing the report). ``fetch_evidence`` supplies each violation's captured
    counterexample analysis; it is optional."""
    properties, rules, skipped, gave_up, curtailed, dropped = await collect(
        components, fetch_verdicts=fetch_verdicts
    )
    rule_outcomes: dict[RuleRef, Outcome] = {r.ref: r.outcome for r in rules}
    props_by_key = {p.key: p for p in properties}

    def _validate(groups: list[PropertyGroup]) -> CoverageReport:
        return validate(
            properties=properties, rules=rules, groups=groups, skipped=skipped,
            gave_up=gave_up, curtailed=curtailed, dropped_orphan_rules=dropped,
            deprioritized_count=len(deprioritized or []),
        )

    async def _group_with(grouping_llm: BaseChatModel) -> tuple[list[PropertyGroup], CoverageReport]:
        grouping = await call_grouping_llm(
            llm=grouping_llm, contract_name=contract_name, properties=properties,
        )
        groups = build_groups(grouping.groups, props_by_key, rule_outcomes)
        coverage = _validate(groups)
        if not any(g.members for g in groups):
            raise ValidationError("grouping produced no high-level properties")
        return groups, coverage

    # The grouping may fail three ways: (a) the LLM call raises, (b) validation rejects a
    # structurally-invalid grouping, (c) the grouping is valid but covers no properties. Each
    # failure passes the question to the next model in ``grouping_llms``; when every model has
    # failed, the single 'general' bucket applies, so the report always has a high-level section.
    # The fallback bucket holds every property exactly once, so its validate cannot raise.
    # With no formalized properties at all (everything gave up or was budget-curtailed), there is
    # nothing to group and no reason to spend the LLM call.
    groups: list[PropertyGroup] = []
    if not properties:
        coverage = _validate(groups)
    else:
        grouping_llms = [llm] if grouping_retry_llm is None else [llm, grouping_retry_llm]
        grouped: tuple[list[PropertyGroup], CoverageReport] | None = None
        for i, grouping_llm in enumerate(grouping_llms):
            try:
                grouped = await _group_with(grouping_llm)
                break
            except Exception as e:  # noqa: BLE001 — any LLM/transport/validation error degrades
                if RERAISE_REPORT_FAILURES:
                    raise
                reason = (
                    f"validation rejected the grouping: {e}" if isinstance(e, ValidationError)
                    else f"grouping failed: {e}"
                )
                next_step = (
                    "retrying with another model" if i + 1 < len(grouping_llms)
                    else "applying fallback grouping"
                )
                _log.warning("report: %s; %s", reason, next_step)
        if grouped is not None:
            groups, coverage = grouped
        else:
            groups = build_groups(
                build_fallback_grouping(properties).groups, props_by_key, rule_outcomes
            )
            coverage = _validate(groups)
            coverage.warnings = ["FALLBACK GROUPING APPLIED"] + coverage.warnings

    # Curtailed results are deliberately absent: a run link on a partial encoding proves nothing
    # about it, so it lives only in the component's appendix record.
    prover_links = {
        c.name: c.formalized.run_link
        for c in components
        if c.formalized is not None and not isinstance(c.formalized, Curtailed)
        and c.formalized.run_link
    }
    # Violated rules -> findings. Its own guard: findings synthesis must never fail the report
    # (the whole phase is also best-effort in the caller, but this keeps a working report even when
    # only findings break).
    findings: list[Finding] = []
    if findings_llm is not None:
        try:
            findings = await build_findings(
                contract_name=contract_name, rules=rules, properties=properties, groups=groups,
                fetch_evidence=fetch_evidence, llm=findings_llm,
            )
        except Exception as e:  # noqa: BLE001
            if RERAISE_REPORT_FAILURES:
                raise
            _log.warning("report: findings synthesis failed (%s); continuing without findings", e)

    report = AutoProverReport(
        backend=backend,
        run_mode=run_mode,
        deprioritized=deprioritized or [],
        active_plugins=active_plugins or [],
        contract_name=contract_name,
        design_doc=design_doc,
        run_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        prover_links=prover_links,
        properties=properties,
        rules=rules,
        groups=groups,
        skipped=skipped,
        gave_up_components=gave_up,
        curtailed_components=curtailed,
        source_edits=source_edits or [],
        verification_artifacts=verification_artifacts or [],
        coverage=coverage,
        findings=findings,
    )
    return report
