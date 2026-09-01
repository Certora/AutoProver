"""Munging a resolved dependency: what gets patched, and what refuses.

``docs/cvlr-backend-plan.md`` §7.6 and ``docs/upstream-defects.md`` P1. The munge exists because
``anchor_lang::error::Error`` boxes its payload and the Solana Prover rejects that as [3006], so on an
Anchor target the difference between munged and not is the difference between a rule that can be
analyzed and one that cannot.

Which makes the risk here specific: **a munge that silently does not apply is worse than one that
fails.** The build still succeeds, the prover still runs, and the error it then reports is the original
defect — so the symptom points at the munge not working rather than at the munge not having happened.
Most of what follows is about that: exact match counts, declared version lines, and a blocked plan
applying nothing.

No cargo and no network — ``Workspace`` objects are built directly, and the patch is exercised against
a stub source tree rather than the real crate, so these run in the routine env. The real crate is
covered by ``tests/test_cvlr_anchor_reach.py``, which is expensive.
"""

import dataclasses
from pathlib import Path

import pytest

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.munge import (
    ANCHOR_UNBOX,
    MUNGE_DIR,
    SOLANA_PATCHES,
    CratePatch,
    MungeBlocked,
    Replacement,
    apply_munge,
    manifest_additions,
    plan_munge,
)

#: The lines of anchor-lang 0.31.1's ``src/error.rs`` the patch touches, verbatim. Enough of the real
#: file to exercise every replacement, and no more — a stub keeps the test honest about *which* text
#: the patch depends on.
ERROR_RS = """\
#[derive(Debug)]
pub enum Error {
    AnchorError(Box<AnchorError>),
    ProgramError(Box<ProgramErrorWithOrigin>),
}

impl From<AnchorError> for Error {
    fn from(ae: AnchorError) -> Self {
        Self::AnchorError(Box::new(ae))
    }
}

impl From<ProgramError> for Error {
    fn from(program_error: ProgramError) -> Self {
        Self::ProgramError(Box::new(program_error.into()))
    }
}

impl From<BorshIoError> for Error {
    fn from(error: BorshIoError) -> Self {
        Error::ProgramError(Box::new(ProgramError::from(error).into()))
    }
}

impl From<ProgramErrorWithOrigin> for Error {
    fn from(pe: ProgramErrorWithOrigin) -> Self {
        Self::ProgramError(Box::new(pe))
    }
}

impl From<TryFromIntError> for Error {
    fn from(e: TryFromIntError) -> Self {
        Self::AnchorError(Box::new(AnchorError {
            error_name: ErrorCode::InvalidNumericConversion.name(),
            error_code_number: ErrorCode::InvalidNumericConversion.into(),
            error_msg: format!("{}", e),
            error_origin: None,
            compared_values: None,
        }))
    }
}
"""


