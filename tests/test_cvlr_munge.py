"""Pointing a target at the Anchor fork Certora maintains, and refusing when it cannot.

``docs/cvlr-backend-plan.md`` §7.6. Upstream ``anchor_lang::error::Error`` boxes its payload and the
Solana Prover rejects that as [3006], so on an Anchor target the difference between the fork and
crates.io is the difference between a rule that can be analyzed and one that cannot.

Which fixes what these tests are for. The failure mode is silence: a target left on the crates.io
crate builds fine, submits fine, and then reports [3006] — a message about pointer analysis, with no
hint that a fork exists. So the cases worth pinning are the ones where nothing would otherwise
complain: a version the fork does not cover must **block** rather than fall through, and a project
that already sources Anchor itself must be left alone rather than overridden.

No cargo and no network — ``Workspace`` objects are built directly. The fork actually building is
covered by ``tests/test_cvlr_anchor_reach.py``, which is expensive.
"""

from pathlib import Path

import pytest

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.munge import (
    ANCHOR_FORK,
    SOLANA_OVERRIDES,
    AlreadyMunged,
    EarlyPanic,
    FunctionAmbiguous,
    FunctionMunge,
    FunctionNotFound,
    MockFn,
    MungeBlocked,
    Munged,
    already_patched,
    apply_munge,
    manifest_additions,
    merge_munges,
    munge_history,
    plan_munge,
)

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def _workspace(root: Path, *resolved: CratePackage) -> Workspace:
    return Workspace(
        root=root, target_directory=root / "target", members=(), packages=resolved
    )


def _package(name: str, version: str, source: str | None = REGISTRY) -> CratePackage:
    return CratePackage(
        name=name,
        version=version,
        manifest_path=Path("/nonexistent") / f"{name}-{version}" / "Cargo.toml",
        lib=None,
        features=(),
        source=source,
    )


# ---------------------------------------------------------------------------------------------
# what it writes


def test_a_covered_anchor_version_is_pointed_at_its_branch(tmp_path):
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1")))
    assert plan.blocked == ()
    (override,) = plan.overrides
    assert override.branch == "certora-v0.31.1"
    assert override.repo == "https://github.com/Certora/anchor.git"


