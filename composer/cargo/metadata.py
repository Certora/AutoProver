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
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

_log = logging.getLogger(__name__)

#: Crate types that make a target the library of its package. Bins, tests, and
#: examples are never the verification target.
_LIB_CRATE_TYPES = frozenset({"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"})

#: On a cold cache this resolves (and may download) the whole dependency graph.
METADATA_TIMEOUT_S = 300


class CargoUnavailable(RuntimeError):
    """``cargo`` is not on ``PATH``."""


class _CargoJson(BaseModel):
    """cargo adds fields across releases; only the ones read here are declared."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class CargoTargetJson(_CargoJson):
    name: str
    src_path: Path
    #: Older cargos report only ``kind``.
    crate_types: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("crate_types", "kind")
    )


class CargoPackageJson(_CargoJson):
    id: str
    name: str
    version: str
    manifest_path: Path
    targets: tuple[CargoTargetJson, ...] = ()
    features: dict[str, list[str]] = {}
    source: str | None = None


class CargoMetadataJson(_CargoJson):
    """The parts of ``cargo metadata --format-version 1`` output this module reads."""

    packages: tuple[CargoPackageJson, ...]
    workspace_root: Path
    workspace_members: tuple[str, ...] = ()
    target_directory: Path | None = None


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
class GitBranch:
    name: str


@dataclass(frozen=True)
class GitTag:
    name: str


@dataclass(frozen=True)
class GitRev:
    rev: str


type GitReference = GitBranch | GitTag | GitRev


@dataclass(frozen=True)
class GitSource:
    """A package from a git repository.

    cargo spells it ``git+<url>[?branch=<b>|?tag=<t>|?rev=<r>][#<commit>]``.
    """

    spelling: str
    #: The repository URL as the manifest named it, without the reference or the commit.
    repository: str
    #: ``None`` when the manifest names no reference and cargo follows the default branch.
    reference: GitReference | None
    #: The commit the build resolved to.
    commit: str | None

    @classmethod
    def parse(cls, spelling: str) -> "GitSource":
        parts = urlsplit(spelling.removeprefix("git+"))
        query = parse_qs(parts.query)
        reference: GitReference | None = None
        if branch := query.get("branch"):
            reference = GitBranch(branch[0])
        elif tag := query.get("tag"):
            reference = GitTag(tag[0])
        elif rev := query.get("rev"):
            reference = GitRev(rev[0])
        return cls(
            spelling=spelling,
            repository=urlunsplit(parts._replace(query="", fragment="")),
            reference=reference,
            commit=parts.fragment or None,
        )

    def is_from(self, repository: str) -> bool:
        """Whether this is ``repository``, however the two URLs spell it.

        The scheme, a user, a trailing ``.git`` or ``/``, and case are ignored: ``https://`` and
        ``ssh://git@`` reach the same repository, and GitHub paths are case-insensitive.
        """
        return _repository_key(self.repository) == _repository_key(repository)


def _repository_key(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix(".git")
    return (parts.hostname or "", path.lower())


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
        return GitSource.parse(spelling)
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
    for target in raw.targets:
        if _LIB_CRATE_TYPES.intersection(target.crate_types):
            return LibTarget(
                name=target.name, src_path=target.src_path, crate_types=target.crate_types
            )
    return None


def _package(raw: CargoPackageJson) -> CratePackage:
    return CratePackage(
        name=raw.name,
        version=raw.version,
        manifest_path=raw.manifest_path,
        lib=_lib_target(raw),
        features=tuple(sorted(raw.features)),
        source=_package_source(raw.source),
    )


def parse_metadata(payload: CargoMetadataJson) -> Workspace:
    """Build a :class:`Workspace` from validated ``cargo metadata --format-version 1`` output."""
    by_id = {raw.id: _package(raw) for raw in payload.packages}
    root = payload.workspace_root
    return Workspace(
        root=root,
        target_directory=payload.target_directory or root / "target",
        members=tuple(by_id[i] for i in payload.workspace_members if i in by_id),
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
        return CargoMetadataJson.model_validate_json(completed.stdout)
    except ValidationError as exc:
        _log.warning("cargo metadata in %s printed unreadable output: %s", project_root, exc)
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
    return parse_metadata(payload) if payload is not None else None


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
