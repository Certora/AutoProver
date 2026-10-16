"""The harness patch: what a run wrote into the project, as ``git apply`` takes it."""

import shutil
import subprocess
from pathlib import Path

import pytest

from composer.spec.cvlr.patch import BASELINE_DIR, PATCH_PATH, snapshot, write_patch

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
    from types import SimpleNamespace

    from composer.spec.cvlr.pipeline import CvlrFormalizer

    root = _project(tmp_path / "proj")
    snapshot(root)
    _run(root)
    formalizer = CvlrFormalizer(None, None, deps=None, build=None)  # type: ignore[arg-type]
    assert formalizer.deliverable_patch() is None

    run = SimpleNamespace(source=SimpleNamespace(project_root=str(root)))
    await formalizer.finalize([], run)  # type: ignore[arg-type]

    assert formalizer.deliverable_patch() == PATCH_PATH.as_posix()
    assert (root / PATCH_PATH).is_file()
