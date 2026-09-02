"""Shared builder for a Certora `packages` list from a Foundry project's remapping sources.

Both the *initial* conf generation (``build_systems/foundry.py``'s ``FoundryManager.parse_config``)
and the *reactive* source-not-found workaround (``utils/compilation_workarounds.py``) must produce
the same packages list — historically they diverged, which is what left the initial conf missing
``remappings.txt`` / auto-inferred ``lib/*`` entries and caused ``ParserError: Source "…" not
found``. This module is the single source of truth both call.

A run root often holds more than one project, and each of them declares its own import
resolution. Every project other than the anchor therefore contributes *context-scoped* entries
(``<project-rel-path>/:prefix=target``) next to the anchor's unscoped ones. solc matches a
context against the importing file's source unit name and prefers the longest match, so a
scoped entry governs the files of the project it names while the anchor's entries remain the
default for every file no context claims.
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Tuple

import tomllib

from certora_autosetup.setup.solidity_utils import DEPENDENCIES
from certora_autosetup.utils.project_dir import BUILD_CONFIG_FILENAMES

# (message, level) -> None; matches BuildSystemManager.log / CompilationWorkaroundManager.log.
LogFn = Callable[[str, str], None]

# Remapping entries whose key ends in one of these target a concrete source file, not a directory
# prefix, so they must not receive a trailing-slash boundary (see `_merge_remapping_entry`).
_SOURCE_SUFFIXES = (".sol", ".vy", ".yul")

# The one directory name npm/yarn/pnpm hoist packages into, and the only target prefix the
# ancestor walk applies to (see `resolve_node_modules_target`).
_NODE_MODULES = "node_modules"


@dataclass
class PackageResolution:
    """Where a remapping target actually lives, and how it was found.

    ``path`` is the target to emit (always an absolute path built from ``base_dir`` or one of
    its ancestors). ``kind`` says which candidate answered:
    - ``local``     — resolved under ``base_dir``, i.e. the target as authored;
    - ``hoisted``   — resolved in an ancestor's ``node_modules`` (a hoisted install);
    - ``subpath_missing`` — a ``node_modules/<pkg>`` was found, but the remapped subdirectory
      inside it is absent, so the target names a directory that does not exist;
    - ``unresolved`` — no ancestor up to the run root has the package at all; ``path`` is the
      base-dir target, unchanged.
    ``package_dir`` is the ``node_modules/<pkg>`` directory the resolution came from (None when
    nothing was found), and ``searched`` lists every candidate tested, for the log message.
    """

    path: str
    kind: Literal["local", "hoisted", "subpath_missing", "unresolved"]
    package_dir: Optional[str] = None
    searched: List[str] = field(default_factory=list)


def _split_node_modules_target(target: str) -> Optional[Tuple[str, str]]:
    """Split a bare ``node_modules/<pkg>[/<subpath>]`` target into ``(pkg, subpath)``.

    Returns None for anything else, which is what keeps the ancestor walk narrow. A target
    that spells a location before ``node_modules`` (``packages/a/node_modules/x``,
    ``../vendor/node_modules/x``) names a specific install the author chose, so it is left
    alone; only a bare ``node_modules/...`` is the node-resolution idiom that hoisting applies
    to. ``<pkg>`` takes two segments for a scoped package (``@scope/name``), one otherwise.
    """
    normalized = target.replace(os.sep, "/").replace("\\", "/")
    segments = [s for s in normalized.split("/") if s and s != "."]
    if not segments or segments[0] != _NODE_MODULES:
        return None
    rest = segments[1:]
    if not rest:
        return None
    pkg_len = 2 if rest[0].startswith("@") else 1
    if len(rest) < pkg_len:
        return None
    return "/".join(rest[:pkg_len]), "/".join(rest[pkg_len:])


def node_modules_package_root(target: str) -> Optional[str]:
    """The ``…/node_modules/<pkg>`` prefix of ``target``, or None when it names no package.

    Unlike ``_split_node_modules_target`` this accepts a target with anything in front of
    ``node_modules`` (an absolute path, a sub-project prefix), because its callers are handed a
    finished packages entry rather than an authored remapping. The *last* ``node_modules``
    segment wins: with a nested install (``a/node_modules/b/node_modules/c``) the innermost one
    is what governs the tail. ``<pkg>`` takes two segments for a scoped package (``@scope/name``),
    one otherwise; the result equals ``target`` when the target IS the package root, which is how
    a caller tells "no subdirectory was remapped" apart from one that was.
    """
    segments = [s for s in target.replace(os.sep, "/").replace("\\", "/").split("/") if s != "."]
    if _NODE_MODULES not in segments:
        return None
    index = len(segments) - 1 - segments[::-1].index(_NODE_MODULES)
    rest = segments[index + 1:]
    if not rest:
        return None
    pkg_len = 2 if rest[0].startswith("@") else 1
    if len(rest) < pkg_len:
        return None
    return "/".join(segments[: index + 1 + pkg_len])


def _ancestor_roots(base_dir: Path, run_root: Optional[Path]) -> List[str]:
    """Directories to search for a hoisted package: ``base_dir`` first, then each parent up to
    and including ``run_root``.

    The chain has a single element — today's behaviour exactly — when there is no run root or
    when ``base_dir`` is not inside it.

    Both the containment test and the walk are textual (``os.path.normpath`` /
    ``os.path.dirname``, never ``Path.resolve``), and they must stay that way *together*:
    ``BuildSystemConfig._relativize_packages`` makes every emitted package path relative with a
    textual ``Path.relative_to(project_root)``, so a resolved path (``/tmp`` → ``/private/tmp``
    on macOS) would stop matching the run root and fall back to absolute paths. ``_rebase_context``
    makes the same textual assumption with ``os.path.relpath``. Mixing the two — a step count
    taken from resolved paths, candidates composed textually — makes the walk escape the run root
    when ``base_dir`` reaches it through a symlink (a candidate above the run root names a
    directory certoraRun does not upload) and stop short of it in the opposite case (a hoisted
    package silently missed). Each candidate is therefore derived from the previous one and the
    loop ends on the run root itself.
    """
    if run_root is None:
        return [str(base_dir)]
    root = os.path.normpath(str(run_root))
    candidate = os.path.normpath(str(base_dir))
    if candidate != root and not candidate.startswith(root.rstrip(os.sep) + os.sep):
        return [str(base_dir)]
    roots = [candidate]
    while candidate != root:
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
        roots.append(candidate)
    return roots


def resolve_node_modules_target(
    target: str, base_dir: Path, run_root: Optional[Path]
) -> PackageResolution:
    """Resolve a relative remapping target, walking ancestor ``node_modules`` when needed.

    npm/yarn hoist a dependency to the highest ``node_modules`` that can satisfy every consumer,
    so a sub-project's ``node_modules/<pkg>`` frequently does not exist while the repo root's
    does. solc has no such resolver: the packages list must name the directory. This reproduces
    node's own order — nearest ``node_modules`` first, then outwards — bounded at the run root so
    the emitted path stays inside the tree certoraRun uploads.

    Only bare ``node_modules/...`` targets are walked. forge (``lib/``) and soldeer
    (``dependencies/``) do not hoist, and a sibling project's ``lib/<name>`` is routinely a
    different pin, so walking those would silently bind the wrong version.

    A target that resolves under ``base_dir`` is always returned unchanged, so the walk can only
    change an entry whose current target does not exist on disk. When the nearest package lacks
    the remapped subpath, a farther ancestor that has the whole target is preferred — a
    deliberate deviation from node, since only the full target is usable by solc.
    """
    split = _split_node_modules_target(target)
    if split is None:
        return PackageResolution(path=str(base_dir / target), kind="local")

    package, subpath = split
    searched: List[str] = []
    nearest_package_dir: Optional[str] = None

    for index, root in enumerate(_ancestor_roots(base_dir, run_root)):
        package_dir = os.path.join(root, _NODE_MODULES, package)
        full_target = os.path.join(package_dir, *subpath.split("/")) if subpath else package_dir
        searched.append(full_target)
        if not Path(package_dir).is_dir():
            continue
        if nearest_package_dir is None:
            nearest_package_dir = package_dir
        # `.exists()` rather than `.is_dir()`: a remapping may target a single source file.
        if Path(full_target).exists():
            return PackageResolution(
                path=full_target,
                kind="local" if index == 0 else "hoisted",
                package_dir=package_dir,
                searched=searched,
            )

    if nearest_package_dir is not None:
        full_target = (
            os.path.join(nearest_package_dir, *subpath.split("/")) if subpath else nearest_package_dir
        )
        return PackageResolution(
            path=full_target,
            kind="subpath_missing",
            package_dir=nearest_package_dir,
            searched=searched,
        )

    return PackageResolution(path=str(base_dir / target), kind="unresolved", searched=searched)


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

    ``run_root`` is the directory certoraRun is invoked from (the autosetup run root). It bounds
    both halves of a remapping. The *context* half must be expressed against it, because solc
    matches contexts against source unit names — see ``_rebase_context``. The *target* half of a
    bare ``node_modules/...`` entry is resolved against it too: the package is looked for in
    ``base_dir/node_modules`` first and then in each ancestor's up to the run root, which is how
    a hoisted install is found (see ``resolve_node_modules_target``). A target that resolves
    under ``base_dir`` resolves to exactly the same path either way, so with ``run_root`` equal
    to ``base_dir`` or absent the result is unchanged; pass it whenever the caller knows it.

    Priority on key conflict (highest wins, with a warning on path mismatch):
    1. ``forge remappings`` — recursively walks nested foundry.toml files (e.g. lib/*/foundry.toml)
        and emits paths relative to CWD; strictly stronger than parsing the top-level
        foundry.toml alone. Best-effort: skipped silently if forge is not installed or
        the command fails.
    2. foundry.toml — hand-curated source of truth for the build system
    3. remappings.txt — often partially auto-generated; may drift
    4. package.json — npm-style fallback

    Every *other* project under ``run_root`` contributes its own resolution too, as
    context-scoped entries (``<project-rel-path>/:prefix=target``). solc matches a context
    against the importing file's source unit name and prefers the longest match, so such an
    entry governs that project's files while the entries above stay the default for every file
    no context claims (see ``_scope_other_projects``).
    """
    remapping_key_to_path, remapping_key_to_source = _collect_remapping_entries(
        base_dir, log_fn, profile, run_root
    )
    _scope_other_projects(
        remapping_key_to_path=remapping_key_to_path,
        remapping_key_to_source=remapping_key_to_source,
        base_dir=base_dir,
        run_root=run_root,
        log_fn=log_fn,
    )
    return [f"{key}={path}" for key, path in remapping_key_to_path.items()]


