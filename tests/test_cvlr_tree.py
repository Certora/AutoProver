"""The one working tree, and the claim that it is derived rather than state.

``docs/single-working-tree.md``. Every unit of a run shares one copy of the project, which is safe
because each unit's module is behind its own cargo feature — and *resumable* because everything in
the tree that the run put there can be recomputed from the checkpoint. These are the cheap half of
§8's checks; the two that need a real cargo (dependency fingerprints must not vary with a unit
feature, and a third build of an already-built feature set must be a no-op) belong to the expensive
gate.

No cargo, no network, no prover.
"""

import asyncio
from pathlib import Path

import pytest

from composer.layout import INTERNAL_DIR
from composer.sandbox.recipes import SANDBOX_CARGO_DIR

from composer.spec.cvlr.harness import CvlrArtifactStore, HarnessModule
from composer.spec.cvlr.munge import EarlyPanic, FunctionMunge, MockFn
from composer.spec.cvlr.scaffold import declare_unit_features
from composer.spec.cvlr.tree import SharedTree, UnitEdits, munge_diff

_RESERVE = "programs/p/src/reserve.rs"

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


def _tree(tmp_path: Path) -> SharedTree:
    project = _project(tmp_path)
    tree = SharedTree(pristine=project, root=tmp_path / "work" / "build")
    tree.materialize()
    return tree


def _munge(function: str, feature: str, kind=None) -> FunctionMunge:
    return FunctionMunge(
        path=_RESERVE,
        function=function,
        kind=kind or EarlyPanic(),
        why="[3308] on the `?` path",
        feature=feature,
    )


def _edits(tree: SharedTree, unit: str, draft: str, *munges: FunctionMunge) -> UnitEdits:
    return UnitEdits(
        module_path=tree.root / "src" / "certora" / "specs" / f"{unit}.rs",
        draft=draft,
        munges=munges,
    )


# ---------------------------------------------------------------------------------------------
# derived, not state


def test_deleting_the_tree_and_reconciling_reproduces_it(tmp_path: Path):
    """§8's disposability invariant, in miniature. The tree is a build cache: losing it costs a
    rebuild and never correctness, which is what makes a cache replay — where there is no tree at
    all — a resume rather than a fresh start."""
    tree = _tree(tmp_path)
    edits = _edits(tree, "vault", "// draft\n", _munge("redeem_fees", "unit_vault"))
    tree.reconcile("vault", edits)
    before = (tree.root / _RESERVE).read_text()

    import shutil

    shutil.rmtree(tree.root)
    fresh = SharedTree(pristine=tree.pristine, root=tree.root)
    fresh.materialize()
    fresh.reconcile("vault", edits)
    assert (fresh.root / _RESERVE).read_text() == before


def test_a_munge_dropped_from_state_leaves_the_file(tmp_path: Path):
    """The bug the old arrangement could not have: `stage` edited in place and never removed, so a
    rewound checkpoint silently kept an attribute nothing in state knew about. Rebuilding each
    munged file from the pristine copy is what makes rewinding mean something."""
    tree = _tree(tmp_path)
    tree.reconcile("vault", _edits(tree, "vault", "//\n", _munge("redeem_fees", "unit_vault")))
    assert "early_panic" in (tree.root / _RESERVE).read_text()

    tree.reconcile("vault", _edits(tree, "vault", "//\n"))
    assert (tree.root / _RESERVE).read_text() == _SOURCE


def test_a_later_session_restores_a_file_whose_munge_is_gone(tmp_path: Path):
    """The same removal, across a resume. The tree is reused, so the attribute is still on disk, and
    a session that starts with an empty munge set has nothing in state naming the file — which is
    exactly when it most needs restoring. The tree's own note of what it derived is what closes it.
    """
    tree = _tree(tmp_path)
    tree.reconcile("vault", _edits(tree, "vault", "//\n", _munge("redeem_fees", "unit_vault")))
    assert "early_panic" in (tree.root / _RESERVE).read_text()

    resumed = SharedTree(pristine=tree.pristine, root=tree.root)
    resumed.materialize()
    resumed.reconcile("vault", _edits(resumed, "vault", "//\n"))
    assert (tree.root / _RESERVE).read_text() == _SOURCE


