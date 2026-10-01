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

from composer.spec.cvl_generation import GeneratedCVL, _output_link
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
#: and ``/job/<job>`` and has no ``jobStatus`` branch, so it raises rather than returning an id.
#: ``_fetch`` sidesteps that by rewriting the link to its ``/output/`` view; a call that takes the
#: link as given goes through :func:`job_input`. See ``docs/upstream-defects.md`` P5.
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


def _fetch(api: ProverOutputAPI, link: str, spec_file: str | None) -> dict[RuleName, Verdict]:
    """rule_name -> rolled-up `Verdict` for one prover run, each stamped with ``spec_file`` (the spec that
    run verified — constant for the run), else with the file each check's source location names.
    Best-effort: any POU failure -> {}.

    A run link is a raw ``/jobStatus/`` job URL; POU (and the report's own links) want the ``/output/``
    view, so normalize before the call and stamp the normalized link onto the verdict.
    """
    link = _output_link(link) or link
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
            spec_file or (Path(loc.file).name if (loc and loc.file) else None),
            link=link,
        )
        name = RuleName(c.rule_name)
        verdicts[name] = cand.merge(verdicts.get(name))
    return verdicts


def _fetch_covering(
    api: ProverOutputAPI, run_link_specs: list[tuple[str, str]]
) -> dict[RuleName, Verdict]:
    """rule_name -> `Verdict` across the runs that compose the component's buffers.

    ``run_link_specs`` is (link, the spec that run verified) newest run first — the order
    `composer.spec.source.prover.completing_run_specs` produces, walking each buffer's history backwards.
    The whole result rests on that order, because the first verdict found for a rule is the one kept.

    Newest run wins per rule, not the most terminal outcome: a rule that timed out in one run
    and verified in a later scoped re-run is verified. Within a single run the rollup stays
    `Verdict.merge`'s, which is where "most terminal wins" is the right answer.
    """
    verdicts: dict[RuleName, Verdict] = {}
    for link, spec_file in run_link_specs:
        for name, v in _fetch(api, link, spec_file).items():
            # First writer wins, and the newest run is read first, so a later run's verdict
            # never overwrites it.
            verdicts.setdefault(name, v)
    return verdicts


def fetch_unsat_cores(api: ProverOutputAPI, link: str, rule: str) -> tuple[str, ...]:
    """The text of every unsat core the run at ``link`` wrote for ``rule``'s checks.

    Only a run with ``coverage_info`` on writes any. The job's ``unsat_core_map.json`` keys a core by
    check — the rule itself, or a sub-check such as ``<rule>-Assertions`` — so a rule's cores are
    those under its own name or a name it prefixes with ``-``, which no rule name can contain.
    """
    job = job_input(link)
    names = sorted(
        {
            name
            for check, files in api.unsat_core_map(job).items()
            if check == rule or check.startswith(f"{rule}-")
            for name in files
        }
    )
    return tuple(api.fetch_output_file(job, name) for name in names)


def make_prover_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[GeneratedCVL]:
    """A `VerdictFetcher` that pulls per-rule verdicts from ProverOutputUtility, reading every
    prover run that composes the component's buffers (``GeneratedCVL.run_link_specs``). With buffers +
    rule-striping one component's rules are run across several jobs, so a fetch keyed on a single link
    would report every rule whose verdict came from another run as UNKNOWN; ``_fetch_covering`` unions
    them newest-run-first, so a rule re-proved in a later scoped run wins over its earlier verdict. Each
    rule is stamped with the spec its run verified, taken from ``run_link_specs``. POU calls run off the
    event loop (one blocking call per run). Only ever invoked for delivered results (collect skips
    gave-up / curtailed inputs)."""
    api = api or ProverOutputAPI()

    async def fetch(formalized: Formalized[GeneratedCVL]) -> dict[RuleName, Verdict]:
        run_link_specs = formalized.result.run_link_specs
        if not run_link_specs:
            return {}
        return await asyncio.to_thread(_fetch_covering, api, run_link_specs)

    return fetch


def make_run_link_fetcher(api: ProverOutputAPI | None = None) -> VerdictFetcher[ReportableResult]:
    """A `VerdictFetcher` for a backend whose component is verified by one prover run: it reads
    ``Formalized.run_link`` and nothing of the result, so it serves every such backend. Each verdict
    names the file its check's source location does. POU calls run off the event loop."""
    api = api or ProverOutputAPI()

    async def fetch(formalized: Formalized[ReportableResult]) -> dict[RuleName, Verdict]:
        if formalized.run_link is None:
            return {}
        return await asyncio.to_thread(_fetch, api, formalized.run_link, None)

    return fetch
