"""The one working tree, and the claim that it is derived rather than state.

Every unit of a run shares one copy of the project, which is safe because each unit's module is
behind its own cargo feature and only the staging unit's edits to the program are in it — and
*resumable* because everything in the tree that the run put there can be recomputed from the
checkpoint.

No cargo, no network, no prover.
"""

import tomllib
from pathlib import Path, PurePosixPath

import pytest
from graphcore.tools.vfs import MATERIALIZED_MANIFEST

from composer.cargo.metadata import CratePackage, LibTarget, RegistrySource, Workspace
from composer.layout import INTERNAL_DIR
from composer.sandbox.recipes import SANDBOX_CARGO_DIR

from composer.spec.cvlr.harness import CvlrArtifactStore, GeneratedHarness, HarnessModule
from composer.spec.cvlr.scaffold import declare_unit_features
from composer.spec.cvlr.tree import (
    NOT_PROJECT_SOURCE,
    Editable,
    NotInProject,
    NotProjectRelative,
    NotProjectSource,
    ProjectSource,
    SharedTree,
    UnitEdits,
)

_RESERVE = "programs/p/src/reserve.rs"
_SPECS = "programs/p/src/certora/specs"
_SOURCE_DIRS = ProjectSource(roots=(PurePosixPath("programs/p/src"),))

_SOURCE = """\
pub fn redeem_fees(reserve: &mut Reserve) -> Result<u64> {
    let amount = reserve.calculate_fees()?;
    Ok(amount)
}

pub fn calculate_fees(reserve: &Reserve) -> Result<u64> {
    Ok(0)
}
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "programs" / "p" / "src").mkdir(parents=True)
    (project / _RESERVE).write_text(_SOURCE)
    return project


async def _tree(tmp_path: Path) -> SharedTree:
    project = _project(tmp_path)
    tree = SharedTree(pristine=project, root=tmp_path / "work" / "build", source=_SOURCE_DIRS)
    await tree.materialize()
    return tree


def _early_panic_overlay(feature: str) -> dict[str, str]:
    """An overlay putting one gated attribute on ``redeem_fees``."""
    return {
        _RESERVE: _SOURCE.replace(
            "pub fn redeem_fees",
            f'#[cfg_attr(feature = "{feature}", cvlr::early_panic)]\npub fn redeem_fees',
        )
    }


def _edits(
    tree: SharedTree, unit: str, draft: str, overlay: dict[str, str] | None = None
) -> UnitEdits:
    return UnitEdits(
        module_path=tree.root / _SPECS / f"{unit}.rs",
        draft=draft,
        overlay=overlay or {},
    )


def _mtimes(tree: SharedTree, *paths: str) -> dict[str, int]:
    return {p: (tree.root / p).stat().st_mtime_ns for p in paths}


# ---------------------------------------------------------------------------------------------
# derived, not state


@pytest.mark.asyncio
async def test_deleting_the_tree_and_reconciling_reproduces_it(tmp_path: Path):
    """The disposability invariant, in miniature. The tree is a build cache: losing it costs a
    rebuild and never correctness, which is what makes a cache replay — where there is no tree at
    all — a resume rather than a fresh start."""
    tree = await _tree(tmp_path)
    edits = _edits(tree, "vault", "// draft\n", _early_panic_overlay("unit_vault"))
    await tree.reconcile("vault", edits)
    before = (tree.root / _RESERVE).read_text()

    import shutil

    shutil.rmtree(tree.root)
    fresh = SharedTree(pristine=tree.pristine, root=tree.root, source=_SOURCE_DIRS)
    await fresh.materialize()
    await fresh.reconcile("vault", edits)
    assert (fresh.root / _RESERVE).read_text() == before


@pytest.mark.asyncio
async def test_an_edit_dropped_from_state_leaves_the_file(tmp_path: Path):
    """A reverted edit or a rewound checkpoint must not leave a change on disk that nothing in state
    knows about. A path the overlay stops serving is restored from the project."""
    tree = await _tree(tmp_path)
    await tree.reconcile("vault", _edits(tree, "vault", "//\n", _early_panic_overlay("unit_vault")))
    assert "early_panic" in (tree.root / _RESERVE).read_text()

    await tree.reconcile("vault", _edits(tree, "vault", "//\n"))
    assert (tree.root / _RESERVE).read_text() == _SOURCE


@pytest.mark.asyncio
async def test_a_new_run_starts_from_the_project(tmp_path: Path):
    """Nothing a previous run put in the tree survives into the next: not its edits and drafts,
    which a resumed run rederives from state, and not its build output and cargo home, which a
    dependency's build script can write to and which a later run must not inherit."""
    tree = await _tree(tmp_path)
    await tree.reconcile("vault", _edits(tree, "vault", "// draft\n", _early_panic_overlay("unit_vault")))
    built = tree.root / "target" / "debug" / "x.rlib"
    cached = tree.root / SANDBOX_CARGO_DIR / "registry" / "src" / "anchor" / "lib.rs"
    for leftover in (built, cached):
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("from the previous run")

    await SharedTree(pristine=tree.pristine, root=tree.root, source=_SOURCE_DIRS).materialize()

    assert (tree.root / _RESERVE).read_text() == _SOURCE
    assert not (tree.root / f"{_SPECS}/vault.rs").exists()
    assert not built.exists()
    assert not cached.exists()


