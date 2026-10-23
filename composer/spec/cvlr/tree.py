"""The one working tree, and reconciling it to what the staging unit's state says it should contain.

A unit is one batch of a run's properties, whose rules are one harness module in the program's
crate. Every unit of a run builds in one shared copy of the project, so the dependency graph is
compiled once per run rather than once per unit. Sharing is safe only because each unit's harness module is
declared behind its own cargo feature, which the caller arranges before the tree is made: a module
behind a disabled ``cfg`` is never compiled, so a sibling's draft cannot break or dirty a build.
What this module does is make the tree **derived**:

.. code-block:: text

    tree = the project + each unit's draft + the staging unit's edits to the program

Every term after the first is in the run's checkpointed state, so the tree is a build cache rather
than state. Each run starts it fresh, and losing it costs a rebuild and never correctness, which is
what lets a resumed run, or one replayed from cache, pick up where it left off.

**Only the staging unit's edits are in the tree.** A unit's
edits to the program are whole files — its overlay — and two units' copies of one file have no
defined union. They do not need one: the tree is only ever read by a build, and every build holds the
run's build permit from staging until the Prover has uploaded what it built, so each build has the
tree to itself and the tree can hold the building unit's copy of each edited file. A path the next
unit's overlay does not serve is put back to the project's copy, which is how a sibling's edit
reverts.

Writes are content-compared. Cargo fingerprints on mtime, so rewriting a file with identical bytes
forces a rebuild; comparing first is what keeps restaging the same unit from recompiling what nothing
changed. Alternating units that edit files does rebuild the program crate, and that is the price of
this arrangement: on klend, about 20s per SBF build and 1s per ``cargo check``, the same rebuild a
unit pays whenever its own draft changed.

The tree is a graphcore :class:`~graphcore.tools.vfs.PersistentMaterializer`: the project is its
base, copied once per run by :meth:`SharedTree.materialize`, and the drafts and the staging unit's
edited files are one overlay above it.
"""

import asyncio
import functools
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath

from graphcore.tools.vfs import DictBackend, DirBackend, PersistentMaterializer

from composer.cargo.metadata import Workspace
from composer.layout import RUST_BUILD_DIRS
from composer.spec.gen_types import CVLR_DELIVERABLE_DIR

#: Directories the working tree is never filled from, wherever they sit in the project.
#:
#: :data:`~composer.layout.RUST_BUILD_DIRS` are build output, which is what the agent's source tools
#: withhold too. The rest are this backend's: ``.git`` is neither needed nor ours to duplicate,
#: ``certora_out`` is the prover's output, and ``.cvlr_work`` is this backend's work directory.
#: Matched at any depth because a workspace member can have its own ``target``, at the cost of also
#: skipping a source directory named like one of these.
NOT_PROJECT_SOURCE = (*RUST_BUILD_DIRS, ".git", "certora_out", ".cvlr_work")

#: Project-relative directories the working tree is never filled from: this backend's
#: deliverables. Matched only at the project root, since ``certora`` also names the harness
#: directory the scaffold puts in the package's ``src``.
NOT_PROJECT_SOURCE_AT_ROOT = (CVLR_DELIVERABLE_DIR,)


def _not_project_source(directory: PurePath) -> bool:
    """Whether a project-relative directory is, or is under, one the tree is never filled from."""
    return any(d in NOT_PROJECT_SOURCE for d in directory.parts) or any(
        directory.is_relative_to(d) for d in NOT_PROJECT_SOURCE_AT_ROOT
    )


def _relative_key(relative: str) -> str | None:
    """``relative`` as a project-relative path, or None if it is not one.

    Absolute paths and paths with ``..`` are not. ``..`` collapses to a different string than the
    one a later removal unlinks, so the overlay is keyed only by the path that is written. ``.`` is
    collapsed by :class:`PurePosixPath`.
    """
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    return path.as_posix()


