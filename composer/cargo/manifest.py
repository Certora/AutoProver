"""Typed reading of a ``Cargo.toml``.

Only the tables the CVLR scaffold consults are declared. Everything else in a manifest is
accepted and ignored, so a manifest cargo takes is not refused here for a table this module
never reads.

This is for reading. Edits are text insertions into the file (:mod:`composer.spec.cvlr.scaffold`)
because reserializing a manifest would rewrite the project's comments.
"""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class MalformedManifest(RuntimeError):
    """A ``Cargo.toml`` could not be read, parsed, or does not have cargo's shape."""


class _ManifestModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Dependency(_ManifestModel):
    """One dependency entry, from ``[dependencies]``, ``[workspace.dependencies]``, or a patch
    table.

    The fields are not exclusive: cargo allows ``version`` beside ``path`` or ``git``, where the
    version is what a publish of the dependent uses.
    """

    version: str | None = None
    git: str | None = None
    path: str | None = None
    #: ``workspace = true``: the entry inherits the root's ``[workspace.dependencies]`` one.
    workspace: bool = False

    @model_validator(mode="before")
    @classmethod
    def _bare_requirement(cls, data: object) -> object:
        """``foo = "1.0"`` is cargo's shorthand for ``foo = { version = "1.0" }``."""
        return {"version": data} if isinstance(data, str) else data


class PackageTable(_ManifestModel):
    #: ``[package.metadata]`` is free-form; cargo passes it to tools without reading it.
    metadata: dict[str, object] = {}


class WorkspaceTable(_ManifestModel):
    dependencies: dict[str, Dependency] = {}


class Manifest(_ManifestModel):
    #: ``None`` for a virtual workspace manifest.
    package: PackageTable | None = None
    #: ``None`` when the manifest has no ``[workspace]``. An empty ``[workspace]`` still makes
    #: the manifest a workspace root.
    workspace: WorkspaceTable | None = None
    dependencies: dict[str, Dependency] = {}
    features: dict[str, list[str]] = {}
    #: Keyed by the source being patched (``crates-io``, or a registry or git URL), then by crate.
    patch: dict[str, dict[str, Dependency]] = {}

    @property
    def workspace_dependencies(self) -> dict[str, Dependency]:
        return self.workspace.dependencies if self.workspace is not None else {}

    @property
    def package_metadata(self) -> dict[str, object]:
        return self.package.metadata if self.package is not None else {}


_UNPARSEABLE = (tomllib.TOMLDecodeError, ValidationError)


def _validated(text: str) -> Manifest:
    return Manifest.model_validate(tomllib.loads(text))


def parse_manifest(text: str) -> Manifest:
    try:
        return _validated(text)
    except _UNPARSEABLE as exc:
        raise MalformedManifest(str(exc)) from exc


def read_manifest(path: Path) -> Manifest:
    try:
        return _validated(path.read_text())
    except (OSError, *_UNPARSEABLE) as exc:
        raise MalformedManifest(f"{path}: {exc}") from exc
