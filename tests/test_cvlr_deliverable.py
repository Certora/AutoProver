"""Each unit's deliverables: a directory with a patch of that unit's tree alone and the files the
patch adds, and its conf beside the other units'."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.pipeline.ptypes import Curtailed, Delivered, GaveUp
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION, OptimisticLoop, TunableConf
from composer.spec.cvlr.deliverable import (
    BASELINE_DIR,
    COMPONENTS_DIR,
    CONFS_DIR,
    FILES_DIR,
    UnitTree,
    clear_components,
    snapshot,
    write_unit,
)
from composer.spec.cvlr.harness import GeneratedHarness, HarnessModule
from composer.spec.cvlr.scaffold import ENVS_DIR
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.source.munge.edit_store import AppliedEdit

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_CARGO = '[package]\nname = "p"\n'
_SCAFFOLDED_CARGO = (
    _CARGO + "\n[features]\ncertora = []\ncertora_unit_vault = []\ncertora_unit_fees = []\n"
)
_LIB = 'fn f() {}\n#[cfg(feature = "certora")]\nmod certora;\n'


def _project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO)
    (root / "Cargo.lock").write_text("# lock v1\n")
    (root / "src" / "lib.rs").write_text("fn f() {}\n")
    (root / "src" / "processor.rs").write_text("fn process() {}\n")
    return root


def _scaffold(root: Path) -> None:
    """What a run leaves in the project: the scaffold and every unit's declarations, the modules of
    the units delivered so far, and output that is not source."""
    (root / "Cargo.toml").write_text(_SCAFFOLDED_CARGO)
    (root / "Cargo.lock").write_text("# lock v2\n")
    (root / "src" / "lib.rs").write_text(_LIB)
    specs = root / "src" / "certora" / "specs"
    specs.mkdir(parents=True)
    (root / "src" / "certora" / "mod.rs").write_text("pub mod specs;\n")
    (specs / "mod.rs").write_text("pub mod fees;\npub mod vault;\n")
    (specs / "fees.rs").write_text("// fees rules\n")
    (specs / "vault.rs").write_text("// vault rules, as written\n")
    (specs / "cut.rs.unverified").write_text("// cut draft\n")
    envs = root / ENVS_DIR
    envs.mkdir(parents=True)
    (envs / "cvlr_summaries.txt").write_text(";; package\n")
    (envs / "cvlr_summaries_fees.txt").write_text(";; fees\n")
    (root / "target").mkdir()
    (root / "target" / "out.txt").write_text("built\n")
    (root / "certora" / "cvlr" / "reports").mkdir(parents=True)
    (root / "certora" / "cvlr" / "reports" / "report.json").write_text("{}\n")


def _tree(own_module: str, *, edits: dict[str, str] | None = None) -> UnitTree:
    others = {"vault", "fees", "cut"} - {own_module}
    return UnitTree(
        dropped=frozenset(
            path
            for m in ("vault", "fees", "cut")
            for path in (
                f"src/certora/specs/{m}.rs",
                f"src/certora/specs/{m}.rs.unverified",
                f"src/certora/envs/cvlr_summaries_{m}.txt",
            )
        ),
        own={
            f"src/certora/specs/{own_module}.rs": f"// {own_module} rules\n",
            "src/certora/specs/mod.rs": f"pub mod {own_module};\n",
            f"src/certora/envs/cvlr_summaries_{own_module}.txt": f";; {own_module}\n",
        },
        edits=edits or {},
        manifest="Cargo.toml",
        withdrawn=tuple(HarnessModule(m).feature for m in sorted(others)),
    )


_VAULT_EDITS = {
    "Cargo.toml": _SCAFFOLDED_CARGO.replace(
        "certora_unit_vault = []", 'certora_unit_vault = ["dep"]'
    ),
    "src/processor.rs": '#[cfg(feature = "certora_unit_vault")]\nfn process() {}\n',
    "../outside.txt": "nope\n",
    "Cargo.lock": "not carried\n",
}


def _added(patch: str) -> set[str]:
    return {line[len("+++ b/"):] for line in patch.splitlines() if line.startswith("+++ b/")}


def test_a_unit_tree_has_its_own_harness_and_no_other_units(tmp_path):
    """Other units' modules, drafts, summaries and features are out. The edits are in, the manifest
    edit included, and the features are withdrawn from it too. A path the patch must refuse stays
    out."""
    root = _project(tmp_path / "proj")
    snapshot(root)
    _scaffold(root)

    written = write_unit(root, "vault", _tree("vault", edits=_VAULT_EDITS), None)

    assert written is not None
    patch = (root / written.patch).read_text()
    assert _added(patch) == {
        "Cargo.toml",
        "src/lib.rs",
        "src/processor.rs",
        "src/certora/mod.rs",
        "src/certora/specs/mod.rs",
        "src/certora/specs/vault.rs",
        "src/certora/envs/cvlr_summaries.txt",
        "src/certora/envs/cvlr_summaries_vault.txt",
    }
    assert 'certora_unit_vault = ["dep"]' in patch
    assert "certora_unit_fees" not in patch
    assert "// vault rules\n" in patch and "as written" not in patch
    assert "outside" not in patch and "not carried" not in patch and "lock v2" not in patch


def test_the_files_a_patch_adds_are_copied_beside_it_and_edits_are_not(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    _scaffold(root)

    write_unit(root, "fees", _tree("fees"), None)

    files = root / COMPONENTS_DIR / "fees" / FILES_DIR
    copied = sorted(p.relative_to(files).as_posix() for p in files.rglob("*") if p.is_file())
    assert copied == [
        "src/certora/envs/cvlr_summaries.txt",
        "src/certora/envs/cvlr_summaries_fees.txt",
        "src/certora/mod.rs",
        "src/certora/specs/fees.rs",
        "src/certora/specs/mod.rs",
    ]
    assert (files / "src/certora/specs/fees.rs").read_text() == "// fees rules\n"


@needs_git
def test_each_patch_applies_to_the_clean_project_and_not_onto_another(tmp_path):
    """Including the edit to a file that had no final newline, which a diff has to mark for
    ``git apply`` to accept it."""
    root = _project(tmp_path / "proj")
    (root / "src" / "lib.rs").write_text("fn f() {}")
    snapshot(root)
    _scaffold(root)
    vault = write_unit(root, "vault", _tree("vault", edits=_VAULT_EDITS), None)
    fees = write_unit(root, "fees", _tree("fees"), None)
    assert vault is not None and fees is not None

    def applied(name: str, patch: str) -> Path:
        tree = _project(tmp_path / name)
        (tree / "src" / "lib.rs").write_text("fn f() {}")
        subprocess.run(
            ["git", "apply", str(root / patch)], cwd=tree, check=True, capture_output=True
        )
        return tree

    vault_tree = applied("vault", vault.patch)
    assert (vault_tree / "Cargo.toml").read_text() == (
        _CARGO + '\n[features]\ncertora = []\ncertora_unit_vault = ["dep"]\n'
    )
    assert "certora_unit_vault" in (vault_tree / "src" / "processor.rs").read_text()
    assert (vault_tree / "src" / "lib.rs").read_text() == _LIB
    assert not (vault_tree / "src" / "certora" / "specs" / "fees.rs").exists()

    fees_tree = applied("fees", fees.patch)
    assert (fees_tree / "Cargo.toml").read_text() == (
        _CARGO + "\n[features]\ncertora = []\ncertora_unit_fees = []\n"
    )
    assert (fees_tree / "src" / "processor.rs").read_text() == "fn process() {}\n"
    assert (fees_tree / "src" / "certora" / "specs" / "mod.rs").read_text() == "pub mod fees;\n"

    second = subprocess.run(
        ["git", "apply", "--check", str(root / fees.patch)], cwd=vault_tree, capture_output=True
    )
    assert second.returncode != 0


def test_a_resumed_run_keeps_the_first_baseline(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    _scaffold(root)
    snapshot(root)

    assert (root / BASELINE_DIR / "src" / "lib.rs").read_text() == "fn f() {}\n"


def test_without_a_baseline_nothing_is_written(tmp_path):
    root = _project(tmp_path / "proj")
    _scaffold(root)
    assert write_unit(root, "vault", _tree("vault"), None) is None
    assert not (root / COMPONENTS_DIR).exists()


def test_a_unit_directory_is_named_for_a_module(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    with pytest.raises(ValueError):
        write_unit(root, "../vault", _tree("vault"), None)


def test_rewriting_a_unit_replaces_its_directory_and_clearing_removes_them_all(tmp_path):
    root = _project(tmp_path / "proj")
    snapshot(root)
    _scaffold(root)
    stale = root / COMPONENTS_DIR / "vault" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale\n")
    stale_conf = root / CONFS_DIR / "vault.conf"
    stale_conf.parent.mkdir(parents=True)
    stale_conf.write_text("{}\n")

    write_unit(root, "vault", _tree("vault"), None)
    assert not stale.exists()
    assert not stale_conf.exists()

    write_unit(root, "fees", _tree("fees"), {"msg": "fees"})
    clear_components(root)
    assert not (root / COMPONENTS_DIR).exists()
    assert not (root / CONFS_DIR).exists()


def _formalizer(root: Path):
    """A formalizer whose ``deps`` hold only what :meth:`settle` reads. The package is the root."""
    from composer.spec.cvlr.env_paths import PathDialect
    from composer.spec.cvlr.harness import CvlrArtifactStore
    from composer.spec.cvlr.pipeline import CvlrFormalizer

    deps = SimpleNamespace(
        store=CvlrArtifactStore(root, Path(".")),
        package_dir=Path("."),
        preflight=SimpleNamespace(package="p", scaffold=SimpleNamespace(dialect=PathDialect())),
    )
    units = tuple(HarnessModule(m) for m in ("vault", "fees", "cut", "lost"))
    return CvlrFormalizer(None, None, deps=deps, build=None, units=units)  # type: ignore[arg-type]


def _outcome(slug: str, name: str, result: object) -> SimpleNamespace:
    return SimpleNamespace(feat=SimpleNamespace(slug=slug, display_name=name), result=result)


@pytest.mark.asyncio
async def test_settling_writes_every_unit_with_a_harness_and_the_report_names_them(tmp_path):
    """A delivered unit gets a patch and a conf whether or not it edited the program. A curtailed
    one gets a patch with its draft as the module, and no conf. A unit that gave up gets nothing.
    ``cumulative_diff`` stays the edits against the tree the run left; ``patch_path`` names the
    unit's patch."""
    root = _project(tmp_path / "proj")
    snapshot(root)
    _scaffold(root)
    settings = TunableConf(loop_iter=4, optimistic_loop=OptimisticLoop(why="bounded by input"))
    summary = SummaryDirective(pattern="^<p::E as Display>::fmt$", why="[3308]")
    edited = GeneratedHarness(
        commentary="",
        harness="// vault rules\n",
        applied_edits=[
            AppliedEdit(edit_id="e", executive_summary="forward the feature", why_sound="cfg")
        ],
        vfs={"src/processor.rs": '#[cfg(feature = "certora_unit_vault")]\nfn process() {}\n'},
        declared_rules=["rule_b", "rule_a"],
        summaries=[summary],
        settings=settings,
    )
    outcomes = [
        _outcome("vault", "Vault & Fees", Delivered(result=edited, deliverable=Path("vault.rs"))),
        _outcome(
            "fees",
            "Fees",
            Delivered(
                result=GeneratedHarness(commentary="", harness="// fees rules\n"),
                deliverable=Path("fees.rs"),
            ),
        ),
        _outcome(
            "cut",
            "Cut",
            Curtailed(
                Delivered(
                    result=edited.model_copy(update={"harness": "// cut draft\n"}),
                    deliverable=Path("cut.rs.unverified"),
                )
            ),
        ),
        _outcome("lost", "Lost", GaveUp(reason="no")),
    ]
    formalizer = _formalizer(root)
    run = SimpleNamespace(source=SimpleNamespace(project_root=str(root)))
    for outcome in outcomes:
        await formalizer.settle(outcome, run)  # type: ignore[arg-type]

    deliverables = formalizer.unit_deliverables()
    assert sorted(deliverables) == ["Cut", "Fees", "Vault & Fees"]
    vault = deliverables["Vault & Fees"]
    assert vault.patch == (COMPONENTS_DIR / "vault" / "vault.patch").as_posix()
    assert vault.conf == (CONFS_DIR / "vault.conf").as_posix()
    assert deliverables["Fees"].conf is None
    assert deliverables["Cut"].conf is None
    assert not (root / COMPONENTS_DIR / "lost").exists()

    vault_patch = (root / vault.patch).read_text()
    assert "certora_unit_vault" in vault_patch and "certora_unit_fees" not in vault_patch
    assert "certora_unit_lost" not in vault_patch
    assert summary.pattern in vault_patch
    cut_patch = (root / deliverables["Cut"].patch).read_text()
    assert "+++ b/src/certora/specs/cut.rs\n" in cut_patch and "// cut draft" in cut_patch
    assert "unverified" not in cut_patch

    conf = json.loads((root / vault.conf).read_text())
    assert "build_script" not in conf
    assert conf["cargo_features"] == ["certora", "certora_unit_vault"]
    assert conf["cargo_tools_version"] == PLATFORM_TOOLS_VERSION
    assert conf["loop_iter"] == "4" and conf["optimistic_loop"] is True
    assert conf["rule"] == ["rule_a", "rule_b"]
    assert "&" not in conf["msg"]
    (summaries,) = conf["solana_summaries"]
    assert Path(summaries).parent == ENVS_DIR
    assert (root / COMPONENTS_DIR / "vault" / FILES_DIR / summaries).is_file()

    records = await formalizer.source_edits(outcomes, run)  # type: ignore[arg-type]
    assert [r.component for r in records] == ["Vault & Fees"]
    assert records[0].patch_path == vault.patch
    assert "certora_unit_vault" in records[0].cumulative_diff
    assert "mod certora" not in records[0].cumulative_diff
    partials = await formalizer.curtailed_source_edits(outcomes, run)  # type: ignore[arg-type]
    assert [r.patch_path for r in partials] == [deliverables["Cut"].patch]
