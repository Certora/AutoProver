"""Locate the build-system project directory that owns the contract under analysis.

A repository's Foundry/Hardhat project is not always at the repo root: monorepos keep the
build config next to the sources it governs (``<repo>/<package>/foundry.toml``), and
autosetup may be invoked from anywhere above it. These helpers resolve the directory that
owns a contract — the remappings, pinned solc and artifact layout all come from there.

"Owns" is decided by build *artifacts*, not by the presence of a config file. Plenty of
monorepos vendor a per-package ``foundry.toml`` under ``modules/`` or ``lib/`` while the
root config is what actually builds the tree, so the nearest config is often not the one
that ran. See ``find_build_config_dir``.
"""

import os
import tomllib
from pathlib import Path
from typing import Optional

# Build config filenames that mark a directory as a project root, in no particular order —
# presence of any one of them is enough to anchor there. Truffle has two: `truffle.js` is
# the v4 spelling, `truffle-config.js` everything since.
BUILD_CONFIG_FILENAMES = (
    "foundry.toml",
    "hardhat.config.js",
    "hardhat.config.ts",
    "truffle-config.js",
    "truffle.js",
)

# Truffle writes artifacts here unless the config sets `contracts_build_directory`. Reading
# that override means running node against the config, which is more than this needs: it
# only has to recognise a built project, and the manager resolves the real path later.
TRUFFLE_DEFAULT_BUILD_DIR = Path("build") / "contracts"


def _artifact_dir_of(config_dir: Path) -> Optional[Path]:
    """Where *config_dir*'s build system would put artifacts, or None if it holds no config.

    Foundry's ``out`` is configurable, so read it when present; Hardhat's ``artifacts`` and
    Truffle's ``build/contracts`` are taken as defaults. The directory is not required to
    exist — the caller tests that.
    """
    foundry_toml = config_dir / "foundry.toml"
    if foundry_toml.exists():
        out = "out"
        try:
            with foundry_toml.open("rb") as f:
                data = tomllib.load(f)
            profiles = data.get("profile", {})
            out = profiles.get("default", {}).get("out") or data.get("out") or "out"
        except (tomllib.TOMLDecodeError, OSError):
            pass
        return config_dir / out
    if (config_dir / "hardhat.config.js").exists() or (config_dir / "hardhat.config.ts").exists():
        return config_dir / "artifacts"
    if (config_dir / "truffle-config.js").exists() or (config_dir / "truffle.js").exists():
        return config_dir / TRUFFLE_DEFAULT_BUILD_DIR
    return None


def find_build_config_dir(contract_path: Path, root: Path) -> Path:
    """Return the directory whose build system actually produced *contract_path*'s artifacts.

    Walks up from the contract's own directory to *root*, and returns the nearest ancestor
    that both holds a build config **and** has its artifact directory on disk. Falling back
    to *root* when nothing qualifies is deliberate: a build config alone does not mean that
    project is the one that got built. Monorepos routinely vendor per-package ``foundry.toml``
    files under ``modules/`` or ``lib/`` while the root config builds the whole tree into a
    single ``out/`` — anchoring on the nearest config there would point the extractor at an
    artifact directory that does not exist. Requiring artifacts means this only ever moves
    off *root* on positive evidence, so every project that worked before still resolves to
    *root* exactly as it did.

    Consequence worth knowing: this must run *after* the build. Called on an unbuilt tree
    nothing has artifacts, so it answers *root* — correct for the common case, and for a
    nested project it merely restores the old behaviour rather than inventing a wrong one.

    Args:
        contract_path: Path to the main contract source file. May be relative to *root*.
        root: Directory to stop the upward walk at (the autosetup run root).

    Returns:
        The owning project directory, or *root* if none qualified.
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
        artifacts = _artifact_dir_of(current)
        if artifacts is not None and artifacts.is_dir():
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

    For logging: None means "nothing worth saying", so callers can mention the project
    directory only when it is somewhere other than where the run started.
    """
    if build_config_dir.resolve() == root.resolve():
        return None
    try:
        return str(build_config_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(build_config_dir)
