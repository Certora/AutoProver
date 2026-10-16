"""The patch that carries a run's harness back to the project as shipped.

A CVLR harness is not a file beside the program: it is a module inside the crate, wired in through
the crate's ``Cargo.toml`` and ``lib.rs``, with tuning files next to it. A consumer that takes only
:data:`~composer.spec.cvlr.harness.DELIVERABLE_DIR` gets none of that, so the run also writes
:data:`PATCH_PATH` there, a unified diff that ``git apply`` lays onto the project as it was before
the run.

The baseline is a copy of the project taken before preflight writes anything, kept under
:data:`BASELINE_DIR`. Diffing against it rather than against a list of what each step wrote is what
keeps the patch complete: preflight's scaffold is not the only writer (each unit adds its own cargo
feature), and a writer added later is covered without anyone remembering to register it. Program
edits are not on disk, so they never reach the patch; they stay in the report's ``source_edits``.
"""

import shutil
from pathlib import Path

from composer.spec.cvlr.harness import DELIVERABLE_DIR, INTERNAL_DIR
from composer.spec.source.munge.vfs_diff import compute_diff

BASELINE_DIR = INTERNAL_DIR / "baseline"
PATCH_PATH = DELIVERABLE_DIR / "harness.patch"

#: Directories that are never source: build output, VCS state, and this run's own output.
_SKIPPED_DIRS = frozenset({".certora_internal", "target", ".git", "node_modules"})
#: Cargo rewrites the lockfile on the consumer's first build; carrying the run's copy would make the
#: patch fail to apply against any lockfile that moved since.
_SKIPPED_FILES = frozenset({"Cargo.lock"})


def _is_source(relative: Path) -> bool:
    if relative.is_relative_to(DELIVERABLE_DIR):
        return False
    if _SKIPPED_DIRS.intersection(relative.parts[:-1]) or relative.name in _SKIPPED_DIRS:
        return False
    return relative.name not in _SKIPPED_FILES


def _source_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not _is_source(relative) or path.is_symlink() or not path.is_file():
            continue
        try:
            files[relative.as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def snapshot(root: Path) -> None:
    """Copy the project's source files to :data:`BASELINE_DIR`, unless a baseline is already there.

    Kept rather than retaken, so a resumed run diffs against the project as first shipped and not
    against the tree its earlier attempt already scaffolded.
    """
    baseline = root / BASELINE_DIR
    if baseline.exists():
        return
    staging = baseline.with_name(baseline.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    for relative, text in _source_files(root).items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    staging.mkdir(parents=True, exist_ok=True)
    staging.rename(baseline)


def write_patch(root: Path) -> Path | None:
    """Write :data:`PATCH_PATH` from the baseline to the tree as it is now, and return its path
    relative to ``root``. ``None`` when there is no baseline or nothing changed, and then no patch
    is left behind: one from an earlier run would describe a tree this run did not produce."""
    out = root / PATCH_PATH
    baseline = root / BASELINE_DIR
    diff = (
        compute_diff(_source_files(baseline).get, _source_files(root)) if baseline.is_dir() else ""
    )
    if not diff:
        out.unlink(missing_ok=True)
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diff, encoding="utf-8")
    return PATCH_PATH
