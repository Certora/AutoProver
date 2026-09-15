"""Record the CVLR smoke tape: one real, paid run of the scenario in :mod:`composer.testing.cvlr_tape`.

    COMPOSER_RECORD_TAPE=cvlr_vault uv run --no-sync python -m composer.testing.record_cvlr_tape

``scripts/record_cvlr_tape.sh`` is the supported way to invoke this — it checks the prerequisites
that otherwise fail late and namelessly, and it sets the environment. This module is what that
script runs.

**Why a module rather than ``console-solana``.** The skill's recipe records through the shipped CLI,
and here that would lose the one setting the parser exposes no flag for
(:func:`composer.testing.cvlr_tape.tape_args` — the memory tool). Going through the scenario
definition instead means the recording and the replay are configured by the same function, so
"record with exactly what you intend to replay" is a property of the code rather than of whoever
retyped the command.

``import composer.bind`` is the first statement, exactly as in
:mod:`composer.cli.console_solana`, and it is not decorative: ``bind`` is where
``COMPOSER_RECORD_TAPE`` installs the recorder, by replacing
``composer.llm.registry.get_provider_for``. Anything that bound that name at import time before the
patch keeps the original and is recorded as nothing at all — the failure prints "no LLM responses
captured" at exit, after the run has been paid for.
"""

import composer.bind as _  # noqa: F401  — must precede every other composer import

import asyncio
import sys
import tempfile
from pathlib import Path

from composer.diagnostics.timing import RunSummary
from composer.pipeline.ptypes import Curtailed, Delivered
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.testing.cvlr_tape import SCENARIO_NAME, run_scenario, stage_scenario


async def _main() -> int:
    # A fresh directory per recording, and printed rather than cleaned up: the run's deliverables
    # — the harness, the conf, report.json — are what a reader checks a recording against, and the
    # tape's curation pass needs them. Removing it would delete the evidence.
    workdir = Path(tempfile.mkdtemp(prefix="cvlr-tape-record-"))
    project = stage_scenario(workdir)
    print(f"[record] staged {SCENARIO_NAME} at {project}", file=sys.stderr)

    summary = RunSummary()
    result = await run_scenario(project, summary, GenericRustConsoleHandler(set()).make_handler)

    print(f"\n{'=' * 60}")
    print(summary.format())
    print(f"\n  Components:    {result.n_components}")
    print(f"  Properties:    {result.n_properties}")
    print(f"  Delivered:     {result.n_delivered}")
    for outcome in result.outcomes:
        published = outcome.result
        if isinstance(published, Curtailed):
            published = published.partial
        if not isinstance(published, Delivered):
            print(f"    - {outcome.feat.display_name}: no deliverable ({outcome.result})")
            continue
        print(f"    - {published.deliverable}")
        if (link := published.result.final_link) is not None:
            print(f"      {link}")
    for failure in result.failures:
        print(f"  FAILED: {failure}")
    print(f"\n  Run tree:      {project}")
    print(f"{'=' * 60}")

    # A recording that delivered nothing is not a tape worth curating: the lanes would carry the
    # run's flailing and no terminal `result`, which is the "missing terminal turn" the tape would
    # then have to be hand-authored around. Say so here rather than at replay time.
    if result.n_delivered == 0:
        print(
            "\n[record] this run delivered no harness — the tape it wrote has no clean ending. "
            "Read the run tree above before curating.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