@pytest.mark.asyncio
async def test_a_new_run_sees_the_project_as_it_is_now(tmp_path: Path):
    """An edited program, a regenerated tuning file and a deleted file all reach the next run, so
    nothing is verified against a previous run's copy of the project."""
    tree = await _tree(tmp_path)
    envs = tree.pristine / "programs/p/src/certora/envs"
    envs.mkdir(parents=True)
    (envs / "cvlr_inlining.txt").write_text("#[inline(never)] ^old$\n")
    mutated = _SOURCE.replace("Ok(0)", "Ok(1)")
    (tree.pristine / _RESERVE).write_text(mutated)
    (tree.pristine / "Cargo.toml").write_text("[workspace]\n")
    await SharedTree(pristine=tree.pristine, root=tree.root, source=_SOURCE_DIRS).materialize()
    (tree.pristine / "Cargo.toml").unlink()

    await SharedTree(pristine=tree.pristine, root=tree.root, source=_SOURCE_DIRS).materialize()

    assert (tree.root / _RESERVE).read_text() == mutated
    assert (tree.root / "programs/p/src/certora/envs/cvlr_inlining.txt").is_file()
    assert not (tree.root / "Cargo.toml").exists()


def test_a_tree_that_overlaps_the_project_is_refused(tmp_path: Path):
    """Starting a run deletes the tree and copies the project into it. A tree that is the project or
    one of its ancestors would delete the developer's checkout; one inside the project would delete
    source sharing its path and be copied into itself."""
    project = _project(tmp_path)
    for root in (project, tmp_path):
        with pytest.raises(ValueError, match="is or contains the project"):
            SharedTree(pristine=project, root=root, source=_SOURCE_DIRS)
    for root in (project / "programs" / "p" / "extra", project / "target" / "build"):
        with pytest.raises(ValueError, match="inside the project"):
            SharedTree(pristine=project, root=root, source=_SOURCE_DIRS)


@pytest.mark.asyncio
async def test_re_reconciling_the_same_state_writes_nothing(tmp_path: Path):
    """Cargo fingerprints on mtime, so rewriting identical bytes is indistinguishable from an edit
    and costs a rebuild. This is what keeps a unit's next gate from recompiling what nothing
    changed."""
    tree = await _tree(tmp_path)
    edits = _edits(tree, "vault", "// draft\n", _early_panic_overlay("unit_vault"))
    await tree.reconcile("vault", edits)
    before = _mtimes(tree, _RESERVE, f"{_SPECS}/vault.rs")

    await tree.reconcile("vault", edits)
    assert _mtimes(tree, _RESERVE, f"{_SPECS}/vault.rs") == before


