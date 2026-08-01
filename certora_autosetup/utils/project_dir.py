"""Locate the build-system project directory that owns the contract under analysis.

A repository's Foundry/Hardhat project is not always at the repo root: monorepos keep the
build config next to the sources it governs (``<repo>/<package>/foundry.toml``). Autosetup
runs with CWD at the repo root, and ``BuildSystemManager.find_config_file`` only walks
*upward* from there, so a nested project is invisible to it — detection reports ``unknown``,
the run proceeds "without build config", and the generated conf carries neither the project's
remappings nor its pinned solc. The symptom is a bare
``ParserError: Source "@pkg/Foo.sol" not found ... Searched the following locations: ""``.

Anchoring on the main contract's own path fixes this without any new plumbing: the directory
that owns a contract is the nearest ancestor of that contract holding a build config.
"""

import os
from pathlib import Path
from typing import Optional

# Build config filenames that mark a directory as a project root, in no particular order —
# presence of any one of them is enough to anchor there.
BUILD_CONFIG_FILENAMES = ("foundry.toml", "hardhat.config.js", "hardhat.config.ts")


def find_build_config_dir(contract_path: Path, root: Path) -> Path:
    """Return the nearest ancestor of *contract_path* holding a build config.

    The search starts at the contract file's own directory and walks up, stopping at (and
    including) *root*. ``root`` is returned when no ancestor holds a build config, so the
    caller keeps today's root-anchored behaviour whenever there is nothing better to anchor
    on — including for a repo whose project genuinely is at the root, where the first
    directory that matches *is* ``root``.

    Args:
        contract_path: Path to the main contract source file. May be relative to *root*.
        root: Directory to stop the upward walk at (the autosetup run root).

    Returns:
        The owning project directory, or *root* if none was found.
    """
    root = root.resolve()
    absolute_contract = contract_path if contract_path.is_absolute() else root / contract_path

    # A contract outside the run root has no ancestor chain to walk within it.
    try:
        absolute_contract.resolve().relative_to(root)
    except ValueError:
        return root

    current = absolute_contract.resolve().parent
    while True:
        if any((current / name).exists() for name in BUILD_CONFIG_FILENAMES):
            return current
        if current == root:
            return root
        current = current.parent


def rebase(rel_path: str, from_dir: Path, to_dir: Path) -> str:
    """Reinterpret *rel_path* — given relative to *from_dir* — as relative to *to_dir*.

    Pure path arithmetic; nothing is required to exist on disk. Absolute inputs are
    returned unchanged, since they need no anchor.

    Build systems record source paths relative to their own project dir (Foundry's
    ``compilationTarget``, for one), so a nested project hands back ``src/Foo.sol``
    while the process CWD is the repo root and needs ``pkg/portal/src/Foo.sol``.
    """
    if Path(rel_path).is_absolute():
        return rel_path
    absolute = (from_dir / rel_path).resolve()
    return os.path.relpath(absolute, to_dir.resolve())


def describe_build_config_dir(build_config_dir: Path, root: Path) -> Optional[str]:
    """Return a root-relative description of *build_config_dir*, or None if it is *root*.

    Used purely for logging, so a nested project announces itself and the "unknown build
    system" case stays distinguishable from "root project with no config".
    """
    if build_config_dir.resolve() == root.resolve():
        return None
    try:
        return str(build_config_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(build_config_dir)