def _collect_remapping_entries(
    base_dir: Path,
    log_fn: LogFn,
    profile: str,
    run_root: Optional[Path],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Read one project's four remapping sources into (key -> path, key -> source) maps.

    The sources, their priority and the meaning of ``profile`` and ``run_root`` are the ones
    ``build_packages_from_remapping_sources`` documents. The maps are handed back unformatted
    so another project's resolution can be merged into them before the list is built.
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

    return remapping_key_to_path, remapping_key_to_source


def _projects_under(base_dir: Path, run_root: Optional[Path]) -> List[Path]:
    """Every project under ``run_root`` other than ``base_dir`` and its ancestors.

    A project is a directory holding one of ``BUILD_CONFIG_FILENAMES``. The config is what
    declares the project's import resolution, and it declares it whether or not the project
    ever compiled: a sibling whose own build failed has no artifacts, and a build that failed
    for want of the right copy of a package is exactly the case these entries answer. The walk
    prunes hidden directories and the vendored-dependency names in ``DEPENDENCIES``, whose
    configs belong to a dependency rather than to the repo under analysis. A project nested
    inside another is kept: its
    context is longer, hence more specific, which is what solc should prefer for its files.

    ``base_dir``, its ancestors and the run root are excluded. An ancestor's context prefixes
    the anchor's own source unit names as well, and being longer than the empty context it
    would outrank the anchor's global entries for exactly the files those entries are for.

    Comparison is textual (``os.path.normpath``, never ``Path.resolve``), the contract
    ``_ancestor_roots`` documents and depends on.
    """
    if run_root is None:
        return []

    excluded = {os.path.normpath(root) for root in _ancestor_roots(base_dir, run_root)}
    projects: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(run_root)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in DEPENDENCIES]
        if os.path.normpath(dirpath) in excluded:
            continue
        if any(config in filenames for config in BUILD_CONFIG_FILENAMES):
            projects.append(Path(dirpath))
    return sorted(projects, key=lambda project: os.path.relpath(project, run_root))


