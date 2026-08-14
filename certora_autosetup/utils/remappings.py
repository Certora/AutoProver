"""Shared builder for a Certora `packages` list from a Foundry project's remapping sources.

Both the *initial* conf generation (``build_systems/foundry.py``'s ``FoundryManager.parse_config``)
and the *reactive* source-not-found workaround (``utils/compilation_workarounds.py``) must produce
the same packages list — historically they diverged, which is what left the initial conf missing
``remappings.txt`` / auto-inferred ``lib/*`` entries and caused ``ParserError: Source "…" not
found``. This module is the single source of truth both call.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

import tomllib

# (message, level) -> None; matches BuildSystemManager.log / CompilationWorkaroundManager.log.
LogFn = Callable[[str, str], None]

# Remapping entries whose key ends in one of these target a concrete source file, not a directory
# prefix, so they must not receive a trailing-slash boundary (see `_merge_remapping_entry`).
_SOURCE_SUFFIXES = (".sol", ".vy", ".yul")


def build_packages_from_remapping_sources(
    base_dir: Path,
    log_fn: LogFn,
    profile: str = "default",
    run_root: Optional[Path] = None,
) -> List[str]:
    """Build a merged packages list from forge remappings, foundry.toml, remappings.txt, package.json.

    All sources are read relative to ``base_dir`` (the Foundry project dir), and ``forge remappings``
    is run with ``cwd=base_dir``, so the result is correct even when the process CWD differs from the
    project dir (nested/walked-up ``foundry.toml``). ``profile`` is passed to forge via
    ``FOUNDRY_PROFILE`` and selects the ``[profile.<profile>]`` remappings read from foundry.toml
    when forge is unavailable, so a non-default profile's remappings are honored.

    ``run_root`` is the directory certoraRun is invoked from (the autosetup run root). It is what
    the *context* half of a context-scoped remapping must be expressed against — see
    ``_rebase_context``. It equals ``base_dir`` for a project whose build config sits at the run
    root, in which case every context comes out unchanged; pass it whenever the caller knows it.

    Priority on key conflict (highest wins, with a warning on path mismatch):
    1. ``forge remappings`` — recursively walks nested foundry.toml files (e.g. lib/*/foundry.toml)
        and emits paths relative to CWD; strictly stronger than parsing the top-level
        foundry.toml alone. Best-effort: skipped silently if forge is not installed or
        the command fails.
    2. foundry.toml — hand-curated source of truth for the build system
    3. remappings.txt — often partially auto-generated; may drift
    4. package.json — npm-style fallback
    """
    # Data collection: key -> resolved path (first source to set a key wins) and key -> source
    # (for the mismatch warning). The packages list is formatted once at the end, preserving this
    # insertion order (= the priority order above).
    remapping_key_to_path: Dict[str, str] = {}
    remapping_key_to_source: Dict[str, str] = {}

    # Try `forge remappings` (highest priority — walks nested foundry.toml files)
    try:
        result = subprocess.run(
            ["forge", "remappings"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=str(base_dir),
            env={**os.environ, "FOUNDRY_PROFILE": profile},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log_fn(f"Could not run `forge remappings` ({e}); falling back to local files", "INFO")
        result = None

    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _merge_remapping_entry(
                entry=line,
                source_name="`forge remappings`",
                remapping_key_to_path=remapping_key_to_path,
                remapping_key_to_source=remapping_key_to_source,
                warn_on_mismatch=False,
                base_dir=base_dir,
                run_root=run_root,
                log_fn=log_fn,
            )
    elif result is not None:
        log_fn(
            f"`forge remappings` exited with code {result.returncode}; falling back to local files",
            "WARNING",
        )

    # Read foundry.toml (next priority — top-level remappings field)
    foundry_toml_path = base_dir / "foundry.toml"
    if foundry_toml_path.exists():
        try:
            with foundry_toml_path.open("rb") as f:
                foundry_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            log_fn(f"Failed to parse foundry.toml: {e}", "WARNING")
            foundry_data = {}

        foundry_remappings: List[str] = []
        # foundry.toml keeps remappings under `[profile.<name>]` (top-level keys belong to the
        # default profile). Read the requested profile's remappings plus any top-level ones
        # (profiles.get(profile) is the default section when profile is "default").
        profiles = foundry_data.get("profile", {})
        foundry_remappings.extend(profiles.get(profile, {}).get("remappings", []) or [])
        foundry_remappings.extend(foundry_data.get("remappings", []) or [])

        for entry in foundry_remappings:
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            _merge_remapping_entry(
                entry=entry,
                source_name="foundry.toml",
                remapping_key_to_path=remapping_key_to_path,
                remapping_key_to_source=remapping_key_to_source,
                warn_on_mismatch=False,
                base_dir=base_dir,
                run_root=run_root,
                log_fn=log_fn,
            )

    # Read remappings.txt
    remappings_path = base_dir / "remappings.txt"
    if remappings_path.exists():
        for line in remappings_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _merge_remapping_entry(
                entry=line,
                source_name="remappings.txt",
                remapping_key_to_path=remapping_key_to_path,
                remapping_key_to_source=remapping_key_to_source,
                warn_on_mismatch=True,
                base_dir=base_dir,
                run_root=run_root,
                log_fn=log_fn,
            )

    # Read package.json and add entries not already in remappings
    package_json_path = base_dir / "package.json"
    if package_json_path.exists():
        try:
            package_data = json.loads(package_json_path.read_text())
        except json.JSONDecodeError as e:
            log_fn(f"Failed to parse package.json: {e}", "WARNING")
            package_data = {}
        for section in ("dependencies", "devDependencies", "resolutions"):
            for key in package_data.get(section, {}):
                _merge_remapping_entry(
                    entry=f"{key}=node_modules/{key}",
                    source_name="package.json",
                    remapping_key_to_path=remapping_key_to_path,
                    remapping_key_to_source=remapping_key_to_source,
                    warn_on_mismatch=True,
                    base_dir=base_dir,
                    run_root=run_root,
                    log_fn=log_fn,
                )

    return [f"{key}={path}" for key, path in remapping_key_to_path.items()]


def _rebase_context(context: str, base_dir: Path, run_root: Optional[Path], log_fn: LogFn) -> str:
    """Re-express a remapping *context* against ``run_root``.

    A remapping's two halves are matched against different things. The target is a filesystem
    path, so any spelling that reaches the directory works. The context is matched by solc
    against the **source unit name** of the importing file — i.e. the path as it appears in the
    conf's ``files``, which is relative to the directory certoraRun runs from. ``forge
    remappings`` reports contexts relative to the Foundry project dir instead, so for a project
    nested under the run root (``<repo>/chains/ethereum-1/foundry.toml``) a context like
    ``src/Widget_1234/`` never prefixes the source unit name ``chains/ethereum-1/src/Widget_1234/…``,
    no remapping applies, and every import of that sub-project fails to resolve.

    A context is rebased only when ``base_dir/context`` is a real directory, which is what tells
    a project-relative context apart from one that is already run-root-relative. Anything else
    (no ``run_root``, a context naming no directory, a context resolving to the run root itself
    or outside it) is returned untouched: those are shapes this cannot improve on, so leave them
    alone. Only the outside-the-run-root case warns — it is the one that cannot work at all.
    """
    if run_root is None or not context:
        return context

    resolved = Path(context) if Path(context).is_absolute() else base_dir / context
    if not resolved.is_dir():
        return context

    rebased = os.path.relpath(resolved, run_root)
    if rebased == os.curdir:
        # The context IS the run root, so it already covers every source unit name. Its faithful
        # translation is a global, context-free remapping — but promoting a scoped remapping to
        # global would let it shadow correct global mappings (solc ranks longest matching context
        # first), so keep it as authored. Nothing is wrong here, hence no warning.
        return context
    if rebased.startswith(os.pardir):
        log_fn(
            f"Remapping context '{context}' resolves outside the run root {run_root} "
            f"— leaving it as is",
            "WARNING",
        )
        return context

    # relpath drops the trailing slash; the context is a path *prefix*, so put it back exactly
    # when the source had one (see `_merge_remapping_entry` on why the boundary slash matters).
    return rebased + "/" if context.endswith("/") else rebased


def _merge_remapping_entry(
    *,
    entry: str,
    source_name: str,
    remapping_key_to_path: Dict[str, str],
    remapping_key_to_source: Dict[str, str],
    warn_on_mismatch: bool,
    base_dir: Path,
    run_root: Optional[Path] = None,
    log_fn: LogFn,
) -> None:
    """Record a single `key=path` remapping entry into the running key->path/source maps.

    Both sides of the entry are whitespace-stripped and normalized to a canonical *trailing-slash*
    form (a slash is appended when missing), so the merged list is internally consistent regardless
    of which source emitted the entry (and tolerant of ``@oz/ = lib/oz/``-style spacing). The key's
    trailing slash is significant and MUST be preserved: a remapping key marks a path *prefix* whose
    boundary is the slash, so ``@openzeppelin/contracts/`` must not swallow imports beginning
    ``@openzeppelin/contracts-upgradeable/``. solc chooses among applicable remappings by longest
    matching *context* first (only then longest prefix), so a context-scoped key like
    ``lib/some-dependency/:@openzeppelin/contracts`` — if its boundary slash were stripped —
    outranks the correct global ``@openzeppelin/contracts-upgradeable`` mapping and rewrites the
    upgradeable import to a nonexistent path. Keeping the slash keeps the two packages distinct.
    (Normalizing both sides to end in a slash also collapses ``@oz/contracts`` and
    ``@oz/contracts/`` from different sources onto one key, so the dedup below stays correct.)

    Relative target paths are resolved to absolute against ``base_dir`` so the packages list is
    valid even when the process CWD differs from the project dir. The *context* half of a
    context-scoped key gets the opposite treatment — ``_rebase_context`` re-expresses it against
    ``run_root``, because solc matches it against source unit names rather than the filesystem.

    On a key conflict (already populated by an earlier-priority source):
    - if ``warn_on_mismatch`` and the stored path differs from the new one, log a warning naming
      the actual earlier source from ``remapping_key_to_source``;
    - otherwise silently skip.

    Caller is responsible for confirming the entry contains an ``=`` before calling.
    """
    raw_key, raw_path = entry.split("=", 1)
    key = raw_key.strip()
    path = raw_path.strip()

    # A context-scoped key is `context:prefix`; solc reads the context as a source-unit-name
    # prefix, so it belongs to the run root while the prefix is opaque text (see `_rebase_context`).
    if ":" in key:
        context, prefix = key.split(":", 1)
        key = f"{_rebase_context(context, base_dir, run_root, log_fn)}:{prefix}"

    # Resolve a relative target against base_dir first (Path() drops any trailing slash).
    if not Path(path).is_absolute():
        path = str(base_dir / path)

    # Canonicalize a DIRECTORY remapping to a trailing-slash form so the key's prefix boundary is
    # preserved (see docstring) and key/path agree on that boundary. A remapping that targets a
    # concrete source file (e.g. an import-patch entry `.../IFoo.sol=.../IFoo.sol`) must keep its
    # exact form — appending `/` would make solc look for a directory `IFoo.sol/`. Detect the
    # file case by a source-file extension on the key.
    if not key.lower().endswith(_SOURCE_SUFFIXES):
        if key and not key.endswith("/"):
            key += "/"
        if path and not path.endswith("/"):
            path += "/"

    if key in remapping_key_to_path:
        if warn_on_mismatch and remapping_key_to_path[key] != path:
            earlier_source = remapping_key_to_source[key]
            log_fn(
                f"Package '{key}' has different paths in {earlier_source} "
                f"('{remapping_key_to_path[key]}') and {source_name} ('{path}') "
                f"— using {earlier_source}",
                "WARNING",
            )
        return

    remapping_key_to_path[key] = path
    remapping_key_to_source[key] = source_name
