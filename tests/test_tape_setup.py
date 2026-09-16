"""Preconditions for recording and replaying a tape, checked without recording or replaying one.

Two things live here. The first is a property of the tape *mechanism* and protects every tape in the
repo; the second is the CVLR scenario definition, whose whole job is to be the single spelling of a
run that gets recorded once and replayed forever.

Both are here because their failure modes are silent in the same expensive way: the tape gates are
``expensive``, so a break in either is found by whoever next spends an hour on a recording.
"""

import importlib
import importlib.util
import shutil
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

import composer.llm.registry as llm_registry
from composer.diagnostics.budget import BUDGET_PRESSURE_THRESHOLD
import composer.pipeline.cli as pipeline_cli
from composer.pipeline.cli import parse_budget_file
from composer.layout import INTERNAL_DIR
from composer.spec.cvlr.pipeline import WORK_DIR
from composer.spec.cvlr.scaffold import HARNESS_DIR
from composer.testing import cvlr_tape


# ---------------------------------------------------------------------------
# The seam every tape is installed through
# ---------------------------------------------------------------------------


def test_the_pipeline_looks_the_provider_up_through_its_module():
    """Both the fake LLM and the recorder install themselves by replacing
    ``composer.llm.registry.get_provider_for``. A module that did
    ``from composer.llm.registry import get_provider_for`` would bind the original at import time and
    keep it, and neither install would reach it.

    Neither failure is loud. On replay the pipeline calls a **real, paid** model inside a test that
    believes it is taped; on recording the tape comes out empty and says "no LLM responses captured"
    — at exit, after the run has been paid for. Hence a test rather than a comment.
    """
    assert not hasattr(pipeline_cli, "get_provider_for"), (
        "composer.pipeline.cli binds get_provider_for at import time; call it through the registry "
        "module instead, or no tape can replace it"
    )
    assert pipeline_cli.llm_registry is llm_registry


# ---------------------------------------------------------------------------
# The CVLR scenario definition
# ---------------------------------------------------------------------------


def test_the_scenario_the_tape_names_is_in_the_tree():
    source = cvlr_tape.scenario_source()
    assert source.is_dir(), source
    relative_main, _, identifier = cvlr_tape.MAIN_PROGRAM.partition(":")
    assert (source / relative_main).is_file(), f"{relative_main} is not in {source}"
    assert identifier, cvlr_tape.MAIN_PROGRAM
    assert (source / "system.md").is_file(), "the scenario's design document"


def test_staging_leaves_out_everything_a_run_generates(tmp_path):
    """A recording must start from a project that has not been scaffolded, or it records a run that
    skipped the scaffold — and the replay, starting clean, would then take a phase the tape has no
    lane for. The exclusions are what make a developer's already-built checkout record the same run
    as a clean CI one, so they are checked against a tree that has all of them.
    """
    dirty = tmp_path / "dirty"
    shutil.copytree(cvlr_tape.scenario_source(), dirty)
    package = dirty / "programs" / "vault"
    for generated in (
        dirty / "target",
        dirty / INTERNAL_DIR,
        dirty / WORK_DIR,
        dirty / "certora" / "cvlr",
        package / HARNESS_DIR,
    ):
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "leftover.txt").write_text("from an earlier run")

    staged = cvlr_tape.stage_scenario(tmp_path / "staged", source=dirty)

    assert not list(staged.rglob("leftover.txt")), sorted(
        str(p.relative_to(staged)) for p in staged.rglob("leftover.txt")
    )
    # The source the run is about is still there — an over-broad exclusion would be just as wrong.
    assert (staged / "programs" / "vault" / "src" / "lib.rs").is_file()
    assert (staged / "Cargo.lock").is_file(), (
        "the lock is committed for this scenario on purpose: it holds the transitive set the "
        "platform-tools cargo can parse"
    )


def test_the_recorded_run_does_not_ask_for_the_context_management_beta():
    """``memory_tool`` names the Anthropic beta, not the ``memory`` tool: all it does is add
    ``context-management-2025-06-27`` to the request (``composer.llm.anthropic``). Off here because
    that is what the expensive gate runs under, and because a beta is a request-shape change nobody
    controls between a recording and a replay months apart.

    The ``memory`` *tool* is bound unconditionally by every authoring agent and is not affected. It
    is the reason this setting is applied after parsing rather than through the CLI, and the reason
    a re-record goes through this module — see the scenario module's docstring for what keeps it
    from breaking a replay."""
    assert cvlr_tape.tape_args(Path("/proj")).memory_tool is False


