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
from composer.spec.source.report.collect import Formalized, Verdict, VerdictFetcher
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
            link=link,
        )
        name = RuleName(c.rule_name)
        verdicts[name] = cand.merge(verdicts.get(name))
    return verdicts


def _fetch_covering(api: ProverOutputAPI, links: list[str]) -> dict[RuleName, Verdict]:
    """rule_name -> `Verdict` across the runs that account for one spec, ``links`` newest first.

    Newest run wins per rule, rather than the most terminal outcome: a rule that timed out in
    one run and verified in a later scoped re-run is verified. Within a single run the rollup
    stays `Verdict.merge`'s — that is where "most terminal wins" is the right answer.
    """
    verdicts: dict[RuleName, Verdict] = {}
    for link in links:
        for name, v in _fetch(api, link).items():
            verdicts.setdefault(name, v)
    return verdicts


def make_prover_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[GeneratedCVL]:
    """A `VerdictFetcher` that pulls per-rule verdicts from ProverOutputUtility, reading every
    run that accounts for the component's published spec. POU calls run off the event loop (one
    blocking call per run). Only ever invoked for delivered results (collect skips gave-up /
    curtailed inputs)."""
    api = api or ProverOutputAPI()

    async def fetch(formalized: Formalized[GeneratedCVL]) -> dict[RuleName, Verdict]:
        links = formalized.result.covering_output_links
        if not links:
            return {}
        return await asyncio.to_thread(_fetch_covering, api, links)

    return fetch
