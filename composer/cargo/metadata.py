"""Reading a Cargo workspace: ``cargo metadata``, typed.

Two questions are asked of a target project, and both have to be answered by the host rather than
guessed by an agent:

* **Which crate owns this source file** — the ``source_unit`` half of
  :mod:`composer.rustapp.toolchain`, and what a build command needs in order to name a package.
* **Which version of a dependency does this build actually resolve** — ``docs/cvlr-backend-plan.md``
  §5.5 mechanic 3. ``RUST_FORBIDDEN_READ`` hides ``Cargo.lock`` from the agents, deliberately, so
  the resolved version is not something they *can* see; and reading a different CVLR's source than
  the build compiles is worse than reading none, because it is confidently wrong.

``cargo metadata`` runs no build scripts and no proc-macros, so it needs no confinement — unlike
everything in :mod:`composer.cargo.session`. It does resolve the dependency graph, which wants
either a warm cache or the network; see :func:`read_workspace`'s ``offline``.
"""

import asyncio
import dataclasses
import json
import logging
import shutil
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

#: Cargo crate types that make a target *the* library of its package — the thing a `.so` is built
#: from and the thing `extern crate` names. A package may also carry bins, tests and examples;
#: those are never what a verification build targets.
_LIB_CRATE_TYPES = frozenset({"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"})

#: How long `cargo metadata` may take. Generous: on a cold cache it resolves (and may download) the
#: whole dependency graph. A hang here would otherwise block a run with no diagnostic.
METADATA_TIMEOUT_S = 300


class CargoUnavailable(RuntimeError):
    """``cargo`` is not on ``PATH``.

    Distinct from "this project is not a Cargo project": one is a broken machine and the other is a
    fact about the target, and only the first is worth telling an operator to fix."""


@dataclasses.dataclass(frozen=True)
class CratePackage:
    """One package in the resolved graph.

    ``lib_target`` is the ``[lib]`` target's name, which is *not* always the package name — cargo
    lets them differ, and it is the target name that the built artifact is named after (with ``-``
    normalized to ``_``). ``None`` for a package with no library target.
    """

    name: str
    version: str
    manifest_path: Path
    lib_target: str | None
    features: tuple[str, ...]
    #: ``None`` for a workspace member or a path dependency; the registry/git URL for a fetched one.
    #: The distinction is what separates "code in this project" from "code the project depends on",
    #: which is the line every source-mounting and source-collecting decision is drawn on.
    source: str | None

    @property
    def root(self) -> Path:
        """The package directory — where its ``Cargo.toml`` lives."""
        return self.manifest_path.parent

    @property
    def is_local(self) -> bool:
        return self.source is None

    @property
    def artifact_stem(self) -> str | None:
        """The file stem cargo gives this package's library artifact (``<stem>.so`` for sbf)."""
        return self.lib_target.replace("-", "_") if self.lib_target is not None else None


@dataclasses.dataclass(frozen=True)
class Workspace:
    """A resolved Cargo workspace: its root, its members, and every package in the graph."""

    root: Path
    target_directory: Path
    #: Workspace members, in the order cargo lists them.
    members: tuple[CratePackage, ...]
    #: Every package the graph resolves, members included — the dependency versions this build uses.
    packages: tuple[CratePackage, ...]

    def owning(self, path: Path) -> CratePackage | None:
        """The member whose directory contains ``path`` — the *deepest* one, so a nested crate wins
        over the workspace-root package that also contains it.

        ``None`` when nothing does, which is a real answer: a file outside every member (a script, a
        doc, a path outside the workspace) has no owning crate, and the seam this feeds documents an
        empty answer as "apply your own convention"."""
        try:
            resolved = path.resolve()
        except OSError:
            return None
        containing = [m for m in self.members if resolved.is_relative_to(m.root)]
        return max(containing, key=lambda m: len(m.root.parts)) if containing else None

    def member(self, name: str) -> CratePackage | None:
        return next((m for m in self.members if m.name == name), None)

    def resolved(self, name: str) -> CratePackage | None:
        """The version of ``name`` this build compiles against, wherever it came from."""
        return next((p for p in self.packages if p.name == name), None)

    def family(self, prefix: str) -> tuple[CratePackage, ...]:
        """Every resolved package named ``prefix`` or ``prefix-*``.

        Crate families are named by convention rather than declared anywhere (``cvlr`` pulls in
        ``cvlr-asserts``, ``cvlr-log``, ``cvlr-nondet``, …), and a caller that wants "the CVLR
        sources" wants all of them, not the one the manifest happens to name."""
        return tuple(
            p for p in self.packages if p.name == prefix or p.name.startswith(f"{prefix}-")
        )


