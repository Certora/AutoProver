"""Resolving a real Cargo project with real cargo, from a bare workspace to one that compiles.

Everything else about preflight is checked against a hand-built ``Workspace`` with
``read_workspace`` monkeypatched out, which is the right way to pin what the planner *decides*. It
cannot pin what this does, because the three things most likely to be wrong here are all facts about
cargo rather than about us:

* which member owns a source file — cargo's answer, not a prefix match of ours;
* that the CVLR crates a run resolves are only visible *after* the scaffold has been applied and the
  graph re-read under the verification feature, from the package's own directory. Both halves of
  that were found by running it, and a fake graph answers whatever it was built to answer;
* that the result compiles at all.

So this is the one test that installs nothing of its own. The workspace has two members with library
targets, which is a workspace :func:`_pick_package` refuses on its own — so the selection can only
come from cargo's view of who owns ``main_source``, and a regression to a prefix match fails here.

Marked ``expensive``: it resolves and compiles a real dependency graph off the network. It skips,
naming what is missing, rather than failing when cargo is absent.
"""

import shutil
from pathlib import Path

import pytest

from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.preflight import gate_workspace, prepare_workspace, select_package

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

WORKSPACE_MANIFEST = """\
[workspace]
members = ["programs/vault", "programs/other"]
resolver = "2"
"""

MEMBER_MANIFEST = """\
[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "lib"]

[dependencies]
"""

PROGRAM = """\
pub fn deposit(amount: u64) -> u64 {
    amount
}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    (tmp_path / "Cargo.toml").write_text(WORKSPACE_MANIFEST)
    for name in ("vault", "other"):
        source = tmp_path / "programs" / name / "src"
        source.mkdir(parents=True)
        (source.parent / "Cargo.toml").write_text(MEMBER_MANIFEST.format(name=name))
        (source / "lib.rs").write_text(PROGRAM)
    return tmp_path


async def test_a_bare_cargo_workspace_becomes_one_that_compiles_with_a_harness_in(project):
    """The whole of what this slice can do, in the order a run does it.

    The compile gate is the assertion that matters: everything before it is a plan, and a plan that
    produces a project which does not build is worth nothing. It is also the only check that the
    scaffold's feature wiring, its manifest edits and the crate versions it pins are consistent with
    each other, since each of those is separately plausible and only rustc reads all three.
    """
    main_source = project / "programs" / "vault" / "src" / "lib.rs"

    selected = await select_package(project, None, main_source=main_source)
    assert selected.name == "vault"
    assert selected.package_dir == Path("programs/vault")

    pre = await prepare_workspace(selected.workspace_root, package=selected.name)
    assert pre.scaffold.blocked == (), pre.scaffold.blocked
    assert pre.artifact_stem == "vault"
    # Resolved from the scaffolded graph: before it, this project had no CVLR dependency at all.
    assert {crate.name for crate in pre.sources.crates} >= {"cvlr", "cvlr-solana"}
    assert (main_source.parent / "certora" / "specs" / "mod.rs").is_file()

    await gate_workspace(pre, sandbox=SandboxConfig(provider="none"))


async def test_pointing_it_at_a_project_it_already_scaffolded_is_a_read(project):
    """The scaffold never overwrites, which is what makes it safe to run against a project somebody
    else maintains — and what lets a run re-enter one it prepared earlier without a diff."""
    main_source = project / "programs" / "vault" / "src" / "lib.rs"
    selected = await select_package(project, None, main_source=main_source)

    first = await prepare_workspace(selected.workspace_root, package=selected.name)
    assert first.applied != ()
    authored = main_source.parent / "certora" / "specs" / "mod.rs"
    authored.write_text(f"{authored.read_text()}\n// an author's work\n")

    again = await prepare_workspace(selected.workspace_root, package=selected.name)

    assert again.applied == ()
    assert again.scaffold.changes == ()
    assert "an author's work" in authored.read_text()