@pytest.mark.asyncio
async def test_no_scratch_file_is_left_behind(tmp_path: Path):
    """Derived files are replaced atomically, because the agents' source tools read the tree without
    the build permit. A leftover scratch file would also be collected as a source and uploaded."""
    tree = await _tree(tmp_path)
    await tree.reconcile("vault", _edits(tree, "vault", "//\n", _early_panic_overlay("unit_vault")))
    assert sorted(p.name for p in (tree.root / "programs" / "p" / "src").iterdir()) == [
        "certora",
        "reserve.rs",
    ]
    assert [p.name for p in (tree.root / _SPECS).iterdir()] == ["vault.rs"]


# ---------------------------------------------------------------------------------------------
# two units in one tree


@pytest.mark.asyncio
async def test_staging_swaps_one_units_edits_in_and_a_siblings_out(tmp_path: Path):
    """Two units' copies of one file have no union, and need none: every build holds the permit from
    staging to upload, so the tree only ever has to hold the building unit's copy."""
    tree = await _tree(tmp_path)
    await tree.reconcile("a", _edits(tree, "a", "//a\n", _early_panic_overlay("unit_a")))
    assert '"unit_a"' in (tree.root / _RESERVE).read_text()

    await tree.reconcile("b", _edits(tree, "b", "//b\n", _early_panic_overlay("unit_b")))
    text = (tree.root / _RESERVE).read_text()
    assert '"unit_b"' in text
    assert '"unit_a"' not in text


@pytest.mark.asyncio
async def test_a_sibling_that_edited_nothing_builds_the_project_as_shipped(tmp_path: Path):
    tree = await _tree(tmp_path)
    await tree.reconcile("a", _edits(tree, "a", "//a\n", _early_panic_overlay("unit_a")))
    await tree.reconcile("b", _edits(tree, "b", "//b\n"))
    assert (tree.root / _RESERVE).read_text() == _SOURCE


@pytest.mark.asyncio
async def test_a_swap_back_writes_the_same_bytes(tmp_path: Path):
    """Swapping costs a rebuild of the program crate, and only that: the unit's file comes back
    byte for byte, so its next build sees exactly the source of its last one."""
    tree = await _tree(tmp_path)
    a = _edits(tree, "a", "//a\n", _early_panic_overlay("unit_a"))
    await tree.reconcile("a", a)
    first = (tree.root / _RESERVE).read_bytes()
    await tree.reconcile("b", _edits(tree, "b", "//b\n", _early_panic_overlay("unit_b")))
    await tree.reconcile("a", a)
    assert (tree.root / _RESERVE).read_bytes() == first


@pytest.mark.asyncio
async def test_a_siblings_module_is_left_alone(tmp_path: Path):
    """It is `cfg`'d out of this build, so its contents cannot affect it — and rewriting it would
    dirty a file the sibling is the author of."""
    tree = await _tree(tmp_path)
    await tree.reconcile("a", _edits(tree, "a", "// a's draft\n"))
    await tree.reconcile("b", _edits(tree, "b", "// b's draft\n"))
    assert (tree.root / _SPECS / "a.rs").read_text() == "// a's draft\n"


# ---------------------------------------------------------------------------------------------
# what an overlay may write


@pytest.mark.asyncio
async def test_an_overlay_writing_a_dependency_is_refused(tmp_path: Path):
    """The editor's tools refused this when the edit was made, but the overlay now arrives from a
    checkpoint or the edit store. Confinement puts `CARGO_HOME` inside the tree, so containment
    alone would let a stage rewrite a dependency for every crate in the graph."""
    tree = await _tree(tmp_path)
    dependency = str(SANDBOX_CARGO_DIR / "registry/src/idx/anchor-lang-0.31.1/src/error.rs")
    with pytest.raises(ValueError, match="programs/p/src"):
        await tree.reconcile("a", _edits(tree, "a", "//\n", {dependency: "x"}))
    assert not (tree.root / dependency).exists()


@pytest.mark.asyncio
async def test_an_overlay_writing_a_harness_module_is_refused(tmp_path: Path):
    """The modules are the authors', and the tree overlays them from state."""
    tree = await _tree(tmp_path)
    await tree.reconcile("a", _edits(tree, "a", "// a\n"))
    with pytest.raises(ValueError, match="harness module"):
        await tree.reconcile("b", _edits(tree, "b", "//\n", {f"{_SPECS}/a.rs": "// b"}))
    assert (tree.root / _SPECS / "a.rs").read_text() == "// a\n"