def _package(raw: dict) -> CratePackage:
    lib = next(
        (
            t["name"]
            for t in raw.get("targets", ())
            if _LIB_CRATE_TYPES.intersection(t.get("crate_types") or t.get("kind") or ())
        ),
        None,
    )
    return CratePackage(
        name=raw["name"],
        version=raw["version"],
        manifest_path=Path(raw["manifest_path"]),
        lib_target=lib,
        features=tuple(sorted(raw.get("features") or {})),
        source=raw.get("source"),
    )


def parse_metadata(payload: dict) -> Workspace:
    """Build a :class:`Workspace` from ``cargo metadata --format-version 1`` output.

    Split out from running cargo so the parse is testable against a recorded payload — the shape is
    cargo's, and a change in it should fail a unit test rather than an integration one."""
    by_id = {raw["id"]: _package(raw) for raw in payload["packages"]}
    member_ids = payload.get("workspace_members") or ()
    root = Path(payload["workspace_root"])
    return Workspace(
        root=root,
        target_directory=Path(payload.get("target_directory") or root / "target"),
        members=tuple(by_id[i] for i in member_ids if i in by_id),
        packages=tuple(by_id.values()),
    )


def _cargo_metadata(project_root: Path, *, offline: bool, timeout_s: int) -> dict | None:
    """Run ``cargo metadata`` and return its payload, or ``None`` with the reason logged."""
    if shutil.which("cargo") is None:
        raise CargoUnavailable(
            "cargo is not on PATH; a Rust chain's toolchain cannot be resolved without it"
        )
    args = ["cargo", "metadata", "--format-version", "1"]
    if offline:
        args.append("--offline")
    try:
        completed = subprocess.run(
            args, cwd=project_root, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        _log.warning("cargo metadata in %s timed out after %ss", project_root, timeout_s)
        return None
    except OSError as exc:
        _log.warning("cargo metadata in %s could not run: %r", project_root, exc)
        return None
    if completed.returncode != 0:
        _log.info(
            "cargo metadata in %s failed (%s): %s",
            project_root,
            completed.returncode,
            completed.stderr.strip(),
        )
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        _log.warning("cargo metadata in %s printed unreadable JSON: %r", project_root, exc)
        return None


def read_workspace_sync(
    project_root: Path, *, offline: bool = False, timeout_s: int = METADATA_TIMEOUT_S
) -> Workspace | None:
    """The workspace containing ``project_root``, or ``None`` when there is none to read.

    ``None`` covers every way a project can decline to be a Cargo workspace — no manifest, an
    unparseable one, a graph that will not resolve — because the caller's next move is the same in
    all of them, and :func:`composer.rustapp.toolchain.source_unit` documents an empty answer as a
    supported state. The cargo diagnostic is logged rather than raised for the same reason. A
    *missing cargo*, by contrast, raises: that is the machine being wrong, not the project.

    ``offline`` passes ``--offline``, which refuses to touch the network and therefore fails on a
    graph that is not already fetched. Pass it where a warm cache is guaranteed (inside a session
    that has warmed) and leave it off for the first read of an unseen project.

    Synchronous because :meth:`composer.rustapp.toolchain.ProjectToolchain.source_unit` is, and a
    blocking primitive with an async wrapper is the only arrangement that serves both callers
    without one of them running an event loop it does not have.
    """
    payload = _cargo_metadata(project_root, offline=offline, timeout_s=timeout_s)
    if payload is None:
        return None
    try:
        return parse_metadata(payload)
    except KeyError as exc:
        _log.warning("cargo metadata in %s omitted %s", project_root, exc)
        return None


async def read_workspace(
    project_root: Path, *, offline: bool = False, timeout_s: int = METADATA_TIMEOUT_S
) -> Workspace | None:
    """:func:`read_workspace_sync`, off the event loop."""
    return await asyncio.to_thread(
        read_workspace_sync, project_root, offline=offline, timeout_s=timeout_s
    )
