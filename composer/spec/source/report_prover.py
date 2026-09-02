"""Prover-backend adapter for the property-keyed report.

Translates ProverOutputUtility's per-rule `CheckResult`s into the report's backend-agnostic
`Verdict`/`Outcome` vocabulary. This is the only place the report stack touches
`prover_output_utility` — the core report package is backend-neutral.
"""
import asyncio
import logging
import re
from pathlib import Path

from prover_output_utility import ProverOutputAPI
from prover_output_utility.models import CheckResult, NodeStatus

from composer.spec.source.report.collect import (
    Formalized,
    ReportableResult,
    Verdict,
    VerdictFetcher,
)
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


#: The Solana Prover's report link. ``prover_output_utility`` recognizes ``/output/<user>/<job>``
#: and ``/job/<job>`` and has no ``jobStatus`` branch, so it raises rather than returning an id —
#: which ``_fetch`` catches, leaving every rule of every CVLR run to fall through as ``UNKNOWN``.
#: See ``docs/upstream-defects.md`` P5.
_JOB_STATUS_LINK = re.compile(r"/jobStatus/\d+/(?P<job>[0-9A-Za-z]+)")


def job_input(link: str) -> str:
    """What to hand POU for ``link``.

    POU accepts a job URL *or* a bare job id, and the id is the currency that always works: when a
    link's shape is one its URL parser does not know, handing over the id it would have extracted
    gets the same answer. Anything unrecognized here passes through untouched, so links POU already
    parses keep going through POU's own extraction rather than this regex.
    """
    found = _JOB_STATUS_LINK.search(link)
    return found.group("job") if found else link


def fetch_verdicts(api: ProverOutputAPI, link: str) -> dict[RuleName, Verdict]:
    """rule_name -> rolled-up `Verdict` for one prover run. Best-effort: any POU failure -> {}."""
    try:
        checks: list[CheckResult] = api.get_all_checks(job_input(link))
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
        name = RuleName(c.rule_name)
        verdicts[name] = cand.merge(verdicts.get(name))
    return verdicts


def make_prover_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[ReportableResult]:
    """A `VerdictFetcher` that pulls per-rule verdicts from ProverOutputUtility, keyed by each
    component's run link. POU calls run off the event loop (one blocking call per run). Only ever
    invoked for delivered results (collect skips gave-up / curtailed inputs).

    Typed at ``ReportableResult`` rather than at one backend's result: it reads nothing but
    ``run_link``, so every backend with a prover job behind it wants this same fetcher. CVLR was
    reaching past it for the inner function because the annotation named CVL.
    """
    api = api or ProverOutputAPI()

    async def fetch(formalized: Formalized[ReportableResult]) -> dict[RuleName, Verdict]:
        if formalized.run_link is None:
            return {}
        return await asyncio.to_thread(fetch_verdicts, api, formalized.run_link)

    return fetch