@dataclass(frozen=True)
class ProjectSource:
    """The files an edit to the program may touch: the sources and manifests of the project's own
    crates.

    An allowlist, taken from cargo rather than inferred from directory names. A library is the
    project's when cargo resolves its package from a path — a workspace member or a path dependency —
    and its sources are the directory holding its crate root. A dependency cargo fetched is never
    the project's, wherever its source is unpacked; under confinement that is inside the tree, in
    the run's private ``CARGO_HOME``.

    The manifests are there because an edit inside a local dependency crate is gated on a feature
    that crate's manifest does not declare, so the same edit has to declare it there and forward it
    from the program's manifest.

    A crate whose root sits at its package root makes the whole package its source directory, build
    output included, so :data:`NOT_PROJECT_SOURCE` and :data:`NOT_PROJECT_SOURCE_AT_ROOT` are refused
    inside a source directory as well.
    """

    #: Project-relative.
    roots: tuple[PurePosixPath, ...]
    #: Project-relative ``Cargo.toml`` paths of the same packages.
    manifests: tuple[PurePosixPath, ...] = ()

    @classmethod
    def of(cls, workspace: Workspace, project: Path) -> "ProjectSource":
        """The source directories and manifests of ``workspace``'s local libraries that are inside
        ``project``."""
        project = project.resolve()
        roots: set[PurePosixPath] = set()
        manifests: set[PurePosixPath] = set()
        for package in workspace.packages:
            if not package.is_local or package.lib is None:
                continue
            directory = package.lib.src_path.parent.resolve()
            if directory.is_relative_to(project):
                roots.add(PurePosixPath(directory.relative_to(project).as_posix()))
            manifest = package.manifest_path.resolve()
            if manifest.is_relative_to(project):
                manifests.add(PurePosixPath(manifest.relative_to(project).as_posix()))
        return cls(roots=tuple(sorted(roots)), manifests=tuple(sorted(manifests)))

    def contains(self, relative: PurePosixPath | str) -> bool:
        """Whether a project-relative path names a file of one of the project's own crates.

        A path that is absolute or contains ``..`` does not. ``is_relative_to`` does not collapse
        ``..``, so ``src/../../other`` would otherwise count as inside ``src``.
        """
        key = _relative_key(str(relative))
        if key is None:
            return False
        path = PurePosixPath(key)
        return any(path.is_relative_to(root) for root in self.roots) and not _not_project_source(
            path.parent
        )

    def may_edit(self, relative: PurePosixPath | str) -> bool:
        """Whether an edit to the program may write this project-relative path."""
        key = _relative_key(str(relative))
        if key is None:
            return False
        path = PurePosixPath(key)
        return self.contains(path) or path in self.manifests


@dataclass(frozen=True)
class NotProjectSource:
    """The path is inside the project but is neither the source nor the manifest of one of its
    crates.

    Almost always a dependency: confinement puts the run's ``CARGO_HOME`` under
    ``.certora_internal/sandbox/cargo``, so every crate the build resolves has its unpacked source
    in the tree, a few directories away from the program.
    """

    path: str
    source: ProjectSource

    def describe(self) -> str:
        roots = ", ".join(str(r) for r in self.source.roots) or "none"
        return (
            f"{self.path} is not in the source of any of the project's own crates "
            f"(their source directories: {roots}), nor one of their manifests"
        )


def _overlap(pristine: Path, root: Path) -> str | None:
    """Why ``root`` cannot hold the tree of ``pristine``, or None when the two are disjoint.

    The tree is deleted and refilled from the project at the start of every run. A tree that is the
    project or contains it would have that delete remove the project. A tree inside the project
    would be copied into itself, and its delete would remove whatever source shares its path.
    """
    project = pristine.resolve()
    tree = root.resolve()
    if project.is_relative_to(tree):
        return f"the working tree {root} is or contains the project {pristine}"
    if tree.is_relative_to(project):
        return f"the working tree {root} is inside the project {pristine}"
    return None


def _NOT_MATERIALIZED(path: PurePath) -> bool:
    """Whether a tree-relative path is, or is under, a directory the tree is never filled from.

    Asked of directories as well as files, which is what lets the copy skip a directory whole.
    """
    return _not_project_source(path)


@dataclass(frozen=True)
class NotInProject:
    """The path resolves outside the project."""

    path: str

    def describe(self) -> str:
        return f"{self.path} resolves outside the project"


@dataclass(frozen=True)
class NotProjectRelative:
    """The path stays in the project but is not the project-relative path of one file.

    A key with ``..`` is a different string from the file it writes. The overlay is removed by
    that string, so the key has to be the path itself.
    """

    path: str

    def describe(self) -> str:
        return f"{self.path} is not a project-relative path"


@dataclass(frozen=True)
class Editable:
    """An edit to the program may write the path, which the tree keys as ``key``."""

    key: str


type EditCheck = Editable | NotInProject | NotProjectSource | NotProjectRelative


def check_edit(pristine: Path, source: ProjectSource, relative: str) -> EditCheck:
    """Whether an edit to the program may write ``relative``: the key it writes, or why not.

    The one answer to that question: the editor's write tools ask it before accepting a write, and
    staging asks again, because by then the overlay has come back from a checkpoint or the edit
    store rather than from the call that validated it.

    Two things have to hold and they are not the same one. The path must stay **inside** the
    project, which is what keeps an edit from reaching anything else on the machine. And it must
    name the **project's own source** (``source``), which containment does not establish:
    confinement puts ``CARGO_HOME`` under ``.certora_internal/sandbox/cargo``, so every dependency's
    unpacked source is inside the tree, and a check that stopped at containment would let an edit
    rewrite Anchor for every crate in the graph.
    """
    root = pristine.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        return NotInProject(relative)
    key = _relative_key(relative)
    if key is None:
        return NotProjectRelative(relative)
    # ``key`` is the path the tree writes. ``resolved`` is where it lands, which differs when a
    # symlink in the project points somewhere else; that target has to be editable too.
    resolved = candidate.relative_to(root).as_posix()
    if not source.may_edit(key) or not source.may_edit(resolved):
        return NotProjectSource(path=relative, source=source)
    return Editable(key)


