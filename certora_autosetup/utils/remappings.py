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
from typing import Callable, Dict, List

import tomllib

# (message, level) -> None; matches BuildSystemManager.log / CompilationWorkaroundManager.log.
LogFn = Callable[[str, str], None]

# Remapping entries whose key ends in one of these target a concrete source file, not a directory
# prefix, so they must not receive a trailing-slash boundary (see `_merge_remapping_entry`).
_SOURCE_SUFFIXES = (".sol", ".vy", ".yul")


def build_packages_from_remapping_sources(base_dir: Path, log_fn: LogFn, profile: str = "default") -> List[str]:
    """Build a merged packages list from forge remappings, foundry.toml, remappings.txt, package.json.

    All sources are read relative to ``base_dir`` (the Foundry project dir), and ``forge remappings``
    is run with ``cwd=base_dir``, so the result is correct even when the process CWD differs from the
    project dir (nested/walked-up ``foundry.toml``). ``profile`` is passed to forge via
    ``FOUNDRY_PROFILE`` and selects the ``[profile.<profile>]`` remappings read from foundry.toml
    when forge is unavailable, so a non-default profile's remappings are honored.

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
                    log_fn=log_fn,
                )

    return [f"{key}={path}" for key, path in remapping_key_to_path.items()]


def _merge_remapping_entry(
    *,
    entry: str,
    source_name: str,
    remapping_key_to_path: Dict[str, str],
    remapping_key_to_source: Dict[str, str],
    warn_on_mismatch: bool,
    base_dir: Path,
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
    valid even when the process CWD differs from the project dir.

    On a key conflict (already populated by an earlier-priority source):
    - if ``warn_on_mismatch`` and the stored path differs from the new one, log a warning naming
      the actual earlier source from ``remapping_key_to_source``;
    - otherwise silently skip.

    Caller is responsible for confirming the entry contains an ``=`` before calling.
    """
    raw_key, raw_path = entry.split("=", 1)
    key = raw_key.strip()
    path = raw_path.strip()

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
