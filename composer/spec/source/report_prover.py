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


def _fetch_covering(
    api: ProverOutputAPI, newest_first: list[str]
) -> dict[RuleName, Verdict]:
    """rule_name -> `Verdict` across the runs that account for one spec.

    ``newest_first`` must be ordered newest run first, which is the order
    `composer.spec.source.prover.covering_run_links` returns: it walks the author's history
    backwards. The whole result rests on that order, because the first verdict found for a rule
    is the one kept.

    Newest run wins per rule, not the most terminal outcome: a rule that timed out in one run
    and verified in a later scoped re-run is verified. Within a single run the rollup stays
    `Verdict.merge`'s, which is where "most terminal wins" is the right answer.
    """
    verdicts: dict[RuleName, Verdict] = {}
    for link in newest_first:
        for name, v in _fetch(api, link).items():
            # First writer wins, and the newest run is read first, so a later run's verdict
            # never overwrites it.
            verdicts.setdefault(name, v)
    return verdicts


def make_prover_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[GeneratedCVL]:
    """A `VerdictFetcher` that pulls per-rule verdicts from ProverOutputUtility, reading every
    run that accounts for the component's published spec. POU calls run off the event loop (one
    blocking call per run). Only ever invoked for delivered results (collect skips gave-up /
    curtailed inputs)."""
    api = api or ProverOutputAPI()

    async def fetch(formalized: Formalized[GeneratedCVL]) -> dict[RuleName, Verdict]:
        newest_first = formalized.result.covering_output_links
        if not newest_first:
            return {}
        return await asyncio.to_thread(_fetch_covering, api, newest_first)

    return fetch
