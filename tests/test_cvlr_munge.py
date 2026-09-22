"""Pointing a target at the Anchor fork Certora maintains, and refusing when it cannot.

Upstream ``anchor_lang::error::Error`` boxes its payload and the Solana Prover rejects that as
[3006]. On an Anchor target the fork is what makes a handler analyzable.

The failure is quiet. A target left on the crates.io crate builds, submits, and then reports
[3006], a pointer-analysis message with nothing about a fork. A version the fork does not cover
has to block. A project that already chooses where Anchor comes from has to be left alone.

No cargo and no network. ``Workspace`` objects are built in the test.
"""

import pytest
from pathlib import Path

from composer.cargo.metadata import (
    CratePackage,
    GitSource,
    PackageSource,
    RegistrySource,
    Workspace,
)
from composer.spec.cvlr.munge import (
    ANCHOR_FORK,
    SOLANA_OVERRIDES,
    MungeBlocked,
    already_patched,
    manifest_additions,
    plan_munge,
)

REGISTRY = RegistrySource("registry+https://github.com/rust-lang/crates.io-index")


def _workspace(root: Path, *resolved: CratePackage) -> Workspace:
    return Workspace(
        root=root, target_directory=root / "target", members=(), packages=resolved
    )


def _package(name: str, version: str, source: PackageSource | None = REGISTRY) -> CratePackage:
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
    """A property proved against a fork is a property of the fork. The patch section says so,
    next to the dependency it replaces."""
    addition = manifest_additions(plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1"))))
    assert "NOT the deployed program's" in addition
    assert "[3006]" in addition


def test_a_branch_is_named_rather_than_a_commit_pinned(tmp_path):
    """The lockfile records the commit, so the build stays reproducible without editing this
    manifest every time the fork moves."""
    addition = manifest_additions(plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1"))))
    assert "rev =" not in addition


@pytest.mark.parametrize("version", list(ANCHOR_FORK.branches))
def test_every_declared_version_maps_to_a_branch(tmp_path, version):
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", version)))
    assert plan.overrides and plan.overrides[0].branch == f"certora-v{version}"


# ---------------------------------------------------------------------------------------------
# what it refuses, and what it leaves alone


def test_an_uncovered_version_blocks_rather_than_leaving_the_boxing_in(tmp_path):
    """The fork covers 0.30.1 and not 0.30.0, which is why versions are listed. A derived name
    would send cargo after a branch that does not exist, and the error would be about git."""
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.30.0")))
    assert plan.overrides == ()
    assert len(plan.blocked) == 1
    assert "0.30.0" in plan.blocked[0].problem
    assert "do not verify against the unforked crate" in plan.blocked[0].resolution
    with pytest.raises(MungeBlocked):
        manifest_additions(plan)


def test_a_project_that_already_sources_anchor_itself_is_left_alone(tmp_path):
    """A path dependency means the project already decided where Anchor comes from. Overriding
    it would replace that choice."""
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1", source=None)))
    assert plan.overrides == ()
    assert plan.blocked == ()
    assert [a.crate for a in plan.already] == ["anchor-lang"]
    assert not plan.already[0].points_at_fork


def test_a_project_already_patched_to_the_fork_is_recognized_as_such(tmp_path):
    """A project already on the fork is recognized from the resolved graph. ``cargo metadata``
    reports the patch as a git source, not as the ``[patch.crates-io.<crate>]`` header this
    module writes. Projects write ``anchor-lang = { git = … }`` under one shared header. Searching
    for either spelling misses the other, and a second entry for a key TOML already has is a
    manifest cargo refuses."""
    patched = _package(
        "anchor-lang",
        "0.31.1",
        source=GitSource(
            "git+https://github.com/Certora/anchor.git?branch=certora-v0.31.1#3ebe7595"
        ),
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
        "anchor-lang",
        "0.31.1",
        source=GitSource("git+https://github.com/someone/anchor.git?branch=main"),
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
    """Read from ``Certora/anchor`` on 2026-09-01. The fork also has ``-pad-error`` and
    ``-reduce-error`` branches of 0.29.0. Those are experiments and are not in this list."""
    versions = list(ANCHOR_FORK.branches)
    assert versions == sorted(versions), "keep the list ordered so a gap is visible"
    assert len(set(versions)) == len(versions)
    for version, branch in ANCHOR_FORK.branches.items():
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
    """Patching only ``anchor-lang`` clears [3006], because the boxing is in
    ``anchor_lang::error``, and leaves ``anchor-spl`` upstream. Its ``TokenAccount`` and ``Mint``
    are newtypes with a private field. The fork adds ``new_unchecked`` for those, so a harness
    can build a token account. Both crates are redirected to the same branch."""
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
    """Two crates from one fork share one explanation. Repeating it under each reads like two
    unrelated edits."""
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
    """The known release is ``fixed`` 1.23.1 on ``certora-v1.23.1``. One branch is listed because
    that is the release the fork is known to cover."""
    plan = plan_munge(_workspace(tmp_path, _package("fixed", "1.23.1")))
    (override,) = plan.overrides
    assert override.repo == "https://github.com/Certora/fixed.git"
    assert override.branch == "certora-v1.23.1"


# ---------------------------------------------------------------------------------------------
# reading a patch table somebody else wrote


def test_the_inline_spelling_every_real_project_uses_is_recognized():
    """Projects write one ``[patch.crates-io]`` header with an inline table per crate. This
    module writes a ``[patch.crates-io.<crate>]`` sub-table. They are the same TOML and share no
    text, so a search for either misses the other."""
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
    """The graph is what cargo computed, but a snapshot taken before the patch table was applied
    still shows the registry. A second entry for a key TOML already has is a manifest cargo
    refuses."""
    plan = plan_munge(
        _workspace(tmp_path, _package("anchor-lang", "0.31.1")),
        already_redirected=frozenset({"anchor-lang"}),
    )
    assert plan.overrides == ()
    (already,) = plan.already
    assert "already redirected" in already.describe()