def test_the_recorded_run_depends_on_no_corpus():
    """A corpus is optional and degrades to no search tools, so a tape recorded with one would
    replay differently on a machine that has it than on a machine that does not."""
    assert cvlr_tape.tape_args(Path("/proj")).rag_corpus == "none"


def test_the_recording_is_bounded():
    """The first recording ran unbudgeted, for 3h48m, and had to be interrupted with one unit of
    three delivered. The ceiling is what turns that failure into a bounded one — so it is checked
    here rather than left as a flag someone might drop while editing the argv.

    Parsed, not merely present: a budget file the pipeline would reject at startup is worse than
    none, because it fails after the operator has walked away."""
    argv = cvlr_tape.tape_argv(Path("/proj"))
    assert "--budget" in argv
    assert argv[argv.index("--budget") + 1] == str(cvlr_tape.BUDGET)
    budget = parse_budget_file(cvlr_tape.BUDGET)
    assert budget.total > 0


def test_the_ceiling_clears_the_curtailment_threshold():
    """A ceiling that curtails on a *normal* run is not a bound on a runaway, it is a cap on the
    work — and a curtailed largest unit is the one outcome that makes a recording less useful. The
    observed complete-run projection is ~$215 (see ``cvlr_tape.BUDGET``), and curtailment starts at
    ``BUDGET_PRESSURE_THRESHOLD`` of the total, so the total has to clear that with room."""
    budget = parse_budget_file(cvlr_tape.BUDGET)
    assert budget.total * BUDGET_PRESSURE_THRESHOLD > 215.0, (
        f"wrap-up would begin at ${budget.total * BUDGET_PRESSURE_THRESHOLD:.0f}, below the ~$215 a "
        f"complete three-unit recording has been measured to need"
    )


def test_the_argv_is_what_the_parser_is_given():
    """The one spelling. ``tape_argv`` is what the recording runs and what the replay parses, so a
    reader comparing the two is comparing one list with itself."""
    project = Path("/proj")
    argv = cvlr_tape.tape_argv(project)
    assert argv[0] == str(project)
    assert argv[1] == cvlr_tape.MAIN_PROGRAM
    args = cvlr_tape.tape_args(project)
    assert args.project_root == str(project)
    assert args.main_contract == cvlr_tape.MAIN_PROGRAM
    assert args.max_bug_rounds == cvlr_tape.MAX_BUG_ROUNDS


# ---------------------------------------------------------------------------
# What curation took out of the recorded tape
# ---------------------------------------------------------------------------


def _recorded_lanes() -> dict[str, list[AIMessage]]:
    module = f"composer.testing.ui_harness_{cvlr_tape.TAPE_NAME}"
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"no tape at {module} — record one with scripts/record_cvlr_tape.sh")
    return importlib.import_module(module).get_cvlr_vault_llm().lanes


def test_the_tape_carries_no_memory_calls():
    """A recording is a draft, and this is the one edit it always needs.

    The ``memory`` tool is bound unconditionally by every authoring agent and the agents use it
    heavily — the raw recording held 49 calls — but what they keep there is their own scratchpad:
    nothing downstream reads it and no assertion in the replay gate depends on it. Keeping the calls
    would therefore buy coverage of a tool this gate is not for, in exchange for carrying the
    divergence that is documented to exhaust a lane (a recorded read replayed against a store that
    errors, and LangGraph turning the error into a recovery turn the recording never made).

    Checked here rather than left to the curator's memory, because the cost of missing it is an
    hour of replay that fails somewhere in the middle, and the fix is mechanical.
    """
    offenders = [
        (lane, i, tc["name"])
        for lane, messages in _recorded_lanes().items()
        for i, message in enumerate(messages)
        for tc in message.tool_calls or []
        if tc["name"] == "memory"
    ]
    assert not offenders, (
        f"{len(offenders)} memory call(s) survived curation, e.g. {offenders[:3]}. Drop the turns "
        f"whose only tool call is memory, and strip the call from any that batched it with real work"
    )


def test_every_taped_turn_ends_in_a_tool_call_or_text():
    """A turn with neither is one the agent loop discards, and replaying it costs a spurious
    "every AI turn must end with a tool call" retry that the lane has no entry for.

    The recorder drops these on the way out; this is the check that it did, and that no hand-edit
    since has left one behind — deleting a message's last tool call without deleting the message is
    the easy way to make one.
    """
    empty = [
        (lane, i)
        for lane, messages in _recorded_lanes().items()
        for i, message in enumerate(messages)
        if not (message.tool_calls or []) and not _has_text(message)
    ]
    assert not empty, f"turns with neither text nor a tool call: {empty}"


def _has_text(message: AIMessage) -> bool:
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    return any(
        block.get("text", "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
