"""Cargo crate resolution — the ``rust`` language facet's "where does this source live?" lookup.

A backend that must *depend on* the code under analysis needs facts the CLI's
``--main-contract path:Name`` argument does not carry. Crucible's fuzz harness, for instance,
declares the program under test as a path dependency, which needs three of them:

* the crate's **directory** (the path dep's target),
* its Cargo **package name** (the ``[dependencies]`` key), and
* its **lib target name** (the Rust path in ``use <lib>::*``, and the basename of the built
  artifact — ``target/deploy/<lib>.so``).

None of the three is derivable from the ``Name`` half of the argument, and they are independent of
each other: ``Name`` is the *analysis* identifier (it must match the analyzed model's
``program_identifier``), while a lending program, say, lives in ``programs/lend`` as package
``example_lending``. Assuming they coincide — as the old ``programs/<Name>`` convention did — breaks
on any workspace whose directory names differ from its package names. So resolve them from the
manifest that actually owns the source file.
"""

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgramCrate:
    """The Cargo crate that contains the analyzed source (the wire shape of
    ``autoprover_sdk::ProgramCrate`` — an ``AuthorInput`` field)."""

    #: The crate directory relative to the project root, forward-slashed (``"."`` = the root).
    dir: str
    #: ``[package] name`` — the key a dependent's ``[dependencies]`` must use.
    package: str
    #: The lib target name: explicit ``[lib] name``, else the package name with ``-`` → ``_``
    #: (Cargo's default). This — not the package name — is the Rust identifier (``use <lib>::*``)
    #: and the compiled artifact's basename.
    lib: str
    #: The crate's declared ``anchor-lang`` requirement, verbatim (workspace inheritance resolved);
    #: ``""`` when it declares none. A dependent can only link the crate if this matches its own
    #: Anchor major — Anchor's generated trait impls belong to the exact ``anchor-lang`` that
    #: generated them — so it decides whether a harness can depend on the crate at all.
    anchor: str = ""


def resolve_program_crate(project_root: str | Path, relative_path: str) -> ProgramCrate | None:
    """Resolve the crate owning ``project_root/relative_path`` by walking up to the nearest
    ``Cargo.toml`` that declares a ``[package]``, stopping at the project root.

    Returns ``None`` (logged) when there is no such manifest — the source isn't in a crate under
    this root, or its manifest is unreadable/inherits its name from the workspace. Callers fall
    back to their own convention, so an unresolved crate must never raise here.
    """
    root = Path(project_root).resolve()
    src = (root / relative_path).resolve()
    if not src.is_relative_to(root):
        _log.warning("%s is not under project root %s; no crate resolved", src, root)
        return None
    for d in (src.parent, *src.parent.parents):
        crate = _read_manifest(d / "Cargo.toml", root)
        if crate is not None:
            return crate
        if d == root:
            break
    _log.warning("no Cargo.toml with a [package] between %s and %s", src, root)
    return None


def _read_manifest(manifest: Path, root: Path) -> ProgramCrate | None:
    """The crate ``manifest`` describes, or ``None`` if it isn't one (missing, unparseable, or a
    virtual manifest — a ``[workspace]`` with no ``[package]``, which every Cargo workspace root
    has and which the walk must step over)."""
    if not manifest.is_file():
        return None
    try:
        data = tomllib.loads(manifest.read_text())
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        _log.warning("cannot read %s: %s", manifest, exc)
        return None
    package = _table_name(data.get("package"))
    if package is None:
        # Virtual manifest, or `name.workspace = true` (a table, not a name we can use).
        return None
    return ProgramCrate(
        dir=manifest.parent.relative_to(root).as_posix(),
        package=package,
        lib=_table_name(data.get("lib")) or package.replace("-", "_"),
        anchor=_dep_req(data, "anchor-lang", manifest.parent, root),
    )


def _table_name(table: object) -> str | None:
    """The ``name`` of a manifest table, when it is a table carrying a string ``name``."""
    if isinstance(table, dict) and isinstance(name := table.get("name"), str):
        return name
    return None


def _dep_req(data: dict, dep: str, crate_dir: Path, root: Path) -> str:
    """``dep``'s version requirement as declared in ``data``'s ``[dependencies]``, or ``""``.

    Handles the three spellings a member crate uses: a bare string, a table with ``version``, and
    ``{ workspace = true }`` — which is resolved through the workspace root's
    ``[workspace.dependencies]``. A git/path dependency has no version requirement, so it reads as
    ``""`` (unknown) rather than a version we'd compare wrongly."""
    spec = (data.get("dependencies") or {}).get(dep)
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return ""
    if spec.get("workspace") is True:
        inherited = _workspace_deps(crate_dir, root).get(dep)
        if isinstance(inherited, str):
            return inherited
        spec = inherited if isinstance(inherited, dict) else {}
    version = spec.get("version")
    return version if isinstance(version, str) else ""


def _workspace_deps(crate_dir: Path, root: Path) -> dict:
    """``[workspace.dependencies]`` of the workspace owning ``crate_dir`` — the nearest ancestor
    manifest (up to ``root``) that declares a ``[workspace]``. Empty if there is none."""
    for d in (crate_dir, *crate_dir.parents):
        manifest = d / "Cargo.toml"
        if manifest.is_file():
            try:
                data = tomllib.loads(manifest.read_text())
            except (OSError, ValueError):
                data = {}
            if isinstance(ws := data.get("workspace"), dict):
                return ws.get("dependencies") or {}
        if d == root:
            break
    return {}
