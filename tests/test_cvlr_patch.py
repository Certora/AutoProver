"""The harness patch, and the per-component patch that adds one unit's edits to it."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.spec.cvlr.patch import (
    BASELINE_DIR,
    COMPONENTS_DIR,
    PATCH_PATH,
    snapshot,
    write_component_patches,
    write_patch,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _project(root: Path) -> Path:
    (root / "programs" / "p" / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["programs/*"]\n')
    (root / "Cargo.lock").write_text("# lock v1\n")
    (root / "programs" / "p" / "Cargo.toml").write_text('[package]\nname = "p"\n')
    (root / "programs" / "p" / "src" / "lib.rs").write_text("pub fn f() {}")
    return root


def _run(root: Path) -> None:
    """What a run leaves behind: manifest and lib.rs edits, a new harness module, and output that is
    not source."""
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["programs/*"]\n\n[patch]\n')
    (root / "Cargo.lock").write_text("# lock v2\n")
    (root / "programs" / "p" / "src" / "lib.rs").write_text(
        'pub fn f() {}\n#[cfg(feature = "certora")]\nmod certora;\n'
    )
    harness = root / "programs" / "p" / "src" / "certora"
    harness.mkdir()
    (harness / "mod.rs").write_text("pub mod specs;\n")
    (root / "target").mkdir()
    (root / "target" / "out.txt").write_text("built\n")
    (root / "certora" / "cvlr" / "reports").mkdir(parents=True)
    (root / "certora" / "cvlr" / "reports" / "report.json").write_text("{}\n")


def test_the_patch_carries_the_harness_and_its_wiring_and_nothing_else(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    _run(root)

    assert write_patch(root) == PATCH_PATH
    patch = (root / PATCH_PATH).read_text()
    touched = {line[len("+++ b/"):] for line in patch.splitlines() if line.startswith("+++ b/")}
    assert touched == {
        "Cargo.toml",
        "programs/p/src/lib.rs",
        "programs/p/src/certora/mod.rs",
    }


@needs_git
def test_the_patch_applies_to_the_project_as_shipped(tmp_path):
    """Including the edit to a file that had no final newline, which a diff has to mark for
    ``git apply`` to accept it."""
    root = _project(tmp_path / "proj")
    shipped = _project(tmp_path / "shipped")
    snapshot(root)
    _run(root)
    write_patch(root)

    subprocess.run(
        ["git", "apply", str(root / PATCH_PATH)], cwd=shipped, check=True, capture_output=True
    )
    for relative in ("Cargo.toml", "programs/p/src/lib.rs", "programs/p/src/certora/mod.rs"):
        assert (shipped / relative).read_text() == (root / relative).read_text()


def test_a_resumed_run_keeps_the_first_baseline(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    _run(root)
    snapshot(root)

    assert (root / BASELINE_DIR / "programs" / "p" / "src" / "lib.rs").read_text() == "pub fn f() {}"


def test_a_run_that_changed_nothing_leaves_no_patch_not_an_old_one(tmp_path):
    root = _project(tmp_path / "proj")
    (root / PATCH_PATH).parent.mkdir(parents=True)
    (root / PATCH_PATH).write_text("stale\n")
    snapshot(root)

    assert write_patch(root) is None
    assert not (root / PATCH_PATH).exists()


def test_without_a_baseline_there_is_no_patch(tmp_path):
    root = _project(tmp_path / "proj")
    _run(root)
    assert write_patch(root) is None


@pytest.mark.asyncio
async def test_the_formalizer_names_the_patch_it_wrote_for_the_report(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    _run(root)
    formalizer = _formalizer(root)
    assert formalizer.deliverable_patch() is None

    run = SimpleNamespace(source=SimpleNamespace(project_root=str(root)))
    await formalizer.finalize([], run)  # type: ignore[arg-type]

    assert formalizer.deliverable_patch() == PATCH_PATH.as_posix()
    assert (root / PATCH_PATH).is_file()


def _formalizer(root: Path):
    """A formalizer whose ``deps`` hold only what :meth:`finalize` reads. The package is the root."""
    from composer.spec.cvlr.env_paths import PathDialect
    from composer.spec.cvlr.harness import CvlrArtifactStore
    from composer.spec.cvlr.pipeline import CvlrFormalizer

    deps = SimpleNamespace(
        store=CvlrArtifactStore(root, Path(".")),
        package_dir=Path("."),
        preflight=SimpleNamespace(package="p", scaffold=SimpleNamespace(dialect=PathDialect())),
    )
    return CvlrFormalizer(None, None, deps=deps, build=None)  # type: ignore[arg-type]


_CARGO = '[package]\nname = "p"\n'


def _component_project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO)
    (root / "src" / "lib.rs").write_text("fn f() {}\n")
    (root / "src" / "processor.rs").write_text("fn process() {}\n")
    return root


def _component_harness(root: Path) -> None:
    (root / "Cargo.toml").write_text(
        _CARGO + "\n[features]\ncertora = []\nunit_vault = []\nunit_fees = []\n"
    )
    (root / "src" / "lib.rs").write_text('fn f() {}\n#[cfg(feature = "certora")]\nmod certora;\n')
    spec = root / "src" / "certora"
    spec.mkdir()
    (spec / "mod.rs").write_text("pub mod vault;\n")


def test_a_component_patch_carries_the_harness_and_that_units_edits_only(tmp_path):
    """Files the harness and the edit share come from the edit, which already contains the wiring.
    A path the harness patch refuses stays out, and a patch from an earlier run does not linger."""
    root = _component_project(tmp_path / "proj")
    snapshot(root)
    _component_harness(root)
    vault_cargo = (root / "Cargo.toml").read_text().replace(
        "unit_vault = []", 'unit_vault = ["dep"]'
    )
    (root / COMPONENTS_DIR).mkdir(parents=True)
    (root / COMPONENTS_DIR / "gone.patch").write_text("stale\n")

    written = write_component_patches(
        root,
        {
            "vault": {
                "Cargo.toml": vault_cargo,
                "src/processor.rs": '#[cfg(feature = "unit_vault")]\nfn process() {}\n',
                "../outside.txt": "nope\n",
                "Cargo.lock": "not carried\n",
            },
            "fees": {"src/processor.rs": '#[cfg(feature = "unit_fees")]\nfn process() {}\n'},
        },
    )

    assert set(written) == {"vault", "fees"}
    assert not (root / COMPONENTS_DIR / "gone.patch").exists()
    vault = (root / written["vault"]).read_text()
    fees = (root / written["fees"]).read_text()
    assert "mod certora" in vault and 'unit_vault = ["dep"]' in vault
    assert "outside" not in vault and "not carried" not in vault
    assert "unit_fees" in fees and 'unit_vault = ["dep"]' not in fees
    harness = write_patch(root)
    assert harness is not None
    assert "processor.rs" not in (root / harness).read_text()


@needs_git
def test_a_component_patch_applies_to_the_clean_project_and_not_onto_another(tmp_path):
    root = _component_project(tmp_path / "proj")
    snapshot(root)
    _component_harness(root)
    vault_cargo = (root / "Cargo.toml").read_text().replace(
        "unit_vault = []", 'unit_vault = ["dep"]'
    )
    written = write_component_patches(
        root,
        {
            "vault": {
                "Cargo.toml": vault_cargo,
                "src/processor.rs": '#[cfg(feature = "unit_vault")]\nfn process() {}\n',
            },
            "fees": {"src/processor.rs": '#[cfg(feature = "unit_fees")]\nfn process() {}\n'},
        },
    )

    vault_tree = _component_project(tmp_path / "vault")
    fees_tree = _component_project(tmp_path / "fees")
    subprocess.run(
        ["git", "apply", str(root / written["vault"])], cwd=vault_tree, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "apply", str(root / written["fees"])], cwd=fees_tree, check=True, capture_output=True
    )

    assert (vault_tree / "Cargo.toml").read_text() == vault_cargo
    assert (vault_tree / "src" / "processor.rs").read_text().startswith(
        '#[cfg(feature = "unit_vault")]'
    )
    assert (vault_tree / "src" / "lib.rs").read_text() == (root / "src" / "lib.rs").read_text()
    assert (vault_tree / "src" / "certora" / "mod.rs").is_file()
    assert (fees_tree / "Cargo.toml").read_text() == (root / "Cargo.toml").read_text()
    assert "unit_fees" in (fees_tree / "src" / "processor.rs").read_text()
    assert "unit_vault" not in (fees_tree / "src" / "processor.rs").read_text()

    both = _component_project(tmp_path / "both")
    subprocess.run(
        ["git", "apply", str(root / written["vault"])], cwd=both, check=True, capture_output=True
    )
    second = subprocess.run(
        ["git", "apply", "--check", str(root / written["fees"])], cwd=both, capture_output=True
    )
    assert second.returncode != 0


def test_without_a_baseline_no_component_patch_is_left_behind(tmp_path):
    root = _component_project(tmp_path / "proj")
    _component_harness(root)
    (root / COMPONENTS_DIR).mkdir(parents=True)
    (root / COMPONENTS_DIR / "vault.patch").write_text("stale\n")

    assert write_component_patches(root, {"vault": {"src/processor.rs": "fn x() {}\n"}}) == {}
    assert not (root / COMPONENTS_DIR).exists()


def test_a_component_patch_stem_is_a_module_name(tmp_path):
    root = _component_project(tmp_path / "proj")
    snapshot(root)
    with pytest.raises(ValueError):
        write_component_patches(root, {"../vault": {}})


@pytest.mark.asyncio
async def test_the_report_names_the_component_patch_finalize_wrote(tmp_path):
    """``cumulative_diff`` stays the edits against the tree the run left. ``patch_path`` is the file
    that applies to the project as shipped. A unit that edited nothing gets neither. A curtailed one
    gets both, in its appendix record rather than ``source_edits``."""
    from composer.pipeline.ptypes import Curtailed, Delivered
    from composer.spec.cvlr.harness import GeneratedHarness
    from composer.spec.source.munge.edit_store import AppliedEdit

    root = _component_project(tmp_path / "proj")
    snapshot(root)
    _component_harness(root)
    cargo = (root / "Cargo.toml").read_text().replace("unit_vault = []", 'unit_vault = ["dep"]')
    edited = GeneratedHarness(
        commentary="",
        harness="",
        applied_edits=[
            AppliedEdit(edit_id="e", executive_summary="forward the feature", why_sound="cfg")
        ],
        vfs={
            "Cargo.toml": cargo,
            "src/processor.rs": '#[cfg(feature = "unit_vault")]\nfn process() {}\n',
        },
    )
    run = SimpleNamespace(source=SimpleNamespace(project_root=str(root)))
    outcomes = [
        SimpleNamespace(
            feat=SimpleNamespace(slug="vault", display_name="Vault"),
            result=Delivered(result=edited, deliverable=Path("vault.rs")),
        ),
        SimpleNamespace(
            feat=SimpleNamespace(slug="fees", display_name="Fees"),
            result=Delivered(
                result=GeneratedHarness(commentary="", harness=""), deliverable=Path("fees.rs")
            ),
        ),
        SimpleNamespace(
            feat=SimpleNamespace(slug="cut", display_name="Cut"),
            result=Curtailed(Delivered(result=edited, deliverable=Path("cut.rs"))),
        ),
    ]
    formalizer = _formalizer(root)
    await formalizer.finalize(outcomes, run)  # type: ignore[arg-type]
    records = await formalizer.source_edits(outcomes, run)  # type: ignore[arg-type]

    assert [r.component for r in records] == ["Vault"]
    assert records[0].patch_path == (COMPONENTS_DIR / "vault.patch").as_posix()
    assert records[0].patch_path is not None
    assert (root / records[0].patch_path).is_file()
    assert 'unit_vault = ["dep"]' in records[0].cumulative_diff
    assert "mod certora" not in records[0].cumulative_diff
    assert not (root / COMPONENTS_DIR / "fees.patch").exists()

    partials = await formalizer.curtailed_source_edits(outcomes, run)  # type: ignore[arg-type]
    assert [r.component for r in partials] == ["Cut"]
    assert partials[0].patch_path == (COMPONENTS_DIR / "cut.patch").as_posix()
    assert (root / partials[0].patch_path).is_file()
    assert 'unit_vault = ["dep"]' in partials[0].cumulative_diff


@pytest.mark.asyncio
async def test_a_delivered_unit_gets_a_conf_and_the_summaries_it_names(tmp_path):
    """The conf builds with ``cargo certora-sbf`` from the package directory, and the summaries
    file it names is written into the project, so the harness patch carries it. A curtailed unit,
    and a result cached before settings were recorded, get no conf; an old conf is removed."""
    import json

    from composer.pipeline.ptypes import Curtailed, Delivered
    from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION, OptimisticLoop, TunableConf
    from composer.spec.cvlr.harness import CONFS_DIR, GeneratedHarness
    from composer.spec.cvlr.scaffold import ENVS_DIR
    from composer.spec.cvlr.tuning import SummaryDirective

    root = _component_project(tmp_path / "proj")
    snapshot(root)
    _component_harness(root)
    (root / CONFS_DIR).mkdir(parents=True)
    (root / CONFS_DIR / "gone.conf").write_text("{}\n")
    settings = TunableConf(loop_iter=4, optimistic_loop=OptimisticLoop(why="bounded by input"))
    summary = SummaryDirective(pattern="^<p::E as Display>::fmt$", why="[3308]")
    vault = GeneratedHarness(
        commentary="", harness="", declared_rules=["rule_b", "rule_a"], summaries=[summary],
        settings=settings,
    )
    outcomes = [
        SimpleNamespace(
            feat=SimpleNamespace(slug="vault", display_name="Vault & Fees"),
            result=Delivered(result=vault, deliverable=Path("vault.rs")),
        ),
        SimpleNamespace(
            feat=SimpleNamespace(slug="old", display_name="Old"),
            result=Delivered(
                result=GeneratedHarness(commentary="", harness=""), deliverable=Path("old.rs")
            ),
        ),
        SimpleNamespace(
            feat=SimpleNamespace(slug="cut", display_name="Cut"),
            result=Curtailed(Delivered(result=vault, deliverable=Path("cut.rs"))),
        ),
    ]
    formalizer = _formalizer(root)
    run = SimpleNamespace(source=SimpleNamespace(project_root=str(root)))
    await formalizer.finalize(outcomes, run)  # type: ignore[arg-type]

    assert formalizer.deliverable_confs() == {"Vault & Fees": (CONFS_DIR / "vault.conf").as_posix()}
    assert sorted(p.name for p in (root / CONFS_DIR).iterdir()) == ["vault.conf"]
    conf = json.loads((root / CONFS_DIR / "vault.conf").read_text())
    assert "build_script" not in conf
    assert conf["cargo_features"] == ["certora", "unit_vault"]
    assert conf["cargo_tools_version"] == PLATFORM_TOOLS_VERSION
    assert conf["loop_iter"] == "4" and conf["optimistic_loop"] is True
    assert conf["rule"] == ["rule_a", "rule_b"]
    assert "&" not in conf["msg"]
    (summaries,) = conf["solana_summaries"]
    assert Path(summaries).parent == ENVS_DIR
    assert summary.pattern in (root / summaries).read_text()
    harness = formalizer.deliverable_patch()
    assert harness is not None
    assert summaries in (root / harness).read_text()
