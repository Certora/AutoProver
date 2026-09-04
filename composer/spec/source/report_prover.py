"""Prover-backend adapter for the property-keyed report.

Translates per-rule prover results into the report's backend-agnostic
`Verdict`/`Outcome` vocabulary. This is the only place the report stack touches
`prover_output_utility` — the core report package is backend-neutral.

A run link is either a cloud job URL or a local report directory, mirroring
``run_prover``'s own cloud/local split: ProverOutputUtility speaks only the former, so
a local run is read back with the same parser the run itself used.
"""
import asyncio
import logging
from pathlib import Path

from prover_output_utility import ProverOutputAPI
from prover_output_utility.exceptions import ProverAPIError
from prover_output_utility.models import CheckResult, NodeStatus

from composer.prover.ptypes import StatusCodes
from composer.prover.results import read_and_format_run_result
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

# The local parser's vocabulary. SANITY_FAILED is a real failure of the rule's own
# sanity check; SKIPPED yielded no verdict at all.
_STATUS_TO_OUTCOME: dict[StatusCodes, Outcome] = {
    "VERIFIED": Outcome.GOOD,
    "VIOLATED": Outcome.BAD,
    "ERROR": Outcome.ERROR,
    "TIMEOUT": Outcome.TIMEOUT,
    "SANITY_FAILED": Outcome.BAD,
    "SKIPPED": Outcome.UNKNOWN,
}


def _fetch_local(path: Path) -> dict[RuleName, Verdict]:
    """rule_name -> rolled-up `Verdict` from a local run's report directory, read with
    the parser ``run_prover`` uses on the same directory. No line numbers or
    durations: those come from POU, which a local run never went through."""
    parsed = read_and_format_run_result(path)
    if isinstance(parsed, str):
        _log.warning("report: could not read local prover results at %s: %s", path, parsed)
        return {}
    verdicts: dict[RuleName, Verdict] = {}
    for result in parsed.values():
        name = RuleName(result.path.rule)
        cand = Verdict(_STATUS_TO_OUTCOME.get(result.status, Outcome.UNKNOWN))
        verdicts[name] = cand.merge(verdicts.get(name))
    return verdicts


def _fetch(api: ProverOutputAPI, link: str) -> dict[RuleName, Verdict]:
    """rule_name -> rolled-up `Verdict` for one prover run, cloud or local.

    Best-effort: a fetch that fails yields no verdicts, which the report renders as
    UNKNOWN. That silence is why the local branch matters — POU rejects a filesystem
    path outright, so before it existed a local run reported every rule inconclusive."""
    if (local := Path(link)).is_dir():
        return _fetch_local(local)

    try:
        checks: list[CheckResult] = api.get_all_checks(link)
    except ProverAPIError:
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


def make_prover_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[GeneratedCVL]:
    """A `VerdictFetcher` that pulls per-rule verdicts for each component's run link —
    from ProverOutputUtility for a cloud job, or off disk for a local one. Both are
    blocking and run off the event loop (one call per run). Only ever invoked for
    delivered results (collect skips gave-up / curtailed inputs)."""
    api = api or ProverOutputAPI()

    async def fetch(formalized: Formalized[GeneratedCVL]) -> dict[RuleName, Verdict]:
        if formalized.run_link is None:
            return {}
        return await asyncio.to_thread(_fetch, api, formalized.run_link)

    return fetch
