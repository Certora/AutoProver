"""The CVLR smoke scenario: one definition, shared by the recording and the replay.

``docs/cvlr-backend-plan.md`` §6 asks for a Solana scenario with a recorded tape, giving the CVLR
backend the LLM-free end-to-end coverage the EVM and Foundry backends already have. A tape only
replays if the run that replays it is the run that was recorded — the lanes are keyed by ``run_task``
task id, and which lanes exist is decided by the analysis and extraction responses the tape itself
carries. So the invocation lives here rather than in a shell script beside a test that restates it:
:func:`tape_argv` is what the recorder runs and what the replay parses, and there is no second
spelling to drift.

Three things this pins that are worth stating, because each of them removes a class of replay
divergence rather than merely a symptom:

* **The Anthropic context-management beta is off** (``memory_tool``, which names the beta rather
  than the tool — all it does is add ``context-management-2025-06-27`` to the request). The same
  setting the expensive gate runs under, so the recording is made in the configuration this backend
  has actually been exercised in, and one fewer beta is one fewer thing that can change the request
  shape between a recording and a replay months apart.

  It does **not** remove the ``memory`` tool, which every authoring agent binds unconditionally
  through ``WorkflowContext.get_memory_tool``. That tool is the skill's named hazard — a recorded
  read that succeeded, replayed against an empty store, errors, and LangGraph turns the error into a
  recovery turn the recording never made, exhausting the lane. The argument that it would survive
  here is real: ``memory_ns`` is left unset, so the namespace is the run's own thread id (and a
  per-unit child of it), empty at the start of the recording and of the replay alike, so the same
  sequence of operations sees the same store.

  The tape does not rely on that argument. Curation removes every ``memory`` call instead — 47
  turns whose only tool call was one, and the call alone from two that batched it with real work.
  What those calls do is keep the agent's own scratchpad; nothing downstream reads them and no
  assertion in the gate depends on them. So retaining them buys coverage of a tool this gate is not
  for, in exchange for carrying the one divergence that is documented to exhaust a lane.
* **The corpus is off.** ``--rag-corpus none``, the same choice the expensive gate makes: a corpus
  is documented as optional and degrades to no search tools, so a tape that depends on one would
  replay differently on a machine that has it than on a machine that does not.
* **The scenario is staged as a copy, without the generated directories.** A run scaffolds the
  crate, writes a harness into ``src/certora`` and a working tree into ``.cvlr_work``; recording
  against a tree that already has them would record a run that skipped the scaffold.

Deliberately *not* pinned: the design document is passed explicitly rather than discovered. Doc
discovery is shared code with the EVM pipeline and already taped there, so what it would buy here
is one more lane to curate rather than coverage of anything CVLR-shaped.
"""

import pathlib
import shutil
from pathlib import Path
from typing import cast

from composer.diagnostics.timing import RunSummary
from composer.io.multi_job import HandlerFactory
from composer.layout import INTERNAL_DIR
from composer.spec.cvlr.entry import CvlrArgs, CvlrPipelineResult, build_parser, cvlr_executor
from composer.spec.cvlr.pipeline import WORK_DIR, CvlrPhase
from composer.spec.cvlr.scaffold import HARNESS_DIR

#: The tape's name: ``COMPOSER_RECORD_TAPE`` / ``COMPOSER_TEST_TAPE``, and the
#: ``composer/testing/ui_harness_<name>.py`` it is written to and replayed from.
TAPE_NAME = "cvlr_vault"

#: The checked-in scenario, under ``test_scenarios/``. The Anchor vault the expensive gate already
#: drives — the only Solana scenario in the tree the scaffold accepts, since it pins an Anchor major
#: that resolves the platform generation the CVLR reference set is bound to.
SCENARIO_NAME = "solana_vault_idl"

#: ``<path>:<identifier>``, as the CLI takes it. The path decides which crate the harness lands in;
#: the identifier is what the analysis is required to declare the program as.
MAIN_PROGRAM = "programs/vault/src/lib.rs:vault"

#: Enough to cover the extraction phase without recording rounds that repeat it.
MAX_BUG_ROUNDS = 1

