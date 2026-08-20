"""Findings synthesis for a Rust wheel's results: where its evidence comes from, and how to ask for
a write-up of it.

The shared loop is `composer.spec.source.report.findings`, and `RuleEvidence` is its shape. What is
here is the part that reads a *wheel's* results into it — every field it works from is wire
(`Verdict.detail`, `Verdict.accounting`, `Verdict.finding`, the result's declared failures), so
nothing in this module knows what any particular backend checks or what its evidence means.

Those are the wheel's to say, and it says them in `FindingsDeclaration`: the domain half of the
system prompt (what its evidence *is*), and how severity is reached. A wheel that declares nothing
produces no findings — see `AppDescriptor.findings`.
"""
from typing import TypedDict

from composer.pipeline.ptypes import ComponentOutcome, Delivered
from composer.rustapp.descriptor import FindingsDeclaration
from composer.rustapp.result import RustFormalResult
from composer.spec.gen_types import TypedTemplate
from composer.spec.source.report.collect import RuleEvidence
from composer.spec.source.report.findings import FindingsPolicy, FindingsPromptParams
from composer.spec.source.report.schema import RuleRef
from composer.spec.system_model import FeatureUnit
from composer.templates.loader import load_jinja_template

type RustOutcomes = list[ComponentOutcome[RustFormalResult, FeatureUnit]]


_WRITE_UP_PROMPT = TypedTemplate[FindingsPromptParams]("autoprove_report_findings_rust_prompt.j2")


def observations(outcomes: RustOutcomes) -> dict[RuleRef, RuleEvidence]:
    """Every delivered check as evidence, keyed as the report keys its rows: ``(file, name)``.

    Rebuilt rather than looked up: ``collect`` uses the verdict's file, falling back to the
    component artifact, and a callout-mode wheel gives every component that same fallback — so the
    check name alone does not identify a row.

    First component naming a key wins, as in ``collect``: where two of them do collapse onto one
    row, the evidence here has to be the same run's as the message there.

    ``analysis`` stays unset — a wheel reports what its run found, not a reading of why the check
    broke, and a reader must be able to tell the two apart."""
    observed: dict[RuleRef, RuleEvidence] = {}
    for o in outcomes:
        if not isinstance(o.result, Delivered):
            continue
        res = o.result.result
        for check, verdict in res.verdicts.items():
            ref = (verdict.unit_file or o.result.unit_file, check)
            observed.setdefault(ref, RuleEvidence(
                label=o.feat.display_name,
                counterexample=verdict.prompt_detail(),
                ran=verdict.outcome,
                accounting=verdict.accounting,
                declared=res.expected_failures.get(check),
                finding=verdict.finding,
            ))
    return observed


def rust_findings(
    outcomes: RustOutcomes, declared: FindingsDeclaration | None,
) -> FindingsPolicy | None:
    """This run's findings policy, around the observations its own results carry — or ``None`` for a
    wheel that declared none, which produces no findings at all."""
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