def _crate(root: Path, name: str, version: str, files: dict[str, str]) -> CratePackage:
    """An unpacked registry source on disk, plus the package cargo would report for it."""
    source = root / "registry" / f"{name}-{version}"
    for relative, contents in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (source / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "{version}"\n')
    # Registry checkouts are read-only, which the munge has to cope with.
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    return CratePackage(
        name=name,
        version=version,
        manifest_path=source / "Cargo.toml",
        lib=None,
        features=(),
        source="registry+https://github.com/rust-lang/crates.io-index",
    )


def _workspace(root: Path, *packages: CratePackage) -> Workspace:
    (root / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
    return Workspace(
        root=root,
        target_directory=root / "target",
        members=(),
        packages=packages,
    )


@pytest.fixture
def anchor_project(tmp_path: Path) -> Workspace:
    return _workspace(
        tmp_path, _crate(tmp_path, "anchor-lang", "0.31.1", {"src/error.rs": ERROR_RS})
    )


# ---------------------------------------------------------------------------------------------
# what it patches


def test_the_munge_removes_every_box_from_the_error_type(anchor_project, tmp_path):
    plan = plan_munge(anchor_project)
    assert plan.blocked == ()
    assert [c.crate for c in plan.crates] == ["anchor-lang"]

    apply_munge(plan, tmp_path)
    patched = (tmp_path / MUNGE_DIR / "anchor-lang" / "src" / "error.rs").read_text()

    assert "Box" not in patched, "the point of the munge is that no Box::new survives"
    assert "AnchorError(AnchorError)," in patched
    assert "ProgramError(ProgramErrorWithOrigin)," in patched


def test_the_munged_source_still_balances_its_delimiters(anchor_project, tmp_path):
    """The one replacement that opens a block also has to close it a level shallower, and a
    mismatched paren produces a Rust file that does not parse — which surfaces as a compile error
    inside a *dependency*, the least legible place for it."""
    apply_munge(plan_munge(anchor_project), tmp_path)
    patched = (tmp_path / MUNGE_DIR / "anchor-lang" / "src" / "error.rs").read_text()
    for opener, closer in (("(", ")"), ("{", "}")):
        assert patched.count(opener) == patched.count(closer), f"unbalanced {opener}{closer}"


def test_the_copy_is_writable_although_the_registry_source_is_not(anchor_project, tmp_path):
    """Cargo checks registry sources out read-only. A munge that copied the mode would fail on its
    own second edit, and on any later re-munge."""
    apply_munge(plan_munge(anchor_project), tmp_path)
    copied = tmp_path / MUNGE_DIR / "anchor-lang" / "src" / "error.rs"
    copied.write_text(copied.read_text() + "// still writable\n")


def test_applying_twice_converges(anchor_project, tmp_path):
    """The copies are derived from the registry source plus a declared patch, so re-running must
    replace rather than re-patch. Patching an already-patched file would fail the match counts — that
    is the safe failure — but leaving a partly-copied tree from an interrupted run would not."""
    plan = plan_munge(anchor_project)
    apply_munge(plan, tmp_path)
    first = (tmp_path / MUNGE_DIR / "anchor-lang" / "src" / "error.rs").read_text()
    apply_munge(plan, tmp_path)
    assert (tmp_path / MUNGE_DIR / "anchor-lang" / "src" / "error.rs").read_text() == first


def test_the_munge_records_what_it_changed_and_why(anchor_project, tmp_path):
    """A munged dependency is the least obvious thing in a verification project. Somebody who finds
    the directory has to be able to learn what it is without reading this codebase — and has to be
    told that a property proved here is a property of the copy."""
    apply_munge(plan_munge(anchor_project), tmp_path)
    readme = (tmp_path / MUNGE_DIR / "README.md").read_text()
    assert "anchor-lang 0.31.1" in readme
    assert "[3006]" in readme
    assert "not the deployed program" in readme


def test_the_manifest_addition_redirects_the_graph(anchor_project):
    addition = manifest_additions(plan_munge(anchor_project))
    assert "[patch.crates-io.anchor-lang]" in addition
    assert 'path = ".cvlr_munge/anchor-lang"' in addition


def test_a_target_with_nothing_to_munge_gets_an_empty_plan(tmp_path):
    plan = plan_munge(_workspace(tmp_path))
    assert not plan
    assert plan.crates == ()
    assert manifest_additions(plan) == ""
    # Reported rather than dropped: "Anchor was not munged" is what a reader of a [3006] failure
    # needs to know, and silence looks the same as success.
    assert plan.inapplicable == ("anchor-lang",)


# ---------------------------------------------------------------------------------------------
# what refuses


def test_an_unrecognised_version_blocks_rather_than_patching_on_textual_luck(tmp_path):
    """The text might well match in a version nobody checked. That is not evidence the patch is
    right there — the same lines can mean something different around them."""
    workspace = _workspace(
        tmp_path, _crate(tmp_path, "anchor-lang", "0.28.0", {"src/error.rs": ERROR_RS})
    )
    plan = plan_munge(workspace)
    assert plan.crates == ()
    assert len(plan.blocked) == 1
    assert "0.28.0" in plan.blocked[0].problem
    with pytest.raises(MungeBlocked):
        apply_munge(plan, tmp_path)


def test_a_blocked_plan_writes_nothing(tmp_path):
    """Same rule the scaffold follows: a half-munged project turns the next failure into a question
    with two candidate answers."""
    workspace = _workspace(
        tmp_path, _crate(tmp_path, "anchor-lang", "0.28.0", {"src/error.rs": ERROR_RS})
    )
    with pytest.raises(MungeBlocked):
        apply_munge(plan_munge(workspace), tmp_path)
    assert not (tmp_path / MUNGE_DIR).exists()


def test_a_dependency_that_is_not_an_unpacked_registry_source_blocks(tmp_path):
    """A path or git override has no registry source to copy, and copying somebody's working tree
    into `.cvlr_munge` and patching it is not something to do by inference."""
    package = CratePackage(
        name="anchor-lang",
        version="0.31.1",
        manifest_path=tmp_path / "elsewhere" / "Cargo.toml",
        lib=None,
        features=(),
        source=None,
    )
    plan = plan_munge(_workspace(tmp_path, package))
    assert plan.crates == ()
    assert "no src/error.rs" in plan.blocked[0].problem


def test_an_edit_that_does_not_match_is_an_error_not_a_skip(tmp_path):
    """The defect this whole module guards against. A patch that half-applies produces a build that
    looks munged and is not, and the failure it then produces is the original [3006] — which reads
    as "the munge does not work" rather than "the munge did not happen"."""
    workspace = _workspace(
        tmp_path,
        _crate(
            tmp_path,
            "anchor-lang",
            "0.31.1",
            {"src/error.rs": "// upstream rewrote this file entirely\n"},
        ),
    )
    plan = plan_munge(workspace)
    assert plan.crates, "planning cannot see inside the file, so this has to fail at apply time"
    with pytest.raises(MungeBlocked) as raised:
        apply_munge(plan, tmp_path)
    assert "expected 1 occurrence" in str(raised.value)


def test_an_edit_matching_more_than_declared_is_also_an_error(tmp_path):
    """The other direction, and the reason `occurrences` is a count rather than a flag: a pattern
    that starts matching twice after an upstream refactor would silently patch a second site."""
    patch = dataclasses.replace(
        ANCHOR_UNBOX,
        edits=(("src/error.rs", (Replacement("Box::new", "", occurrences=1),)),),
    )
    workspace = _workspace(
        tmp_path, _crate(tmp_path, "anchor-lang", "0.31.1", {"src/error.rs": ERROR_RS})
    )
    with pytest.raises(MungeBlocked, match="found 5"):
        apply_munge(plan_munge(workspace, (patch,)), tmp_path)


# ---------------------------------------------------------------------------------------------
# keeping the declaration honest


def test_the_patch_matches_the_real_crate_if_it_is_on_this_machine():
    """The stub above is what the tests read; this is what the world contains. Skips rather than
    fails when the crate is not in the local registry, since a machine without it is not a machine
    with a broken patch."""
    registries = sorted(Path.home().glob(".cargo/registry/src/*/anchor-lang-*"))
    covered = [
        d
        for d in registries
        if any(d.name.startswith(f"anchor-lang-{p}") for p in ANCHOR_UNBOX.applies_to)
    ]
    if not covered:
        pytest.skip("no anchor-lang source in the local cargo registry for a covered version")
    uncovered = sorted({d.name for d in registries} - {d.name for d in covered})
    for source in covered:
        text = (source / "src" / "error.rs").read_text()
        for relative, edits in ANCHOR_UNBOX.edits:
            assert relative == "src/error.rs"
            for edit in edits:
                assert text.count(edit.old) == edit.occurrences, (
                    f"{source.name}: {edit.old!r} appears {text.count(edit.old)} times, "
                    f"expected {edit.occurrences} — the patch is declared for this version and does "
                    f"not fit it"
                )
    # Not an assertion. Which versions are on a given machine is an accident, and an uncovered one
    # is a target that would block with its version named — the designed behaviour, not a gap.
    print(f"\npatch fits: {sorted({d.name for d in covered})}; not declared for: {uncovered}")


def test_the_munge_directory_is_gitignored():
    """It is a copy of somebody's dependency tree, derived from their lockfile plus this backend."""
    from composer.spec.cvlr.scaffold import GITIGNORE_LINES

    assert str(MUNGE_DIR) in GITIGNORE_LINES


def test_every_declared_patch_names_at_least_one_edit():
    for patch in SOLANA_PATCHES:
        assert patch.edits, f"{patch.crate} declares no edits"
        assert patch.why.strip(), f"{patch.crate} has no stated reason"
        assert patch.applies_to, f"{patch.crate} names no version line"
