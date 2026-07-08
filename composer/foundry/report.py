"""Foundry-backend adapter for the property-keyed report.

Maps a `GeneratedFoundryTest`'s forge ground truth into the report's backend-agnostic
`Verdict`/`Outcome` vocabulary: a test that ran is GOOD, unless the author declared it an expected
failure (a known-vuln demonstration that forge confirms fails) — which is BAD, so it stands out for
a human to read. There is no external run service; the verdicts come straight off the result object.
"""
import logging

from langchain_core.language_models.chat_models import BaseChatModel

from composer.foundry.author import GeneratedFoundryTest
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import AutoProverReport, Outcome, RuleName

_log = logging.getLogger(__name__)


async def _foundry_verdicts(
    inp: ReportComponentInput[GeneratedFoundryTest],
) -> dict[RuleName, Verdict]:
    """Per-test verdicts from forge ground truth: a ran test is GOOD unless the author marked it an
    expected failure (BAD). No external service — read straight off the result."""
    fm = inp.formalized
    if fm is None:
        return {}
    res = fm.result
    return {
        name: Verdict(
            outcome=Outcome.BAD if res.expected_failures.get(name) else Outcome.GOOD,
            unit_file=fm.unit_file,
        )
        for name in res.ran_tests
    }


async def run_foundry_report(
    *,
    contract_name: str,
    components: list[ReportComponentInput[GeneratedFoundryTest]],
    llm: BaseChatModel,
) -> AutoProverReport:
    """Build the property-keyed report for a foundry run (backend-tagged ``foundry`` so the renderer
    picks test-flavoured labels)."""
    return await build_report(
        contract_name=contract_name,
        backend="foundry",
        components=components,
        llm=llm,
        fetch_verdicts=_foundry_verdicts,
    )
