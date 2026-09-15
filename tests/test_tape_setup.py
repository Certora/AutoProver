"""Preconditions for recording and replaying a tape, checked without recording or replaying one.

Two things live here. The first is a property of the tape *mechanism* and protects every tape in the
repo; the second is the CVLR scenario definition, whose whole job is to be the single spelling of a
run that gets recorded once and replayed forever.

Both are here because their failure modes are silent in the same expensive way: the tape gates are
``expensive``, so a break in either is found by whoever next spends an hour on a recording.
"""

import shutil
from pathlib import Path

import composer.llm.registry as llm_registry
import composer.pipeline.cli as pipeline_cli
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


def test_the_recorded_run_authors_every_extracted_property():
    """No property cap: on replay the tape's own extraction response *is* the property set, so a cap
    would put the size of the run outside the transcript that defines it — and this would be the
    only tape in the repo whose shape is decided by a flag in the test rather than by its content."""
    assert cvlr_tape.tape_args(Path("/proj")).max_properties is None


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