def test_the_derived_note_is_not_a_source_of_truth(tmp_path: Path):
    """Its worst case has to be a redundant restore, never a wrong one: a corrupt or absent note
    must not stop a reconcile."""
    from composer.spec.cvlr.tree import DERIVED_MANIFEST

    tree = _tree(tmp_path)
    tree.reconcile("vault", _edits(tree, "vault", "//\n", _munge("redeem_fees", "unit_vault")))
    (tree.root / DERIVED_MANIFEST).write_text("{ not json")

    resumed = SharedTree(pristine=tree.pristine, root=tree.root)
    resumed.materialize()
    result = resumed.reconcile("vault", _edits(resumed, "vault", "//\n", _munge("redeem_fees", "unit_vault")))
    assert result
    assert "early_panic" in (tree.root / _RESERVE).read_text()


def test_re_reconciling_the_same_state_writes_nothing(tmp_path: Path):
    """Cargo fingerprints on mtime, so rewriting identical bytes is indistinguishable from an edit
    and costs a full rebuild. This is what makes "resume and change nothing" an incremental build."""
    tree = _tree(tmp_path)
    edits = _edits(tree, "vault", "// draft\n", _munge("redeem_fees", "unit_vault"))
    assert tree.reconcile("vault", edits).written
    assert tree.reconcile("vault", edits).written == ()


def test_no_scratch_file_is_left_behind(tmp_path: Path):
    """Derived files are replaced atomically, because the build permit does not cover the prover's
    own rerun of the build script — so another unit's build can be reading the file. A leftover
    temp file would also be collected as a source and uploaded."""
    tree = _tree(tmp_path)
    tree.reconcile("vault", _edits(tree, "vault", "//\n", _munge("redeem_fees", "unit_vault")))
    assert [p.name for p in (tree.root / "programs" / "p" / "src").iterdir()] == ["reserve.rs"]


# ---------------------------------------------------------------------------------------------
# two units in one tree


def test_the_union_of_both_units_munges_is_replayed(tmp_path: Path):
    """Replaying only the staging unit's would delete its sibling's line, and the sibling would add
    it back on its next gate — churning the file and rebuilding the crate for nothing."""
    tree = _tree(tmp_path)
    tree.reconcile("a", _edits(tree, "a", "//a\n", _munge("redeem_fees", "unit_a")))
    tree.reconcile("b", _edits(tree, "b", "//b\n", _munge("calculate_fees", "unit_b")))

    text = (tree.root / _RESERVE).read_text()
    assert '#[cfg_attr(feature = "unit_a", cvlr::early_panic)]' in text
    assert '#[cfg_attr(feature = "unit_b", cvlr::early_panic)]' in text


def test_two_units_munging_one_function_are_two_dormant_lines(tmp_path: Path):
    """The reason the tree can be shared at all. Each attribute is gated on the recording unit's own
    feature, so neither is in effect in the other's build — and neither is a conflict."""
    tree = _tree(tmp_path)
    tree.reconcile("a", _edits(tree, "a", "//a\n", _munge("redeem_fees", "unit_a")))
    tree.reconcile(
        "b",
        _edits(
            tree,
            "b",
            "//b\n",
            _munge("redeem_fees", "unit_b", MockFn(stand_in="crate::certora::mocks::f")),
        ),
    )
    text = (tree.root / _RESERVE).read_text()
    assert '#[cfg_attr(feature = "unit_a", cvlr::early_panic)]' in text
    assert 'feature = "unit_b", cvlr::mock_fn(with = crate::certora::mocks::f)' in text


