"""Map a Rust wheel's results onto the shared findings loop.

The loop and `RuleEvidence` live in `composer.spec.source.report.findings`. This module only
reads wire fields (`Verdict.detail`, `accounting`, `finding_key`, declared failures) into that
shape. What the evidence means is `FindingsDeclaration.domain`; severity is the host's.
No declaration, no findings — see `AppDescriptor.findings`.
"""
from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.descriptor import FindingsDeclaration
from composer.rustapp.result import RustFormalResult
from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.collect import RuleEvidence
from composer.spec.source.report.findings import FindingsPolicy, FindingsPromptParams
from composer.spec.source.report.schema import RuleRef
from composer.spec.system_model import FeatureUnit

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]


_WRITE_UP_PROMPT = TypedTemplate[FindingsPromptParams]("autoprove_report_findings_rust_prompt.j2")


def observations(outcomes: RustOutcomes) -> dict[RuleRef, RuleEvidence]:
    """Every delivered check as evidence, keyed as the report keys its rows: ``(file, name)``.

    Rebuilt to match ``collect``: the key uses the verdict's file, then the component artifact,
    and the first component that names a key wins. A check name alone is not enough — one
    deliverable can hold several components' checks.

    ``analysis`` is left unset. The wheel reports what the run found, not why the check broke.
    """
    observed: dict[RuleRef, RuleEvidence] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed.setdefault(ref, RuleEvidence(
                label=o.feat.display_name,
                counterexample=verdict.detail,
                ran=verdict.outcome,
                accounting=verdict.accounting,
                expected_failure_reason=res.expected_failures.get(check),
                finding_key=verdict.finding_key,
            ))
    return observed


def rust_findings(
    outcomes: RustOutcomes, declared: FindingsDeclaration | None,
) -> FindingsPolicy | None:
    """This run's findings policy from the outcomes, or ``None`` if the wheel declared none."""
    if declared is None:
        return None
    observed = observations(outcomes)

    async def fetch(ref: RuleRef) -> list[RuleEvidence]:
        found = observed.get(ref)
        return [found] if found is not None else []

    return FindingsPolicy(
        fetch_evidence=fetch,
        domain=declared.domain,
        prompt=_WRITE_UP_PROMPT,
    )
