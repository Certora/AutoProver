"""Typed ``cargo metadata`` for a Cargo workspace.

Answers two questions the host has to answer; an agent must not guess them:

* Which crate owns a source file — the ``source_unit`` half of
  :mod:`composer.rustapp.toolchain`, and the package name a build command needs.
* Which version of a dependency this build resolves. ``RUST_FORBIDDEN_READ``
  hides ``Cargo.lock`` from agents. Reading a different CVLR than the build
  compiles is worse than reading none.

``cargo metadata`` runs no build scripts and no proc-macros, so it needs no
confinement — unlike :mod:`composer.cargo.session`. It does resolve the
dependency graph, which needs a warm cache or the network; see
:func:`read_workspace`'s ``offline``.
"""

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

_log = logging.getLogger(__name__)

#: Crate types that make a target the library of its package. Bins, tests, and
#: examples are never the verification target.
_LIB_CRATE_TYPES = frozenset({"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"})

#: On a cold cache this resolves (and may download) the whole dependency graph.
METADATA_TIMEOUT_S = 300


class CargoUnavailable(RuntimeError):
    """``cargo`` is not on ``PATH``."""


class CargoTargetJson(TypedDict):
    name: str
    src_path: str
    #: Older cargos report only ``kind``.
    crate_types: NotRequired[list[str]]
    kind: NotRequired[list[str]]


class CargoPackageJson(TypedDict):
    id: str
    name: str
    version: str
    manifest_path: str
    targets: NotRequired[list[CargoTargetJson]]
    features: NotRequired[dict[str, list[str]]]
    source: NotRequired[str | None]


class CargoMetadataJson(TypedDict):
    """The parts of ``cargo metadata --format-version 1`` output this module reads."""

    packages: list[CargoPackageJson]
    workspace_root: str
    workspace_members: NotRequired[list[str]]
    target_directory: NotRequired[str]


@dataclass(frozen=True)
class LibTarget:
    """A package's library target.

    ``name`` is not always the package name; cargo allows them to differ, and
    the artifact is named after the target. ``src_path`` comes from cargo, not
    the ``src/lib.rs`` convention, because ``[lib] path`` can move it.
    """

    name: str
    src_path: Path
    crate_types: tuple[str, ...]

    @property
    def artifact_stem(self) -> str:
        return self.name.replace("-", "_")

    @property
    def builds_shared_object(self) -> bool:
        """Whether this target produces the loadable object the Solana prover needs.

        An ``rlib``-only package compiles and produces nothing to verify.
        """
        return "cdylib" in self.crate_types


@dataclass(frozen=True)
class RegistrySource:
    """A package from a ``registry+`` index. ``spelling`` is cargo's, verbatim."""

    spelling: str


@dataclass(frozen=True)
class GitSource:
    """A package from a git repository, spelled
    ``git+<url>?branch=<branch>#<commit>`` by cargo."""

    spelling: str


@dataclass(frozen=True)
class OtherSource:
    """A source kind not distinguished here, such as an alternative registry's
    ``sparse+`` index."""

    spelling: str


type PackageSource = RegistrySource | GitSource | OtherSource


def _package_source(spelling: str | None) -> PackageSource | None:
    if spelling is None:
        return None
    if spelling.startswith("registry+"):
        return RegistrySource(spelling)
    if spelling.startswith("git+"):
        return GitSource(spelling)
    return OtherSource(spelling)


@dataclass(frozen=True)
class CratePackage:
    name: str
    version: str
    manifest_path: Path
    lib: LibTarget | None
    features: tuple[str, ...]
    #: ``None`` for a workspace member or a path dependency.
    source: PackageSource | None

    @property
    def root(self) -> Path:
        return self.manifest_path.parent

    @property
    def is_local(self) -> bool:
        return self.source is None