def test_the_manifest_addition_redirects_the_graph_at_the_fork(tmp_path):
    addition = manifest_additions(plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1"))))
    assert "[patch.crates-io.anchor-lang]" in addition
    assert 'git = "https://github.com/Certora/anchor.git"' in addition
    assert 'branch = "certora-v0.31.1"' in addition


def test_the_manifest_says_these_are_not_the_deployed_dependencies(tmp_path):
    """The one thing a reader of that manifest must not have to infer. A property proved against a
    fork is a property of the fork, and the section that swaps the dependency is where somebody will
    be standing when the question occurs to them."""
    addition = manifest_additions(plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1"))))
    assert "NOT the deployed program's" in addition
    assert "[3006]" in addition


def test_a_branch_is_named_rather_than_a_commit_pinned(tmp_path):
    """What the reference project does: the lockfile records the commit, so the build is
    reproducible without this manifest needing an edit every time the fork picks up a fix."""
    addition = manifest_additions(plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1"))))
    assert "rev =" not in addition


@pytest.mark.parametrize("version", [v for v, _ in ANCHOR_FORK.branches])
def test_every_declared_version_maps_to_a_branch(tmp_path, version):
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", version)))
    assert plan.overrides and plan.overrides[0].branch == f"certora-v{version}"


# ---------------------------------------------------------------------------------------------
# what it refuses, and what it leaves alone


def test_an_uncovered_version_blocks_rather_than_leaving_the_boxing_in(tmp_path):
    """The case that would otherwise be silent. 0.30.0 is deliberate: the fork covers 0.30.1 and not
    0.30.0, which is exactly why the versions are listed rather than derived from a pattern — a
    derived name would send cargo looking for a branch that does not exist, and the error would be
    about git rather than about Anchor coverage."""
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.30.0")))
    assert plan.overrides == ()
    assert len(plan.blocked) == 1
    assert "0.30.0" in plan.blocked[0].problem
    assert "do not verify against the unforked crate" in plan.blocked[0].resolution
    with pytest.raises(MungeBlocked):
        manifest_additions(plan)


def test_a_project_that_already_sources_anchor_itself_is_left_alone(tmp_path):
    """A path dependency means somebody already decided where Anchor comes from. Overriding that
    would replace a deliberate choice with a guess."""
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1", source=None)))
    assert plan.overrides == ()
    assert plan.blocked == ()
    assert [a.crate for a in plan.already] == ["anchor-lang"]
    assert not plan.already[0].points_at_fork


def test_a_project_already_patched_to_the_fork_is_recognized_as_such(tmp_path):
    """The bug this replaced. Detection used to search Cargo.toml for a
    ``[patch.crates-io.anchor-lang]`` header, and no real project writes that spelling — every one
    of them uses the inline ``anchor-lang = {{ git = … }}`` form under a shared header. So the search
    found nothing, the scaffold appended a second entry for a key TOML already had, and cargo failed
    outright: the projects already doing the right thing were the ones it broke.

    Seen from the resolved graph instead, which is what cargo itself computed after applying the
    patch table."""
    patched = _package(
        "anchor-lang",
        "0.31.1",
        source="git+https://github.com/Certora/anchor.git?branch=certora-v0.31.1#3ebe7595",
    )
    plan = plan_munge(_workspace(tmp_path, patched))
    assert plan.overrides == ()
    assert plan.blocked == ()
    (already,) = plan.already
    assert already.points_at_fork
    assert "nothing to do" in already.describe()


def test_a_project_sourcing_anchor_from_some_other_fork_is_left_alone_and_said_so(tmp_path):
    """The two cases read identically from the outside and only one of them is fine. This module
    will not override somebody's choice, but a reader of a [3006] failure needs to know it was
    made."""
    other = _package(
        "anchor-lang", "0.31.1", source="git+https://github.com/someone/anchor.git?branch=main"
    )
    plan = plan_munge(_workspace(tmp_path, other))
    (already,) = plan.already
    assert not already.points_at_fork
    assert "someone/anchor" in already.describe()
    assert "will not analyze" in already.describe()


def test_a_target_that_is_not_an_anchor_program_needs_nothing(tmp_path):
    plan = plan_munge(_workspace(tmp_path, _package("solana-program", "2.3.0")))
    assert not plan
    assert manifest_additions(plan) == ""
    # Reported rather than dropped: "Anchor was not replaced" is what a reader of a [3006] failure
    # needs to know, and silence looks the same as success.
    assert set(plan.inapplicable) == {"anchor-lang", "anchor-spl", "fixed"}


# ---------------------------------------------------------------------------------------------
# keeping the declaration honest


def test_the_branch_list_matches_what_the_fork_publishes():
    """The list is a claim about another repository, so it is worth stating what it was checked
    against. Read from `Certora/anchor` on 2026-09-01; the fork also carries `-pad-error` and
    `-reduce-error` variants of 0.29.0, deliberately excluded as experiments."""
    versions = [v for v, _ in ANCHOR_FORK.branches]
    assert versions == sorted(versions), "keep the list ordered so a gap is visible"
    assert len(set(versions)) == len(versions)
    for version, branch in ANCHOR_FORK.branches:
        assert branch == f"certora-v{version}"
    # The gap that motivates listing rather than deriving.
    assert "0.30.0" not in versions
    assert "0.30.1" in versions


def test_every_override_says_why_it_exists_and_covers_at_least_one_version():
    for fork in SOLANA_OVERRIDES:
        assert fork.crates and fork.branches
        assert len(fork.why) > 80, (
            f"{fork.crates}'s reason ends up verbatim in somebody's Cargo.toml, and it is the only "
            f"explanation they will get"
        )


def test_the_anchor_fork_covers_both_crates_it_publishes(tmp_path):
    """Patching only anchor-lang clears [3006], because the boxing is in ``anchor_lang::error``, and
    then leaves anchor-spl as the upstream crate — whose ``TokenAccount`` and ``Mint`` are newtypes
    with a private field. The fork adds ``new_unchecked`` for exactly those, so a harness over a
    token program cannot build an account without it. Both corpus projects that verify an Anchor
    program patch both crates, to the same branch."""
    assert set(ANCHOR_FORK.crates) == {"anchor-lang", "anchor-spl"}
    plan = plan_munge(
        _workspace(
            tmp_path, _package("anchor-lang", "0.31.1"), _package("anchor-spl", "0.31.1")
        )
    )
    assert {o.crate: o.branch for o in plan.overrides} == {
        "anchor-lang": "certora-v0.31.1",
        "anchor-spl": "certora-v0.31.1",
    }


def test_one_forks_two_crates_share_one_reason_in_the_manifest(tmp_path):
    """Repeating a paragraph verbatim under each crate reads like two unrelated changes that happen
    to say the same thing."""
    addition = manifest_additions(
        plan_munge(
            _workspace(
                tmp_path, _package("anchor-lang", "0.31.1"), _package("anchor-spl", "0.31.1")
            )
        )
    )
    assert addition.count("Upstream anchor_lang::error::Error boxes its payload") == 1
    assert "anchor-lang 0.31.1 -> certora-v0.31.1" in addition
    assert "anchor-spl 0.31.1 -> certora-v0.31.1" in addition


def test_the_fixed_fork_is_planned_from_the_version_the_corpus_pins(tmp_path):
    """Both corpus projects that use it resolve ``fixed`` 1.23.1 to ``certora-v1.23.1``, at the same
    commit. One branch is listed because one is what there is evidence for."""
    plan = plan_munge(_workspace(tmp_path, _package("fixed", "1.23.1")))
    (override,) = plan.overrides
    assert override.repo == "https://github.com/Certora/fixed.git"
    assert override.branch == "certora-v1.23.1"


# ---------------------------------------------------------------------------------------------
# reading a patch table somebody else wrote


def test_the_inline_spelling_every_real_project_uses_is_recognized():
    """The spelling that mattered. Nine corpus projects carry a patch table; all nine write one
    shared ``[patch.crates-io]`` header with an inline table per crate, and none writes the
    per-crate sub-table form this module emits. They are the same TOML and share no text, so a
    search for either misses the other."""
    inline = """
[patch.crates-io]
anchor-lang = { git = "https://github.com/Certora/anchor.git", branch = "certora-v0.29.0" }
anchor-spl = { git = "https://github.com/Certora/anchor.git", branch = "certora-v0.29.0" }
spl-token-2022 = { git = "https://github.com/example/solana-program-library.git" }
"""
    assert already_patched(inline) == frozenset(
        {"anchor-lang", "anchor-spl", "spl-token-2022"}
    )


def test_the_subtable_spelling_this_module_writes_is_recognized_too():
    subtables = """
[patch.crates-io.anchor-lang]
git = "https://github.com/Certora/anchor.git"
branch = "certora-v0.31.1"
"""
    assert already_patched(subtables) == frozenset({"anchor-lang"})


def test_a_manifest_with_no_patch_table_redirects_nothing():
    assert already_patched('[workspace]\nmembers = ["."]\n') == frozenset()
    assert already_patched("[patch]\n") == frozenset()


def test_an_unparseable_manifest_is_not_a_reason_to_refuse_to_scaffold():
    """cargo parsed this manifest to produce the graph, so failing here means this reader disagrees
    with cargo's — which is worth a log line and not worth stopping over. The graph still catches
    the redirect on the next run."""
    assert already_patched("[patch.crates-io\nthis is not toml") == frozenset()


def test_a_crate_the_table_already_names_is_left_alone(tmp_path):
    """Belt to the graph's braces. The graph is the better source — it is what cargo computed — but
    it is a snapshot, and a snapshot taken before the patch table was applied still shows the
    registry. Appending a second entry for a key TOML already has is a manifest cargo refuses."""
    plan = plan_munge(
        _workspace(tmp_path, _package("anchor-lang", "0.31.1")),
        already_redirected=frozenset({"anchor-lang"}),
    )
    assert plan.overrides == ()
    (already,) = plan.already
    assert "already redirected" in already.describe()


# ---------------------------------------------------------------------------------------------
# the source half
#
# The charter (plan §7.6.3) is six kinds read off the one real source munge in the corpus, and two of
# them are CVLR attributes an author can apply mechanically. What makes that safe rather than an
# agent editing a program is that the vocabulary is closed, the insert is one line, and a compile
# gate sits behind it. What a compile gate cannot catch is naming the wrong function, which is what
# these refusals are for.

SOURCE = """\
use anchor_lang::prelude::*;

/// Redeem the protocol's accumulated fees.
pub fn redeem_fees(reserve: &mut Reserve, slot: Slot) -> Result<u64> {
    let amount = reserve.calculate_fees()?;
    Ok(amount)
}

fn redeem_fees_inner(x: u64) -> u64 {
    x
}

impl Reserve {
    pub fn calculate_fees(&self) -> Result<u64> {
        Ok(0)
    }
}
"""

FEATURE = "certora"


def _munge(function: str, kind=None, path: str = "programs/p/src/reserve.rs") -> FunctionMunge:
    return FunctionMunge(
        path=path, function=function, kind=kind or EarlyPanic(), why="[3308] on the `?` path"
    )


def test_the_attribute_lands_above_the_signature_and_below_the_doc_comment():
    """Below the doc comment keeps the insert a one-line edit whose diff reads as one change; the
    attribute works from either position."""
    result = apply_munge(SOURCE, _munge("redeem_fees"), FEATURE)
    assert isinstance(result, Munged)
    lines = result.source.splitlines()
    assert lines[result.line - 2] == '#[cfg_attr(feature = "certora", cvlr::early_panic)]'
    assert lines[result.line - 1].startswith("pub fn redeem_fees(")
    assert lines[result.line - 3].startswith("/// Redeem")


def test_a_longer_name_sharing_a_prefix_is_not_the_same_function():
    """`fn redeem_fees` must not match `fn redeem_fees_inner`. The trailing `(` or `<` is what
    settles it, and getting this wrong munges a function nobody asked about."""
    result = apply_munge(SOURCE, _munge("redeem_fees_inner"), FEATURE)
    assert isinstance(result, Munged)
    assert result.source.splitlines()[result.line - 1].startswith("fn redeem_fees_inner(")


def test_a_mock_names_its_stand_in_in_the_attribute():
    result = apply_munge(
        SOURCE, _munge("calculate_fees", MockFn(stand_in="crate::certora::mocks::fees")), FEATURE
    )
    assert isinstance(result, Munged)
    assert (
        '#[cfg_attr(feature = "certora", cvlr::mock_fn(with = crate::certora::mocks::fees))]'
        in result.source
    )


def test_the_indentation_of_the_function_is_matched():
    """`calculate_fees` is inside an impl block. An attribute at column zero above an indented `fn`
    compiles and reads as though nobody looked."""
    result = apply_munge(SOURCE, _munge("calculate_fees"), FEATURE)
    assert isinstance(result, Munged)
    assert '    #[cfg_attr(feature = "certora", cvlr::early_panic)]' in result.source


def test_a_function_the_file_does_not_define_is_refused_with_what_it_does():
    """A compile gate would catch this two minutes and one build later, and say nothing about the
    name that was meant."""
    result = apply_munge(SOURCE, _munge("redeem_fee"), FEATURE)
    assert isinstance(result, FunctionNotFound)
    assert "redeem_fees" in result.nearby


def test_two_functions_of_one_name_are_refused_rather_than_guessed_at():
    """The failure a compile *accepts*: munging the wrong one of two same-named functions builds
    fine, leaves the rule failing, and gives no indication which was changed."""
    twice = SOURCE + """
impl Collateral {
    pub fn calculate_fees(&self) -> Result<u64> {
        Ok(1)
    }
}
"""
    result = apply_munge(twice, _munge("calculate_fees"), FEATURE)
    assert isinstance(result, FunctionAmbiguous)
    assert len(result.lines) == 2


def test_re_applying_a_munge_is_recognized_rather_than_doubled():
    """`stage` re-applies every recorded munge on each build, from whatever is on disk. That is only
    safe because the second application reports the attribute already there."""
    once = apply_munge(SOURCE, _munge("redeem_fees"), FEATURE)
    assert isinstance(once, Munged)
    twice = apply_munge(once.source, _munge("redeem_fees"), FEATURE)
    assert isinstance(twice, AlreadyMunged)


def test_a_munge_invalidates_a_stamp_earned_before_it():
    """The same channel a summary uses, for the stronger version of the reason: a munge changes the
    program the previous run's verdicts were about."""
    assert munge_history(()) == ()
    early = munge_history((_munge("redeem_fees"),))
    mocked = munge_history((_munge("redeem_fees", MockFn(stand_in="crate::m")),))
    assert early != mocked


def test_rewording_a_justification_does_not_cost_a_submission():
    """Keyed on what the prover sees differently — the file, the function, the attribute — and not
    on `why`, exactly as `summary_history` is."""
    one = _munge("redeem_fees")
    two = FunctionMunge(path=one.path, function=one.function, kind=one.kind, why="clearer wording")
    assert munge_history((one,)) == munge_history((two,))


def test_the_same_munge_recorded_twice_lands_once():
    """A reducer for the reason `merge_summaries` is one: several tool calls can land in one graph
    step, and LangGraph refuses two writes to an unreduced key."""
    one = _munge("redeem_fees")
    assert len(merge_munges([one], [one])) == 1
    assert len(merge_munges([one], [_munge("calculate_fees")])) == 2
    # Same function, different file: two munges, not one.
    other_file = _munge("redeem_fees", path="programs/p/src/other.rs")
    assert len(merge_munges([one], [other_file])) == 2


# ---------------------------------------------------------------------------------------------
# what the deliverable says about it
#
# A munge changes the program the verdicts are about, so the report owes a reader that fact. It is
# said through the shared `source_edits` hook rather than a CVLR-specific one, because
# `SourceEditRecord`'s own docstring is already exactly this disclosure: its presence means "the
# component's outcomes are claims about the modified code, not the code as shipped".


def _target(workdir: Path):
    """A `HarnessTarget` for path questions only — the rest of it is not `source_path`'s business."""
    from types import SimpleNamespace

    from composer.spec.cvlr.verify import HarnessTarget

    return HarnessTarget(
        session=SimpleNamespace(workdir=workdir),  # type: ignore[arg-type]
        module_path=workdir / "src" / "spec.rs",
        package="p",
        tuning=SimpleNamespace(),  # type: ignore[arg-type]
    )


def test_a_path_leaving_the_workdir_is_refused(tmp_path):
    """The workdir is this unit's private copy of the project, so writing in it never touches the
    user's tree — but only while every write stays inside it."""
    from composer.spec.cvlr.verify import NotInWorkdir

    workdir = tmp_path / "work"
    (workdir / "src").mkdir(parents=True)
    target = _target(workdir)
    assert target.source_path("src/lib.rs") == (workdir / "src" / "lib.rs").resolve()
    assert isinstance(target.source_path("../outside.rs"), NotInWorkdir)
    assert isinstance(target.source_path("/etc/passwd"), NotInWorkdir)


def test_a_dependency_inside_the_workdir_is_refused_too(tmp_path):
    """Containment is not the question, and this is the case that shows why.

    Confinement gives each unit a private ``CARGO_HOME`` at ``<workdir>/.sandbox_cargo``, so every
    dependency's unpacked source sits inside the workdir, one directory away from the program. A
    check that stopped at "is it in the workdir" would let a munge rewrite Anchor — for every crate
    in the graph, including the ones the property is about. Same failure
    ``validate_rule_subjects`` prevents one axis over.
    """
    from composer.spec.cvlr.munge import NotProjectSource

    workdir = tmp_path / "work"
    (workdir / "src").mkdir(parents=True)
    target = _target(workdir)

    anchor = ".sandbox_cargo/registry/src/index.crates.io-6f17d22/anchor-lang-0.31.1/src/error.rs"
    refusal = target.source_path(anchor)
    assert isinstance(refusal, NotProjectSource)
    assert refusal.directory == ".sandbox_cargo"

    for built in ("target/debug/build/x/out/gen.rs", ".certora_internal/x.rs", "certora_out/y.rs"):
        assert isinstance(target.source_path(built), NotProjectSource), built


def test_the_copy_and_the_munge_rule_are_one_list():
    """What a copy of the project leaves out is what a munge of the project may not touch. Drift in
    one direction is the dangerous one: a directory added to the copy's ignore list and not to this
    one would become munge-able."""
    from composer.spec.cvlr.munge import NOT_PROJECT_SOURCE, is_project_source
    from composer.spec.cvlr.pipeline import WORK_DIR

    assert WORK_DIR.name in NOT_PROJECT_SOURCE
    assert ".sandbox_cargo" in NOT_PROJECT_SOURCE
    assert is_project_source("programs/vault/src/lib.rs")
    assert not is_project_source("target/debug/deps/x.rs")
    # An empty path names no file and is not source.
    assert not is_project_source("")


@pytest.mark.asyncio
async def test_a_delivered_units_munges_reach_the_report_as_source_edits(tmp_path):
    from types import SimpleNamespace

    from composer.pipeline.ptypes import Delivered
    from composer.spec.cvlr.harness import GeneratedHarness
    from composer.spec.cvlr.pipeline import WORK_DIR, CvlrFormalizer

    relative = "programs/p/src/reserve.rs"
    (tmp_path / "programs" / "p" / "src").mkdir(parents=True)
    (tmp_path / relative).write_text(SOURCE)

    workdir = tmp_path / WORK_DIR / "vault"
    (workdir / "programs" / "p" / "src").mkdir(parents=True)
    munge = _munge("redeem_fees", path=relative)
    applied = apply_munge(SOURCE, munge, FEATURE)
    assert isinstance(applied, Munged)
    (workdir / relative).write_text(applied.source)

    harness = GeneratedHarness(commentary="", harness="", munges=[munge])
    outcome = SimpleNamespace(
        feat=SimpleNamespace(slug="vault", display_name="Vault"),
        result=Delivered(result=harness, deliverable=Path("x.rs")),
    )
    run = SimpleNamespace(source=SimpleNamespace(project_root=str(tmp_path)))

    formalizer = CvlrFormalizer(GeneratedHarness, "prover", SimpleNamespace())
    (record,) = await formalizer.source_edits([outcome], run)  # type: ignore[arg-type]

    assert record.component == "Vault"
    (edit,) = record.applied_edits
    assert edit.why_sound == "[3308] on the `?` path"
    assert "redeem_fees" in edit.executive_summary
    assert "`?` rewritten to `.unwrap()`" in edit.executive_summary
    # The diff is against the project tree, which is the pristine copy: a unit only ever writes in
    # its own workdir.
    assert '+#[cfg_attr(feature = "certora", cvlr::early_panic)]' in record.cumulative_diff
    assert f"a/{relative}" in record.cumulative_diff


@pytest.mark.asyncio
async def test_a_unit_that_munged_nothing_contributes_no_record(tmp_path):
    """The record's presence is the claim. An empty one would say the outcomes are about modified
    code when they are not."""
    from types import SimpleNamespace

    from composer.pipeline.ptypes import Delivered, GaveUp
    from composer.spec.cvlr.harness import GeneratedHarness
    from composer.spec.cvlr.pipeline import CvlrFormalizer

    run = SimpleNamespace(source=SimpleNamespace(project_root=str(tmp_path)))
    clean = SimpleNamespace(
        feat=SimpleNamespace(slug="a", display_name="A"),
        result=Delivered(result=GeneratedHarness(commentary="", harness=""), deliverable=Path("a")),
    )
    gave_up = SimpleNamespace(
        feat=SimpleNamespace(slug="b", display_name="B"), result=GaveUp(reason="no")
    )
    formalizer = CvlrFormalizer(GeneratedHarness, "prover", SimpleNamespace())
    assert await formalizer.source_edits([clean, gave_up], run) == []  # type: ignore[arg-type]