@pytest.mark.asyncio
async def test_an_overlay_path_with_dotdot_is_refused(tmp_path: Path):
    """The overlay is keyed by the path that is written. ``foo/../a.rs`` is a different string, and
    removing that key unlinks ``a.rs``."""
    tree = await _tree(tmp_path)
    await tree.reconcile("a", _edits(tree, "a", "// a\n"))
    with pytest.raises(ValueError, match="not a project-relative path"):
        await tree.reconcile(
            "b",
            _edits(tree, "b", "// b\n", {f"{_SPECS}/foo/../a.rs": "// pwned\n"}),
        )
    await tree.reconcile("b", _edits(tree, "b", "// b2\n"))
    assert (tree.root / _SPECS / "a.rs").read_text() == "// a\n"
    assert not (tree.root / _SPECS / "foo").exists()


@pytest.mark.asyncio
async def test_two_spellings_of_one_file_are_refused(tmp_path: Path):
    tree = await _tree(tmp_path)
    with pytest.raises(ValueError, match="twice"):
        await tree.reconcile(
            "a",
            _edits(
                tree,
                "a",
                "//\n",
                {_RESERVE: "A\n", "programs/p/src/./reserve.rs": "B\n"},
            ),
        )
    assert (tree.root / _RESERVE).read_text() == _SOURCE


@pytest.mark.asyncio
async def test_an_edited_crlf_file_is_written_once(tmp_path: Path):
    """An overlay holds the file's text as the editor read it. What matters is that it compares equal
    from then on: a file that never did would be rewritten, and the crate rebuilt, on every gate."""
    project = _project(tmp_path)
    (project / _RESERVE).write_bytes(_SOURCE.replace("\n", "\r\n").encode())
    tree = SharedTree(pristine=project, root=tmp_path / "work" / "build", source=_SOURCE_DIRS)
    await tree.materialize()
    edits = _edits(tree, "a", "//\n", _early_panic_overlay("unit_a"))
    await tree.reconcile("a", edits)
    before = _mtimes(tree, _RESERVE)

    await tree.reconcile("a", edits)

    assert "early_panic" in (tree.root / _RESERVE).read_text()
    assert _mtimes(tree, _RESERVE) == before


# ---------------------------------------------------------------------------------------------
# what the run declares up front


def test_every_unit_module_is_gated_on_its_own_feature(tmp_path: Path):
    """Each unit's module is declared behind its own cargo feature.

    A module behind a disabled `cfg` is never compiled and never enters
    rustc's dep-info, so it cannot break or dirty another unit's build.
    """
    store = CvlrArtifactStore(tmp_path, Path("programs/p"))
    modules = [HarnessModule("deposits"), HarnessModule("admin_config")]
    mod_rs, *_ = store.declare_modules(modules)

    text = mod_rs.read_text()
    for module in modules:
        assert f'#[cfg(feature = "{module.feature}")]\npub mod {module.module};' in text


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://prover.certora.com/jobStatus/1/abc", "https://prover.certora.com/output/1/abc"),
        ("https://prover.certora.com/output/1/abc", "https://prover.certora.com/output/1/abc"),
        ("/tmp/prover-results", "/tmp/prover-results"),
        (None, None),
    ],
)
def test_the_reported_link_is_the_output_view(link: str | None, expected: str | None):
    harness = GeneratedHarness(commentary="", harness="", final_link=link)
    assert harness.output_link == expected


def test_a_units_feature_is_declared_and_empty(tmp_path: Path):
    """Declared, or `--features unit_x` fails with "Package does not contain this feature". Empty,
    or the dependencies' resolved feature set varies per unit and every unit builds its own
    dependency graph again — which is the entire cost the shared tree removes."""
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "p"\n\n[features]\ncertora = ["dep:cvlr"]\n')

    assert declare_unit_features(manifest, ["unit_a", "unit_b"]) == ("unit_a", "unit_b")
    text = manifest.read_text()
    assert "unit_a = []" in text and "unit_b = []" in text
    assert 'certora = ["dep:cvlr"]' in text


