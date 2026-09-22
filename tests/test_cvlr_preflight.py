"""Resolving a real Cargo project with real cargo, from a bare workspace to one that compiles.

The other preflight tests build a ``Workspace`` by hand and patch out ``read_workspace``. That
pins what the planner decides. It cannot pin what cargo decides:

* which member owns a source file, which is cargo's answer, not a prefix match;
* that the CVLR crates are visible only after the scaffold is applied and the graph is re-read
  under the verification feature, from the package directory. A fake graph answers whatever it
  was built to answer;
* that the result compiles.

The workspace has two members with library targets. :func:`_pick_package` refuses that on its
own, so the selection has to come from cargo's view of who owns ``main_source``. A prefix match
fails here.

Marked ``expensive``: it resolves and compiles a real dependency graph off the network. It skips,
naming what is missing, when cargo is absent.
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
    """Select a package, scaffold it, and compile it, in the order a run does.

    The compile is the check that the feature wiring, the manifest edits, and the pinned crate
    versions agree. Each of those can look right on its own. Only rustc reads all three.
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


async def test_pointing_it_at_a_project_it_already_scaffolded_replaces_only_the_edited_harness(
    project,
):
    """The harness files are AutoProver's. A second run puts an edited one back and changes
    nothing else, so the manifests it already extended are not extended again."""
    main_source = project / "programs" / "vault" / "src" / "lib.rs"
    selected = await select_package(project, None, main_source=main_source)

    first = await prepare_workspace(selected.workspace_root, package=selected.name)
    assert first.applied != ()
    specs = main_source.parent / "certora" / "specs" / "mod.rs"
    scaffolded = specs.read_text()
    specs.write_text(f"{scaffolded}\n// an author's work\n")

    again = await prepare_workspace(selected.workspace_root, package=selected.name)

    assert again.applied == (specs.relative_to(selected.workspace_root),)
    assert specs.read_text() == scaffolded
