"""Reading and editing a ``Cargo.toml``.

Only the tables the CVLR scaffold consults are declared. Everything else in a manifest is
accepted and ignored, so a manifest cargo takes is not refused here for a table this module
never reads.

Parsing is ``tomlkit``'s for reading and editing alike, so what is validated is what is edited.
:class:`ManifestEditor` keeps everything it does not change as it was, comments included.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import InlineTable, Item, Table

from composer.cargo.features import CargoFeature


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
    features: dict[CargoFeature, list[str]] = {}
    #: Keyed by the source being patched (``crates-io``, or a registry or git URL), then by crate.
    patch: dict[str, dict[str, Dependency]] = {}

    @property
    def workspace_dependencies(self) -> dict[str, Dependency]:
        return self.workspace.dependencies if self.workspace is not None else {}

    @property
    def package_metadata(self) -> dict[str, object]:
        return self.package.metadata if self.package is not None else {}


_UNPARSEABLE = (TOMLKitError, ValidationError)


def _parsed(text: str) -> tuple[tomlkit.TOMLDocument, Manifest]:
    document = tomlkit.parse(text)
    return document, Manifest.model_validate(document.unwrap())


def parse_manifest(text: str) -> Manifest:
    return ManifestEditor(text).manifest


def read_manifest(path: Path) -> Manifest:
    return ManifestEditor.read(path).manifest


#: A table's name, one key per level: ``("package", "metadata", "certora")``.
type TablePath = tuple[str, ...]

#: A value :class:`ManifestEditor` writes. A mapping is written as an inline table.
type TomlValue = str | bool | Sequence[str] | Mapping[str, str | bool]


@dataclass(frozen=True)
class Comment:
    """A comment line inside a table :class:`AddTable` creates."""

    text: str


type TableItem = tuple[str, TomlValue] | Comment


@dataclass(frozen=True)
class AddEntries:
    """Keys added to ``table``, which is created when the manifest has none."""

    table: TablePath
    entries: tuple[tuple[str, TomlValue], ...]

    def describe(self) -> str:
        return f"[{'.'.join(self.table)}] {', '.join(key for key, _ in self.entries)}"


@dataclass(frozen=True)
class AddTable:
    """A table the manifest does not have yet, created with ``body`` in order. The tables above
    it are created as needed, without headers of their own."""

    table: TablePath
    body: tuple[TableItem, ...]

    def describe(self) -> str:
        return f"[{'.'.join(self.table)}]"


type ManifestAddition = AddEntries | AddTable


class ManifestConflict(ValueError):
    """An addition collides with what the manifest already has."""


class ManifestEditor:
    """One ``Cargo.toml``, parsed once, to read and to add to.

    Edits only add. A new table is built whole before it is placed, which is what keeps its keys
    together and its spacing like the tables around it.
    """

    def __init__(self, text: str, *, origin: str = "Cargo.toml") -> None:
        """``origin`` names the file in a :class:`MalformedManifest`."""
        try:
            self._document, self.manifest = _parsed(text)
        except _UNPARSEABLE as exc:
            raise MalformedManifest(f"{origin}: {exc}") from exc
        self._original = text

    @classmethod
    def read(cls, path: Path) -> "ManifestEditor":
        try:
            text = path.read_text()
        except OSError as exc:
            raise MalformedManifest(f"{path}: {exc}") from exc
        return cls(text, origin=str(path))

    def apply(self, addition: ManifestAddition, *, note: str) -> None:
        """Make ``addition``. ``note`` is a comment on each added line, or once on a new table's
        header. Raises :class:`ManifestConflict` when a key or table it adds is already there."""
        match addition:
            case AddEntries(table=table, entries=entries):
                existing = self._find(table)
                if existing is None:
                    self._add_table(table, entries, note=note)
                else:
                    for key, value in entries:
                        _insert(existing, table, key, value, note=note)
            case AddTable(table=table, body=body):
                self._add_table(table, body, note=note)

    def text(self) -> str:
        """The manifest with the edits, ending in as many newlines as it did."""
        edited = tomlkit.dumps(self._document)
        ending = len(self._original) - len(self._original.rstrip("\n"))
        return edited.rstrip("\n") + "\n" * ending

    def _add_table(self, table: TablePath, body: Sequence[TableItem], *, note: str) -> None:
        if self._find(table) is not None:
            raise ManifestConflict(f"[{'.'.join(table)}] already exists")
        parent: tomlkit.TOMLDocument | Table = self._document
        for name in table[:-1]:
            if name not in parent:
                parent.add(name, tomlkit.table(is_super_table=True))
            found = parent[name]
            if not isinstance(found, Table):
                raise ManifestConflict(f"{name} in [{'.'.join(table)}] is not a table")
            parent = found
        created = tomlkit.table()
        created.comment(note)
        for item in body:
            match item:
                case Comment(text=text):
                    created.add(tomlkit.comment(text))
                case (key, value):
                    created.add(key, _item(value))
        created.add(tomlkit.nl())
        parent.add(table[-1], created)

    def _find(self, table: TablePath) -> Table | InlineTable | None:
        container: object = self._document
        for name in table:
            if not isinstance(container, tomlkit.TOMLDocument | Table | InlineTable):
                return None
            if name not in container:
                return None
            container = container[name]
        return container if isinstance(container, Table | InlineTable) else None


def _insert(
    container: Table | InlineTable, table: TablePath, key: str, value: TomlValue, *, note: str
) -> None:
    if key in container:
        raise ManifestConflict(f"[{'.'.join(table)}] already has {key}")
    container[key] = _item(value)
    container[key].comment(note)


def _item(value: TomlValue) -> Item:
    if isinstance(value, Mapping):
        inline = tomlkit.inline_table()
        inline.update(value)
        return inline
    return tomlkit.item(value)