def test_the_replay_order_does_not_depend_on_who_staged_first(tmp_path: Path):
    """Bytes that depended on scheduling would make the crate's fingerprint depend on it too, and
    two runs of the same state would rebuild for no reason anybody could see."""
    a = _munge("redeem_fees", "unit_a")
    b = _munge("redeem_fees", "unit_b")

    one = _tree(tmp_path / "one")
    one.reconcile("a", _edits(one, "a", "//\n", a))
    one.reconcile("b", _edits(one, "b", "//\n", b))

    two = _tree(tmp_path / "two")
    two.reconcile("b", _edits(two, "b", "//\n", b))
    two.reconcile("a", _edits(two, "a", "//\n", a))

    assert (one.root / _RESERVE).read_text() == (two.root / _RESERVE).read_text()


def test_a_siblings_module_is_left_alone(tmp_path: Path):
    """It is `cfg`'d out of this build, so its contents cannot affect it — and rewriting it would
    dirty a file the sibling is the author of."""
    tree = _tree(tmp_path)
    tree.reconcile("a", _edits(tree, "a", "// a's draft\n"))
    tree.reconcile("b", _edits(tree, "b", "// b's draft\n"))
    assert (tree.root / "src/certora/specs/a.rs").read_text() == "// a's draft\n"


# ---------------------------------------------------------------------------------------------
# drift


def test_a_munge_onto_moved_source_is_reported_rather_than_lost(tmp_path: Path):
    """Replay happens against the pristine project, which can move between the run that recorded a
    munge and the run that replays it. The failure is otherwise mute: the build succeeds, the report
    still carries a source-edit record, and the property was checked against code the record does
    not describe."""
    tree = _tree(tmp_path)
    result = tree.reconcile("a", _edits(tree, "a", "//\n", _munge("gone_away", "unit_a")))
    assert not result
    (drift,) = result.drifted
    assert drift.munge.function == "gone_away"
    assert "gone_away" in drift.describe()


def test_a_munge_of_something_that_is_not_project_source_is_refused(tmp_path: Path):
    """The tool checked this when it recorded the munge, but the record now arrives from a
    checkpoint. Confinement puts `CARGO_HOME` inside the tree, so containment alone would let a
    replay rewrite a dependency for every crate in the graph."""
    tree = _tree(tmp_path)
    dependency = str(SANDBOX_CARGO_DIR / "registry/src/idx/anchor-lang-0.31.1/src/error.rs")
    munge = FunctionMunge(
        path=dependency, function="f", kind=EarlyPanic(), why="w", feature="unit_a"
    )
    result = tree.reconcile("a", _edits(tree, "a", "//\n", munge))
    assert not result
    assert INTERNAL_DIR.name in result.drifted[0].describe()


# ---------------------------------------------------------------------------------------------
# the report's diff, without a tree


def test_the_diff_is_computed_from_state_with_no_tree(tmp_path: Path):
    """A report is the last thing in a run and the tree is disposable by design, so a diff that
    needed one would be the thing that stopped it being disposable."""
    project = _project(tmp_path)
    diff = munge_diff(project, (_munge("redeem_fees", "unit_a"),))
    assert '+#[cfg_attr(feature = "unit_a", cvlr::early_panic)]' in diff
    assert f"a/{_RESERVE}" in diff


def test_one_units_diff_does_not_show_a_siblings_dormant_lines(tmp_path: Path):
    """Reading the diff off the tree would show every unit's munges in every unit's report, and a
    `SourceEditRecord` is a claim about what *this* component's outcomes were earned against."""
    project = _project(tmp_path)
    diff = munge_diff(project, (_munge("redeem_fees", "unit_a"),))
    assert "unit_b" not in diff


# ---------------------------------------------------------------------------------------------
# what the run declares up front


