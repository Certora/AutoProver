"""How much of the inferred property set a run pursues.

``comprehensive`` formalizes every property every component's inference produced —
the shape the pipeline has always had. ``prioritized`` ranks the whole candidate set
once, keeps the highest-contribution property plus the properties needed to support
it, and spends the formalization phase on that alone. The rest of the run (analysis,
harness, autosetup, property inference) is identical either way; only the batches
reaching formalization differ.

The mode is settable from the environment as well as the command line so the cloud
can turn it on by adding one variable to the Batch job, without a code change on
either side of the wrapper — the same route ``AUTOPROVER_GLOBAL_PROVER_TIMEOUT``
takes into ``composer.prover.core``.

``evaluate`` infers the property list the same way but formalizes nothing: it hands the
extracted batches to a mode implementation installed from another package (see
``composer/pipeline/MODES.md``), which measures the project's own specs and confs against
them and returns the report.
"""

import enum
import importlib.metadata
import os
from typing import Literal


type RunModeName = Literal["comprehensive", "prioritized", "evaluate"]
"""The mode's wire form: what a cache key, a report field or an env var carries. Spelled as a
closed literal so a key family cannot be handed an arbitrary string."""


class RunMode(enum.StrEnum):
    COMPREHENSIVE = "comprehensive"
    PRIORITIZED = "prioritized"
    EVALUATE = "evaluate"


RUN_MODE_ENV = "AUTOPROVER_RUN_MODE"

#: Entry-point group an out-of-tree mode implementation registers under. The entry-point name is
#: the mode's wire form (``evaluate``); the target is a ``composer.pipeline.modes.RunModeLoader``.
MODE_ENTRY_POINT_GROUP = "certora.autoprove.modes"

#: Modes whose implementation this package does not ship. A run in one of them needs an
#: implementation registered under :data:`MODE_ENTRY_POINT_GROUP`.
EXTERNAL_MODES: frozenset["RunMode"] = frozenset({RunMode.EVALUATE})


def installed_mode_names() -> set[str]:
    """The entry-point names registered under :data:`MODE_ENTRY_POINT_GROUP`. Reads metadata
    only: no implementation is imported or constructed."""
    return {ep.name for ep in importlib.metadata.entry_points(group=MODE_ENTRY_POINT_GROUP)}


def resolve_run_mode(cli: str | None) -> RunMode:
    """The run's mode: an explicit ``--run-mode`` wins, else ``AUTOPROVER_RUN_MODE``,
    else comprehensive.

    Resolution is deliberately *not* an argparse default: the entry points resolve it
    beside ``parse_budget_file`` so an unrecognised value fails the run before any
    service spins up, and so no flag's help text claims a default that came from the
    environment."""
    raw = cli if cli is not None else os.environ.get(RUN_MODE_ENV)
    if raw is None or not raw.strip():
        return RunMode.COMPREHENSIVE
    try:
        mode = RunMode(raw.strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in RunMode)
        source = "--run-mode" if cli is not None else RUN_MODE_ENV
        raise ValueError(
            f"Unknown run mode {raw!r} from {source}. Expected one of: {valid}."
        ) from None
    if mode in EXTERNAL_MODES and mode.value not in installed_mode_names():
        raise ValueError(
            f"Run mode {mode.value!r} has no implementation installed: register one under the "
            f"entry-point group {MODE_ENTRY_POINT_GROUP!r} with the name {mode.value!r}."
        )
    return mode


def run_mode_name(raw: str | None) -> RunModeName:
    """Narrow a stored mode tag to the closed set.

    Anything unrecognised, ``None`` included, reads as comprehensive: records written before the
    mode existed carry no tag, and they were all comprehensive runs."""
    if raw == RunMode.PRIORITIZED.value:
        return "prioritized"
    if raw == RunMode.EVALUATE.value:
        return "evaluate"
    return "comprehensive"
