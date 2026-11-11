"""The report a run leaves when it ends without writing one of its own.

A run that fails before its report phase — refused at preflight, crashed in analysis, stopped from
outside — would otherwise deliver no ``report.json`` at all, and a consumer that reads only the
report cannot tell such a run from one that is still going. :func:`reporting_run_failure` wraps a
run and, when it ends on an exception without a report, writes one carrying only
:attr:`~composer.spec.source.report.schema.AutoProverReport.run_failure`.
"""
import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from composer.pipeline.refusal import RunRefusal
from composer.spec.artifacts import REPORT_FILE, write_report_file
from composer.spec.source.report.schema import (
    AutoProverReport, CoverageReport, InternalError, Interrupted, Refused, ReportBackend,
    RunFailure,
)

_log = logging.getLogger(__name__)


def run_failure(exc: BaseException) -> RunFailure:
    """How ``exc``, having ended a run, is reported. Within an exception group, a refusal wins over
    an interruption, and an interruption over anything else: a refusal is the one cause the reader
    can act on, and is often what cancelled the group's other tasks."""
    if isinstance(exc, BaseExceptionGroup):
        refusals, rest = exc.split(RunRefusal)
        if refusals is not None:
            return run_failure(_first_leaf(refusals))
        if rest is not None and rest.subgroup((KeyboardInterrupt, asyncio.CancelledError)) is not None:
            return Interrupted()
        return InternalError()
    match exc:
        case RunRefusal():
            return Refused(message=str(exc))
        case KeyboardInterrupt() | asyncio.CancelledError():
            return Interrupted()
        case _:
            return InternalError()


def _first_leaf(group: BaseExceptionGroup) -> BaseException:
    first = group.exceptions[0]
    return _first_leaf(first) if isinstance(first, BaseExceptionGroup) else first


def failure_report(
    *, contract_name: str, backend: ReportBackend, run_mode: str, failure: RunFailure
) -> AutoProverReport:
    return AutoProverReport(
        backend=backend,
        run_mode=run_mode,
        contract_name=contract_name,
        run_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        properties=[],
        rules=[],
        groups=[],
        coverage=CoverageReport(
            total_properties=0,
            total_rules=0,
            total_groups=0,
            properties_per_group_min=0,
            properties_per_group_max=0,
            property_coverage_complete=True,
        ),
        run_failure=failure,
    )


@contextmanager
def reporting_run_failure(
    report_dir: Path, *, contract_name: str, backend: ReportBackend, run_mode: str
) -> Iterator[None]:
    """Run the body, and if it raises before a report is in ``report_dir``, write a
    :func:`failure_report` there. The exception propagates either way.

    A report already in ``report_dir`` when the body starts is removed first: it is an earlier
    run's, and left in place it would both stand in for this run's outcome and suppress the failure
    report. One the body wrote is kept even if the body then fails, since it is the run's own
    account and a teardown fault does not make it wrong.

    Neither ``GeneratorExit`` nor ``SystemExit`` is a failure of the run: the first is how an
    abandoned generator is closed, the second a deliberate exit that has already said why.
    """
    report = report_dir / REPORT_FILE
    report.unlink(missing_ok=True)
    try:
        yield
    except (GeneratorExit, SystemExit):
        raise
    except BaseException as exc:
        if not report.exists():
            try:
                write_report_file(report_dir, failure_report(
                    contract_name=contract_name, backend=backend, run_mode=run_mode,
                    failure=run_failure(exc),
                ))
                _log.info("run failed: wrote %s", report)
            except Exception:
                _log.exception("failed to write the failure report")
        raise