def test_every_unit_module_is_gated_on_its_own_feature(tmp_path: Path):
    """The whole basis of the shared tree: a module behind a disabled `cfg` is never compiled, never
    enters rustc's dep-info, and so cannot break or dirty another unit's build."""
    store = CvlrArtifactStore(tmp_path, Path("programs/p"))
    modules = [HarnessModule("deposits"), HarnessModule("admin-config")]
    mod_rs, *_ = store.declare_modules(modules)

    text = mod_rs.read_text()
    for module in modules:
        assert f'#[cfg(feature = "{module.feature}")]\npub mod {module.module};' in text


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
    assert "[features]\nunit_a = []" in manifest.read_text()


def test_a_reused_tree_picks_up_a_changed_unit_set(tmp_path: Path):
    """`mod.rs` and the manifest are written into the *project* before the tree is copied, because
    they are deliverables and a function of the job list rather than of any unit's state. A resumed
    run whose components changed would otherwise build against the previous run's copy of both — a
    manifest missing a unit's feature, and a `mod.rs` missing its module."""
    project = _project(tmp_path)
    (project / "src" / "certora" / "specs").mkdir(parents=True)
    mod_rs = Path("src/certora/specs/mod.rs")
    (project / mod_rs).write_text('#[cfg(feature = "unit_a")]\npub mod a;\n')

    tree = SharedTree(pristine=project, root=tmp_path / "work" / "build")
    tree.materialize()
    assert "unit_b" not in (tree.root / mod_rs).read_text()

    (project / mod_rs).write_text(
        '#[cfg(feature = "unit_a")]\npub mod a;\n#[cfg(feature = "unit_b")]\npub mod b;\n'
    )
    resumed = SharedTree(pristine=project, root=tree.root)
    resumed.materialize()
    assert resumed.adopt(mod_rs) == (str(mod_rs),)
    assert "unit_b" in (tree.root / mod_rs).read_text()
    # And re-adopting an unchanged file does not dirty the crate.
    assert resumed.adopt(mod_rs) == ()


def test_the_gate_and_the_submission_ask_for_the_same_two_features(tmp_path: Path):
    """A build with only the unit feature compiles no harness at all — `mod certora` is gated on
    `certora` — and a conf naming rules that do not exist ends in FAILED with nothing on disk. One
    property pairs them so no call site can get it half right."""
    import contextlib
    from types import SimpleNamespace

    from composer.spec.cvlr.conf import DEFAULT_FEATURE
    from composer.spec.cvlr.verify import HarnessTarget

    unit = HarnessModule("deposits")
    target = HarnessTarget(
        session=SimpleNamespace(workdir=tmp_path),  # type: ignore[arg-type]
        module_path=tmp_path / "m.rs",
        package="p",
        tuning=SimpleNamespace(),  # type: ignore[arg-type]
        unit=unit,
        tree=SharedTree(pristine=tmp_path, root=tmp_path),
        build_sem=asyncio.Semaphore(1),
    )
    assert target.features == (DEFAULT_FEATURE, unit.feature)
    assert contextlib.nullcontext  # the slot is exercised in test_the_build_permit_serializes


@pytest.mark.asyncio
async def test_the_build_permit_serializes_staging_and_the_build(tmp_path: Path):
    """One permit for the run. Cargo would serialize concurrent builds on its own build-directory
    lock anyway; what the permit adds is a queue the host can see rather than a silent stall."""
    from types import SimpleNamespace

    from composer.spec.cvlr.verify import HarnessTarget

    sem = asyncio.Semaphore(1)
    target = HarnessTarget(
        session=SimpleNamespace(workdir=tmp_path),  # type: ignore[arg-type]
        module_path=tmp_path / "m.rs",
        package="p",
        tuning=SimpleNamespace(),  # type: ignore[arg-type]
        unit=HarnessModule("d"),
        tree=SharedTree(pristine=tmp_path, root=tmp_path),
        build_sem=sem,
    )
    async with target.build_slot():
        assert sem.locked()
    assert not sem.locked()
