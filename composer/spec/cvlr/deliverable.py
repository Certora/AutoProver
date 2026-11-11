"""Each unit's deliverable: the patch that carries its harness back to the project as shipped.

A CVLR harness is not a file beside the program: it is a module inside the crate, wired in through
the crate's ``Cargo.toml`` and ``lib.rs``, with tuning files next to it. A consumer that takes only
:data:`~composer.spec.gen_types.CVLR_DELIVERABLE_DIR` gets none of that, so every unit with a
harness gets a directory under :data:`COMPONENTS_DIR`::

    <module>/
      <module>.patch   # git apply to the project as shipped: this unit's proved tree
      <module>.conf    # rerun its rules, from the package directory (delivered units only)
      files/<path>     # each file the patch adds, as a file

The patch is the unit's tree diffed against a baseline, a copy of the project taken before
preflight writes anything and kept under :data:`BASELINE_DIR`. Diffing against it rather than
against a list of what each step wrote is what keeps the patch complete: preflight's scaffold is not
the only writer, and a writer added later is covered without anyone remembering to register it.

The unit's tree is the project as the run left it, narrowed to this one unit (:class:`UnitTree`):
the other units' modules, tuning files and cargo features taken out, the module index naming this
unit alone, and this unit's edits to the program laid over the files they replace. So each patch
stands alone and reproduces a tree a prover run checked. No patch combines units: edits to the
program are per unit and may conflict, and a merged tree is one no prover run checked. The patches
are applied one at a time, each to the clean project: every one of them creates the scaffold, so a
second one has nothing to apply onto.

Edits to files the project already has stay in the patch only. The ``files/`` copies are there so a
reader who does not apply patches can still read the harness.
"""

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from composer.cargo.features import CargoFeature
from composer.prover.conf import Conf, dump_conf
from composer.spec.cvlr.scaffold import withdraw_unit_features
from composer.spec.gen_types import CVLR_DELIVERABLE_DIR, CVLR_INTERNAL_DIR
from composer.spec.source.munge.vfs_diff import compute_diff
from composer.spec.source.report.schema import UnitDeliverable

BASELINE_DIR = CVLR_INTERNAL_DIR / "baseline"
#: One directory per unit. Under :data:`CVLR_DELIVERABLE_DIR`, so no unit's patch diffs another in.
COMPONENTS_DIR = CVLR_DELIVERABLE_DIR / "components"
#: Where, inside a unit's directory, the files its patch adds are copied.
FILES_DIR = "files"

#: A harness module name, which is the only name a unit's directory is given.
_MODULE_STEM = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Directories that are never source: build output, VCS state, and this run's own output.
_SKIPPED_DIRS = frozenset({".certora_internal", "target", ".git", "node_modules"})
#: Cargo rewrites the lockfile on the consumer's first build; carrying the run's copy would make the
#: patch fail to apply against any lockfile that moved since.
_SKIPPED_FILES = frozenset({"Cargo.lock"})


def _is_source(relative: Path) -> bool:
    if relative.is_relative_to(CVLR_DELIVERABLE_DIR):
        return False
    if _SKIPPED_DIRS.intersection(relative.parts[:-1]) or relative.name in _SKIPPED_DIRS:
        return False
    return relative.name not in _SKIPPED_FILES


def _overlay_key(path: str) -> str | None:
    """The project-relative source path ``path`` names, or ``None`` when it must not enter a patch.

    An overlay key is whatever path the editor wrote. One that leaves the project, or that
    :func:`_is_source` already refuses, would make the patch apply somewhere the scaffold does not.
    """
    if not path or path.startswith(("/", "\\")):
        return None
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    posix = relative.as_posix()
    if not _is_source(Path(posix)):
        return None
    return posix


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


@dataclass(frozen=True)
class UnitTree:
    """How one unit's tree differs from the project as the run left it. Paths are project-relative
    and POSIX."""

    #: Files the run writes for some unit, this one included. None of them is in the unit's tree
    #: unless :attr:`own` puts it back, so the tree does not depend on which units have finished.
    dropped: frozenset[str]
    #: The unit's own harness files, whole: its module, the module index naming it alone, its
    #: tuning files.
    own: Mapping[str, str]
    #: The unit's edits to the program, whole, keyed by the path the editor wrote.
    edits: Mapping[str, str]
    #: The package manifest, and the features in it that only other units use.
    manifest: str
    withdrawn: tuple[CargoFeature, ...]

    def over(self, on_disk: Mapping[str, str]) -> dict[str, str]:
        """The unit's tree, given the project's source files as they are on disk."""
        tree = {path: text for path, text in on_disk.items() if path not in self.dropped}
        for path, text in self.edits.items():
            key = _overlay_key(path)
            if key is not None:
                tree[key] = text
        # After the edits: an edit to the manifest is a whole file from the run's shared tree, so it
        # declares every unit's feature too.
        if self.manifest in tree:
            tree[self.manifest] = withdraw_unit_features(tree[self.manifest], self.withdrawn)
        tree.update(self.own)
        return dict(sorted(tree.items()))


def clear_components(root: Path) -> None:
    """Remove every unit's directory, so a unit this run does not deliver keeps none from an
    earlier run."""
    shutil.rmtree(root / COMPONENTS_DIR, ignore_errors=True)


def write_unit(root: Path, module: str, tree: UnitTree, conf: Conf | None) -> UnitDeliverable | None:
    """Write ``module``'s directory under :data:`COMPONENTS_DIR`, replacing any already there, and
    return its paths relative to ``root``. ``None``, writing nothing, when there is no baseline to
    diff against.

    Raises :class:`ValueError` when ``module`` is not a harness module name.
    """
    if not _MODULE_STEM.fullmatch(module):
        raise ValueError(f"not a harness module name: {module!r}")
    baseline = root / BASELINE_DIR
    if not baseline.is_dir():
        return None
    base = _source_files(baseline)
    files = tree.over(_source_files(root))

    directory = root / COMPONENTS_DIR / module
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    patch = directory / f"{module}.patch"
    patch.write_text(compute_diff(base.get, files), encoding="utf-8")
    for path, text in files.items():
        if path in base:
            continue
        copy = directory / FILES_DIR / path
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(text, encoding="utf-8")
    conf_path: Path | None = None
    if conf is not None:
        conf_path = directory / f"{module}.conf"
        conf_path.write_text(dump_conf(conf))
    return UnitDeliverable(
        patch=patch.relative_to(root).as_posix(),
        conf=None if conf_path is None else conf_path.relative_to(root).as_posix(),
    )