@dataclass(frozen=True)
class Workspace:
    root: Path
    target_directory: Path
    members: tuple[CratePackage, ...]
    #: Every package the graph resolves, members included.
    packages: tuple[CratePackage, ...]

    def owning(self, path: Path) -> CratePackage | None:
        """The member whose directory contains ``path``. Deepest match, so a nested
        crate wins over the workspace-root package that also contains it."""
        try:
            resolved = path.resolve()
        except OSError:
            return None
        containing = [m for m in self.members if resolved.is_relative_to(m.root)]
        return max(containing, key=lambda m: len(m.root.parts)) if containing else None

    def member(self, name: str) -> CratePackage | None:
        return next((m for m in self.members if m.name == name), None)

    def resolved(self, name: str) -> CratePackage | None:
        """The version of ``name`` this build compiles against, member or not."""
        return next((p for p in self.packages if p.name == name), None)

    def family(self, prefix: str) -> tuple[CratePackage, ...]:
        """Every resolved package named ``prefix`` or ``prefix-*``.

        Crate families are a naming convention, not a cargo feature (``cvlr``
        pulls in ``cvlr-asserts``, ``cvlr-log``, …).
        """
        return tuple(
            p for p in self.packages if p.name == prefix or p.name.startswith(f"{prefix}-")
        )


def _lib_target(raw: CargoPackageJson) -> LibTarget | None:
    for target in raw.get("targets", ()):
        kinds = tuple(target.get("crate_types") or target.get("kind") or ())
        if _LIB_CRATE_TYPES.intersection(kinds):
            return LibTarget(
                name=target["name"], src_path=Path(target["src_path"]), crate_types=kinds
            )
    return None


def _package(raw: CargoPackageJson) -> CratePackage:
    return CratePackage(
        name=raw["name"],
        version=raw["version"],
        manifest_path=Path(raw["manifest_path"]),
        lib=_lib_target(raw),
        features=tuple(sorted(raw.get("features") or {})),
        source=_package_source(raw.get("source")),
    )


def parse_metadata(payload: CargoMetadataJson) -> Workspace:
    """Build a :class:`Workspace` from ``cargo metadata --format-version 1`` output."""
    by_id = {raw["id"]: _package(raw) for raw in payload["packages"]}
    member_ids = payload.get("workspace_members") or ()
    root = Path(payload["workspace_root"])
    return Workspace(
        root=root,
        target_directory=Path(payload.get("target_directory") or root / "target"),
        members=tuple(by_id[i] for i in member_ids if i in by_id),
        packages=tuple(by_id.values()),
    )


def _cargo_metadata(
    project_root: Path, *, offline: bool, features: tuple[str, ...], timeout_s: int
) -> CargoMetadataJson | None:
    if shutil.which("cargo") is None:
        raise CargoUnavailable(
            "cargo is not on PATH; a Rust chain's toolchain cannot be resolved without it"
        )
    args = ["cargo", "metadata", "--format-version", "1"]
    if offline:
        args.append("--offline")
    if features:
        args += ["--features", ",".join(features)]
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
        _log.warning(
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
    project_root: Path,
    *,
    offline: bool = False,
    features: tuple[str, ...] = (),
    timeout_s: int = METADATA_TIMEOUT_S,
) -> Workspace | None:
    """The workspace containing ``project_root``, or ``None`` if there is none.

    ``None`` covers no manifest, an unparseable one, or a graph that will not
    resolve — :func:`composer.rustapp.toolchain.source_unit` treats an empty
    answer as a supported state. The cargo diagnostic is logged, not raised.
    Missing cargo raises: that is a machine problem, not a project problem.

    ``offline`` passes ``--offline``. Pass it when a warm cache is guaranteed;
    leave it off for the first read of an unseen project.

    ``features`` selects the graph the verification build resolves. Pass it
    whenever the answer is about CVLR. A scaffolded project marks CVLR crates
    ``optional = true`` behind the ``certora`` feature; a default-feature read
    then reports them as absent. Features resolve against the package cargo
    considers current, so pass the package directory as ``project_root`` when
    naming one.
    """
    payload = _cargo_metadata(
        project_root, offline=offline, features=features, timeout_s=timeout_s
    )
    if payload is None:
        return None
    try:
        return parse_metadata(payload)
    except KeyError as exc:
        _log.warning("cargo metadata in %s omitted %s", project_root, exc)
        return None


async def read_workspace(
    project_root: Path,
    *,
    offline: bool = False,
    features: tuple[str, ...] = (),
    timeout_s: int = METADATA_TIMEOUT_S,
) -> Workspace | None:
    """:func:`read_workspace_sync`, off the event loop."""
    return await asyncio.to_thread(
        read_workspace_sync,
        project_root,
        offline=offline,
        features=features,
        timeout_s=timeout_s,
    )