#: The recording's spend ceiling. A *bound on the catastrophe*, not a target — the first recording
#: ran unbudgeted for 3h48m and was interrupted with one unit of three delivered, having spent
#: $166.50 (§7.8.5). Sized from that run's own manifest rather than guessed:
#:
#:   front half (analysis + three extractions)   $  1.53
#:   formalize-0  delivered                      $ 44.77
#:   formalize-1  lost to a prover-API drop      $  4.04
#:   formalize-2  interrupted, ten judge rounds  $116.16
#:
#: A complete three-unit run therefore projects to roughly $215, and
#: :data:`~composer.diagnostics.budget.BUDGET_PRESSURE_THRESHOLD` starts curtailing units at 80% of
#: the total — so a ceiling below about $270 would curtail the largest unit rather than bound a
#: runaway. $300 leaves that headroom.
#:
#: **Curtailment is not a failed recording.** A budget-curtailed unit still publishes what it has
#: and the run still reaches the report phase, so the tape is complete either way; what the ceiling
#: prevents is the previous run's actual failure, which was having no bound at all.
BUDGET = pathlib.Path(__file__).resolve().parent / "cvlr_tape_budget.json"


def scenario_source() -> Path:
    return Path(__file__).resolve().parents[2] / "test_scenarios" / SCENARIO_NAME


def stage_scenario(into: Path, source: Path | None = None) -> Path:
    """A pristine copy of the scenario under ``into``, ready to be scaffolded.

    ``source`` defaults to the checked-in scenario and exists so the exclusions below can be
    checked against a tree that actually has the generated directories in it — the checked-in one,
    by construction, does not.

    A copy because the run writes to the project, and pristine because the run's *first* act is to
    scaffold it: the generated directories are excluded so that a developer's locally-built,
    already-scaffolded checkout records and replays the same run as a clean CI one. ``target/`` is
    excluded for the same reason it is git-ignored, and because it is by far the largest thing there.
    """
    destination = into / SCENARIO_NAME
    shutil.copytree(
        source if source is not None else scenario_source(),
        destination,
        ignore=shutil.ignore_patterns(
            "target",
            str(INTERNAL_DIR),
            str(WORK_DIR),
            # One pattern for two directories, since `ignore_patterns` matches basenames anywhere:
            # the scaffold's harness module, which lives *inside* the crate's sources
            # (`src/certora`), and the deliverable directory the artifact store writes at the
            # project root. Safe for this scenario, which checks in neither; a target that kept its
            # own `certora/confs/base.conf` under version control would need a narrower rule, since
            # that conf is an input a run is supposed to read.
            HARNESS_DIR.name,
        ),
    )
    return destination


def tape_argv(project: Path) -> list[str]:
    """The exact command line the tape was recorded from and must be replayed with.

    Every flag here is part of the tape's identity, not a preference: the lane set follows from the
    phases the run takes, and the responses in a lane follow from the order the pipeline asked for
    them. A replay that differs fails as ``no tape lane for task_id`` or ``lane exhausted`` — loudly,
    but naming the symptom rather than the flag.
    """
    return [
        str(project),
        MAIN_PROGRAM,
        str(project / "system.md"),
        "--max-bug-rounds", str(MAX_BUG_ROUNDS),
        # See the module docstring: both of these remove a class of replay divergence.
        "--rag-corpus", "none",
        # Inert on replay — the tape decides what the run costs, which is nothing — and the whole
        # point on a recording. See :data:`BUDGET`.
        "--budget", str(BUDGET),
    ]


def tape_args(project: Path) -> CvlrArgs:
    """:func:`tape_argv` through the shipped parser, so the recording and the replay are configured
    by the same code a user's ``console-solana`` invocation is — defaults included.

    One setting is applied after parsing rather than through the command line, and it is the reason
    a re-recording must go through this module rather than through ``console-solana`` directly:
    ``memory_tool`` is part of the model-options surface but the parser exposes no switch for it,
    and it defaults to on. See the module docstring for what it actually selects — the Anthropic
    context-management beta, not the ``memory`` tool — and why a taped run wants it off.
    """
    args = build_parser().parse_args(tape_argv(project))
    args.memory_tool = False
    return cast(CvlrArgs, args)


async def run_scenario(
    project: Path, summary: RunSummary, handler: HandlerFactory[CvlrPhase, None]
) -> CvlrPipelineResult:
    """Drive the scenario through the shipped entry point.

    ``cvlr_executor`` rather than a hand-built ``PipelineRun``: it is the seam the entry point was
    split at (§7.8.2) precisely so a caller holding args reaches the run without ``sys.argv``, and
    going through it means the tape also covers package selection, the source surface, the
    confinement default and the artifact store's placement — everything the expensive gate builds
    for itself and therefore cannot check.
    """
    async with cvlr_executor(tape_args(project), summary) as run:
        return await run(handler)