@dataclass(frozen=True)
class UnitEdits:
    """One unit's contribution to the tree's derived layer, read from its graph state."""

    #: Absolute, inside the tree: the unit's harness is a module of the crate under verification.
    module_path: Path
    draft: str
    #: The unit's edits to the program: whole files, by project-relative path.
    overlay: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _UnitDraft:
    """One unit's harness file in the tree."""

    module: str
    draft: str


@dataclass
class SharedTree:
    """The run's one working copy of the project, and the per-unit edits that derive its contents.

    Mutable and shared by every unit. Callers serialize :meth:`reconcile` with the build that
    follows it — a sibling reconciling between one unit's reconcile and its build would change the
    sources that build compiles — and that serialization is also what keeps two reconciles apart.
    """

    #: The developer's project. Read-only to this module: it is the *from* side of every derivation.
    pristine: Path
    #: The tree every unit builds in.
    root: Path
    #: What an edit to the program may write and where a unit's module may go.
    source: ProjectSource
    #: Every unit that has staged, by unit.
    _drafts: dict[str, _UnitDraft] = field(default_factory=dict)
    #: Every known unit's draft and the staging unit's edited files, as an overlay above the
    #: project, by tree-relative path.
    _derived: DictBackend = field(default_factory=DictBackend)

    def __post_init__(self) -> None:
        """Raises :class:`ValueError` if the tree and the project overlap."""
        if refusal := _overlap(self.pristine, self.root):
            raise ValueError(refusal)

    @functools.cached_property
    def _materializer(self) -> PersistentMaterializer:
        """The writer of the tree.

        Content-compared, so a file whose bytes have not changed keeps its mtime and cargo does not
        rebuild for it. A path the overlay stops serving is restored from the project, or removed if
        the project has no such file.
        """
        return PersistentMaterializer(
            DirBackend(self.pristine),
            [self._derived],
            global_exclude=_NOT_MATERIALIZED,
        )

    async def materialize(self) -> None:
        """Start the run's tree: a fresh copy of the project, replacing any tree a previous run left.

        Called once per run, before any unit stages. Never reused across runs, because the tree
        holds the run's private ``CARGO_HOME``, whose unpacked sources a dependency's build script
        can rewrite, and a tree carried into a later run would carry that rewrite into its build.
        The previous run's tree is left until here so that it can be inspected after that run.
        """
        if self.root.exists():
            await asyncio.to_thread(shutil.rmtree, self.root)
        await self._materializer.dump_to(self.root)

    def check_edit(self, relative: str) -> EditCheck:
        """Whether an edit to the program may write ``relative``: the key it writes, or why not."""
        return check_edit(self.pristine, self.source, relative)

    async def reconcile(self, unit: str, edits: UnitEdits) -> None:
        """Make the tree hold every known unit's draft and ``unit``'s edits to the program.

        A unit is known once it has reconciled. Until then its module is not in the tree,
        which is safe because it is ``cfg``'d out of every other unit's build. Another unit's edits
        are never in the tree once this returns: a file only it edited is a path the overlay stops
        serving, which the materializer restores from the project.

        Raises :class:`ValueError` if the module path is outside the tree, is not project source,
        or is another unit's, or if the unit's edits write a path an edit may not or name one file
        twice.
        """
        module = self._module_key(edits.module_path)
        for other, staged in self._drafts.items():
            if other != unit and staged.module == module:
                raise ValueError(f"units {other!r} and {unit!r} both name {module} as their module")
        drafts = {
            staged.module: staged.draft for other, staged in self._drafts.items() if other != unit
        } | {module: edits.draft}
        overlay: dict[str, str] = {}
        for path, text in edits.overlay.items():
            check = self.check_edit(path)
            if not isinstance(check, Editable):
                raise ValueError(f"unit {unit!r}'s edits write {check.describe()}")
            key = check.key
            if key in drafts:
                raise ValueError(f"unit {unit!r}'s edits write the harness module {key}")
            if key in overlay:
                raise ValueError(f"unit {unit!r}'s edits name {key} twice")
            overlay[key] = text
        self._drafts[unit] = _UnitDraft(module, edits.draft)

        self._derived.files = {**overlay, **drafts}
        await self._materializer.dump_to(self.root)

    def _module_key(self, module_path: Path) -> str:
        """``module_path`` relative to the tree, refusing one outside it or outside the source."""
        root = self.root.resolve()
        resolved = module_path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"{module_path} is outside the working tree {self.root}")
        relative = PurePosixPath(resolved.relative_to(root).as_posix())
        if not self.source.contains(relative):
            raise ValueError(f"{module_path} is not project source")
        return str(relative)
