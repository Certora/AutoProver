"""The Solana Prover's warnings about functions it treated as external.

An external call writes nothing but its return value, so these are the places the model can be
unsound without any verdict saying so. The SBF inliner raises one such alert per rule it
translates, in the job's alert report. The text is the Prover's own and is passed on whole.
"""

from dataclasses import dataclass

from composer.prover.results import AlertReport

_EXTERNALS_ALERT = "The following functions are neither inlined nor summarized"


@dataclass(frozen=True)
class ExternalWarnings:
    """Distinct external-function warnings, sorted. Empty when the run raised none."""

    messages: tuple[str, ...] = ()


def external_warnings(alerts: AlertReport) -> ExternalWarnings:
    return ExternalWarnings(
        tuple(sorted({m for m in alerts.messages if _EXTERNALS_ALERT in m}))
    )
