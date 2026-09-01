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
    MungeBlocked,
    manifest_additions,
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
    """A path or git dependency means somebody already decided where Anchor comes from — quite
    possibly the fork. Overriding that would replace a deliberate choice with a guess."""
    plan = plan_munge(_workspace(tmp_path, _package("anchor-lang", "0.31.1", source=None)))
    assert plan.overrides == ()
    assert plan.blocked == ()
    assert plan.inapplicable == ("anchor-lang",)


def test_a_target_that_is_not_an_anchor_program_needs_nothing(tmp_path):
    plan = plan_munge(_workspace(tmp_path, _package("solana-program", "2.3.0")))
    assert not plan
    assert manifest_additions(plan) == ""
    # Reported rather than dropped: "Anchor was not replaced" is what a reader of a [3006] failure
    # needs to know, and silence looks the same as success.
    assert plan.inapplicable == ("anchor-lang",)


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


def test_every_override_states_a_reason_naming_the_error_it_avoids():
    for override in SOLANA_OVERRIDES:
        assert "[3006]" in override.why, (
            f"{override.crate}'s reason should name the error it avoids — this string ends up in "
            f"somebody's Cargo.toml, and it is the only explanation they will get"
        )
        assert override.branches