def _prefixed_log(prefix: str, log_fn: LogFn) -> LogFn:
    """A log function tagging every message with ``prefix``, so a hoist or an unresolved
    package read out of a sibling project is attributable to that project."""

    def log(message: str, level: str) -> None:
        log_fn(f"[{prefix}] {message}", level)

    return log


def _scope_other_projects(
    *,
    remapping_key_to_path: Dict[str, str],
    remapping_key_to_source: Dict[str, str],
    base_dir: Path,
    run_root: Optional[Path],
    log_fn: LogFn,
) -> None:
    """Merge every other project's resolution in, scoped to that project's files.

    Each entry is keyed ``<project-rel-path>/:prefix``, which solc applies only to files whose
    source unit name starts with that path — so a project pinned to its own copy of a package
    keeps it while the anchor's unscoped entry stays the default everywhere else.
    """
    for project in _projects_under(base_dir, run_root):
        rel = os.path.relpath(project, run_root)
        # Siblings are read under the default profile because the reactive rebuild in
        # `compilation_workarounds` passes no profile; the two writers must produce the same
        # keys, or the first workaround to fire would rebuild the conf without the scoping.
        project_paths, _ = _collect_remapping_entries(
            base_dir=project,
            log_fn=_prefixed_log(rel, log_fn),
            profile="default",
            run_root=run_root,
        )
        for key, path in project_paths.items():
            # A key that already carries a context is one the project authored. `_rebase_context`
            # expresses such a context run-root-relative when it names a directory in the
            # project, and leaves it as authored when it names none — so only a context that
            # lands inside the project is taken as it stands. Anything else is composed under
            # the project, because a context reaching outside it would outrank the anchor's
            # shorter entries for the anchor's own files. Composing `rel` needs no rebasing,
            # since it is run-root-relative by construction.
            if ":" in key:
                context, prefix = key.split(":", 1)
                normalized = os.path.normpath(context)
                if normalized == rel or normalized.startswith(rel + os.sep):
                    scoped_key = key
                else:
                    scoped_key = f"{os.path.join(rel, context.lstrip(os.sep))}:{prefix}"
            else:
                scoped_key = f"{rel}/:{key}"
                prefix = key
            # The same target under a longer context is the same binding spelled twice.
            if remapping_key_to_path.get(prefix) == path:
                continue
            if not Path(path).exists():
                # A scoped entry outranks the global one for every file under it, so binding
                # the prefix to a directory already known to be absent would replace a
                # resolution that may work with one that cannot.
                governing = remapping_key_to_path.get(prefix)
                keeps = (
                    f"'{prefix}={governing}' keeps governing it"
                    if governing
                    else "the prefix stays unmapped"
                )
                log_fn(
                    f"Project '{rel}' maps '{prefix}' to {path}, which does not exist; {keeps}",
                    "WARNING",
                )
                continue
            _record_entry(
                key=scoped_key,
                path=path,
                source_name=f"{rel} remapping sources",
                remapping_key_to_path=remapping_key_to_path,
                remapping_key_to_source=remapping_key_to_source,
                warn_on_mismatch=False,
                log_fn=log_fn,
            )


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

    # Resolve a relative target against base_dir first (Path() drops any trailing slash), letting
    # the ancestor walk find a hoisted node_modules package (see `resolve_node_modules_target`).
    if not Path(path).is_absolute():
        resolution = resolve_node_modules_target(path, base_dir, run_root)
        if resolution.kind == "hoisted":
            log_fn(
                f"Package '{key}' is not installed under {base_dir}; resolving '{path}' to the "
                f"hoisted install at {resolution.path}",
                "INFO",
            )
        elif resolution.kind == "subpath_missing":
            log_fn(
                f"Package '{key}': {resolution.package_dir} exists but the remapped subdirectory "
                f"is missing (looked for {resolution.path}) — the remapping target may be wrong "
                f"or the package is not built",
                "WARNING",
            )
        elif resolution.kind == "unresolved":
            log_fn(
                f"Package '{key}' target {resolution.path} does not exist "
                f"(searched: {', '.join(resolution.searched)}) — the dependency is not installed "
                f"under the project or any ancestor up to the run root; keeping the entry so solc "
                f"reports the exact missing source",
                "WARNING",
            )
        path = resolution.path

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

    _record_entry(
        key=key,
        path=path,
        source_name=source_name,
        remapping_key_to_path=remapping_key_to_path,
        remapping_key_to_source=remapping_key_to_source,
        warn_on_mismatch=warn_on_mismatch,
        log_fn=log_fn,
    )


def _record_entry(
    *,
    key: str,
    path: str,
    source_name: str,
    remapping_key_to_path: Dict[str, str],
    remapping_key_to_source: Dict[str, str],
    warn_on_mismatch: bool,
    log_fn: LogFn,
) -> None:
    """Record an already-canonical key/path pair under the first-wins rule.

    On a key conflict (already populated by an earlier-priority source):
    - if ``warn_on_mismatch`` and the stored path differs from the new one, log a warning naming
      the actual earlier source from ``remapping_key_to_source``;
    - otherwise silently skip.

    Both halves are taken as given, which is what lets a caller holding a key and a target
    already in canonical form record them without composing and re-parsing an entry string.
    """
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
