"""Prover-backend adapter for the property-keyed report.

Translates ProverOutputUtility's per-rule `CheckResult`s into the report's backend-agnostic
`Verdict`/`Outcome` vocabulary. This is the only place the report stack touches
`prover_output_utility` — the core report package is backend-neutral.
"""
import asyncio
import logging
from pathlib import Path

from prover_output_utility import ProverOutputAPI
from prover_output_utility.models import CheckResult, NodeStatus

from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.source.report.collect import ReportComponentInput, Verdict, VerdictFetcher
from composer.spec.source.report.schema import Outcome, RuleName

_log = logging.getLogger(__name__)

# RUNNING / PENDING never belong in a finalized report -> fold into UNKNOWN.
_NODE_TO_OUTCOME: dict[NodeStatus, Outcome] = {
    NodeStatus.VERIFIED: Outcome.GOOD,
    NodeStatus.VIOLATED: Outcome.BAD,
    NodeStatus.ERROR: Outcome.ERROR,
    NodeStatus.TIMEOUT: Outcome.TIMEOUT,
    NodeStatus.UNKNOWN: Outcome.UNKNOWN,
    NodeStatus.RUNNING: Outcome.UNKNOWN,
    NodeStatus.PENDING: Outcome.UNKNOWN,
}


def _fetch(api: ProverOutputAPI, link: str) -> dict[RuleName, Verdict]:
    """rule_name -> rolled-up `Verdict` for one prover run. Best-effort: any POU failure -> {}."""
    try:
        checks: list[CheckResult] = api.get_all_checks(link)
    except Exception:
        _log.warning("report: POU get_all_checks failed for %s", link, exc_info=True)
        return {}
    verdicts: dict[RuleName, Verdict] = {}
    for c in checks:
        loc = c.source_location
        cand = Verdict(
            _NODE_TO_OUTCOME.get(c.status, Outcome.UNKNOWN),
            loc.line if loc else None,
            c.duration or None,
            Path(loc.file).name if (loc and loc.file) else None,
        )
        verdicts[c.rule_name] = cand.merge(verdicts.get(c.rule_name))
    return verdicts


def make_prover_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[GeneratedCVL]:
    """A `VerdictFetcher` that pulls per-rule verdicts from ProverOutputUtility, keyed by each
    component's run link. POU calls run off the event loop (one blocking call per run)."""
    api = api or ProverOutputAPI()

    async def fetch(inp: ReportComponentInput[GeneratedCVL]) -> dict[RuleName, Verdict]:
        if inp.formalized is None or inp.formalized.run_link is None:
            return {}
        return await asyncio.to_thread(_fetch, api, inp.formalized.run_link)

    return fetch