def test_declaring_a_feature_twice_changes_nothing(tmp_path: Path):
    """A resumed run declares again. Nothing this backend writes into somebody's manifest is
    written twice."""
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "p"\n\n[features]\ncertora = []\n')
    declare_unit_features(manifest, ["unit_a"])
    once = manifest.read_text()
    assert declare_unit_features(manifest, ["unit_a"]) == ()
    assert manifest.read_text() == once


def test_a_manifest_with_no_features_table_gets_one(tmp_path: Path):
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "p"\n')
    declare_unit_features(manifest, ["unit_a"])
    assert tomllib.loads(manifest.read_text())["features"] == {"unit_a": []}


# ---------------------------------------------------------------------------------------------
# what the tree is filled from


@pytest.mark.asyncio
async def test_build_output_and_tooling_are_not_copied_at_any_depth(tmp_path: Path):
    project = _project(tmp_path)
    for junk in ("node_modules/pkg/index.js", "programs/p/target/debug/x.rs", ".git/HEAD"):
        (project / junk).parent.mkdir(parents=True, exist_ok=True)
        (project / junk).write_text("x")

    tree = SharedTree(pristine=project, root=tmp_path / "work" / "build", source=_SOURCE_DIRS)
    await tree.materialize()

    assert sorted(p.name for p in tree.root.iterdir()) == [MATERIALIZED_MANIFEST, "programs"]
    assert not (tree.root / "programs/p/target").exists()


# ---------------------------------------------------------------------------------------------
# what reconcile refuses


@pytest.mark.asyncio
async def test_a_module_path_outside_the_tree_is_refused(tmp_path: Path):
    tree = await _tree(tmp_path)
    outside = UnitEdits(module_path=tmp_path / "elsewhere.rs", draft="//\n")
    with pytest.raises(ValueError, match="outside the working tree"):
        await tree.reconcile("a", outside)
    assert not (tmp_path / "elsewhere.rs").exists()


@pytest.mark.asyncio
async def test_two_units_naming_one_module_are_refused(tmp_path: Path):
    """The tree has one file per module, so the second unit's draft would silently replace the
    first's and both would report on one body of work."""
    tree = await _tree(tmp_path)
    await tree.reconcile("a", _edits(tree, "shared", "// a\n"))
    with pytest.raises(ValueError, match="both name"):
        await tree.reconcile("b", _edits(tree, "shared", "// b\n"))
    assert (tree.root / _SPECS / "shared.rs").read_text() == "// a\n"


# ---------------------------------------------------------------------------------------------
# which paths an edit may write


def test_a_path_outside_the_project_is_refused(tmp_path: Path):
    project = _project(tmp_path)
    tree = SharedTree(pristine=project, root=tmp_path / "work" / "build", source=_SOURCE_DIRS)
    assert tree.check_edit(_RESERVE) == Editable(_RESERVE)
    assert isinstance(tree.check_edit("../outside.rs"), NotInProject)
    assert isinstance(tree.check_edit("/etc/passwd"), NotInProject)
    assert isinstance(tree.check_edit("programs/p/src/foo/../reserve.rs"), NotProjectRelative)


def test_a_dependency_inside_the_tree_is_not_project_source(tmp_path: Path):
    """Containment is not the question, and this is the case that shows why. Confinement gives the
    run a private ``CARGO_HOME`` under ``<tree>/.certora_internal``, so every dependency's unpacked
    source sits inside the tree. A check that stopped at "is it in the tree" would let an edit
    rewrite Anchor for every crate in the graph."""
    tree = SharedTree(
        pristine=_project(tmp_path), root=tmp_path / "work" / "build", source=_SOURCE_DIRS
    )

    anchor = str(SANDBOX_CARGO_DIR / "registry/src/index.crates.io-6f17d22/anchor-lang-0.31.1/src/error.rs")
    assert isinstance(tree.check_edit(anchor), NotProjectSource)

    for built in (
        "target/debug/build/x/out/gen.rs",
        "programs/p/target/debug/x.rs",
        "node_modules/pkg/index.rs",
        "certora_out/y.rs",
    ):
        assert isinstance(tree.check_edit(built), NotProjectSource), built


def test_a_local_crates_manifest_may_be_edited(tmp_path: Path):
    """An edit inside a local dependency gates on a feature that crate does not declare, so the same
    edit has to declare it there and forward it from the program's manifest."""
    source = ProjectSource(
        roots=(PurePosixPath("programs/p/src"),),
        manifests=(PurePosixPath("programs/p/Cargo.toml"), PurePosixPath("crates/m/Cargo.toml")),
    )
    assert source.may_edit("crates/m/Cargo.toml")
    assert not source.contains("crates/m/Cargo.toml")
    assert not source.may_edit("Cargo.toml")
    assert not source.may_edit("crates/m/build.rs")


def test_build_output_inside_a_source_directory_is_not_project_source():
    """A crate whose root is at its package root makes the whole package its source directory, and
    at the workspace root that directory holds the run's ``CARGO_HOME`` and cargo's ``target``."""
    import re

    from composer.layout import RUST_BUILD_DIRS
    from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ

    at_root = ProjectSource(roots=(PurePosixPath("."),))
    assert at_root.contains("lib.rs")
    assert not at_root.contains(str(SANDBOX_CARGO_DIR / "registry/src/x/anchor/src/lib.rs"))
    for directory in RUST_BUILD_DIRS:
        path = f"{directory}/x.rs"
        assert re.fullmatch(RUST_FORBIDDEN_READ, path), directory
        assert not at_root.contains(path), directory
    assert INTERNAL_DIR.name in NOT_PROJECT_SOURCE


def test_only_the_project_crates_sources_are_project_source():
    """An allowlist: a path is source only by being in a crate's source directory, so a directory
    nobody thought to name — a vendored crate, a script, a relocated cargo home — is refused."""
    assert _SOURCE_DIRS.contains("programs/p/src/lib.rs")
    assert _SOURCE_DIRS.contains("programs/p/src/state/reserve.rs")
    for elsewhere in (
        "vendor/anchor-lang/src/lib.rs",
        "programs/p/build.rs",
        "programs/p/tests/t.rs",
        "programs/q/src/lib.rs",
        "cargo-home/registry/src/x/anchor/src/lib.rs",
        "programs/p/src/../../programs/q/src/lib.rs",
        "programs/p/src/foo/../lib.rs",
        "",
    ):
        assert not _SOURCE_DIRS.contains(elsewhere), elsewhere


def _package(manifest: Path, *, lib: Path | None, registry: bool = False) -> CratePackage:
    return CratePackage(
        name=manifest.parent.name,
        version="0.1.0",
        manifest_path=manifest,
        lib=None if lib is None else LibTarget(name="x", src_path=lib, crate_types=("lib",)),
        features=(),
        source=RegistrySource("registry+https://github.com/rust-lang/crates.io-index")
        if registry
        else None,
    )


def test_project_source_is_read_off_the_workspace(tmp_path: Path):
    """Local libraries count, wherever their crate root is. A fetched crate does not, even unpacked
    inside the project — which is where confinement puts it — and neither does a package with no
    library or one outside the project."""
    project = tmp_path / "project"
    cargo_home = project / SANDBOX_CARGO_DIR
    packages = (
        _package(project / "programs/p/Cargo.toml", lib=project / "programs/p/src/lib.rs"),
        _package(project / "crates/m/Cargo.toml", lib=project / "crates/m/lib/mod.rs"),
        _package(project / "cli/Cargo.toml", lib=None),
        _package(tmp_path / "sibling/Cargo.toml", lib=tmp_path / "sibling/src/lib.rs"),
        _package(
            cargo_home / "registry/src/i/anchor/Cargo.toml",
            lib=cargo_home / "registry/src/i/anchor/src/lib.rs",
            registry=True,
        ),
    )
    workspace = Workspace(
        root=project, target_directory=project / "target", members=packages[:3], packages=packages
    )

    source = ProjectSource.of(workspace, project)

    assert source.roots == (PurePosixPath("crates/m/lib"), PurePosixPath("programs/p/src"))
    assert source.manifests == (
        PurePosixPath("crates/m/Cargo.toml"), PurePosixPath("programs/p/Cargo.toml")
    )
