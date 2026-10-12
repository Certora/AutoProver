"""The one working tree, and reconciling it to what the run's state says it should contain.

Every unit of a run builds in one shared copy of the project, so the dependency graph is compiled
once per run rather than once per unit. Sharing is safe only because each unit's harness module is
declared behind its own cargo feature, which the caller arranges before the tree is made: a module
behind a disabled ``cfg`` is never compiled, so a sibling's draft cannot break or dirty a build.
What this module does is make the tree **derived**:

.. code-block:: text

    tree = the project + each unit's draft + the union of every unit's munges

Every term after the first is in the run's checkpointed state, so the tree is a build cache rather
than state. Losing it costs a rebuild and never correctness, which is what lets a resumed run, or
one replayed from cache with no tree at all, pick up where it left off. Three things keep it so:

* **A munge dropped from state disappears from the file.** Each munged file is rebuilt from the
  project's copy and then replayed, rather than edited in place, so rewinding to an earlier
  checkpoint cannot leave an attribute on disk that nothing in state knows about.
* **The union is replayed, not one unit's share.** A unit's munge is dormant for every other unit
  (:class:`~composer.spec.cvlr.munge.Munge`), so the union is well defined; replaying only the
  staging unit's would delete its siblings' lines and have them re-added on their next gate,
  churning the file and rebuilding the crate for nothing.
* **Writes are content-compared.** Cargo fingerprints on mtime, so rewriting a file with identical
  bytes forces a rebuild. Comparing first is what makes "resume and change nothing" an incremental
  build instead of a cold one.

Each kind of file has one writer. Project files are copied by :meth:`SharedTree.materialize`, which
records what it copied so that a later run copies only what the project has changed since. A draft
belongs to one unit and is written by that unit's :meth:`SharedTree.reconcile`. Munged files are an
overlay above the project, written by graphcore's
:class:`~graphcore.tools.vfs.PersistentMaterializer`, whose manifest in the tree is what puts a file
back to the project's copy once no unit's state munges it — including in a later run.

The report's diff does not need a tree at all. :func:`munge_diff` replays a unit's munges into an
in-memory overlay, so one unit's diff shows only its own edits, and is available after the tree has
been deleted.
"""

import asyncio
import dataclasses
import functools
import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePath, PurePosixPath

from graphcore.tools.vfs import DictBackend, PersistentMaterializer

from composer.layout import INTERNAL_DIR
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.munge import (
    DeriveSwap,
    ImportSwap,
    FunctionExtraction,
    FunctionMunge,
    ModuleRedirect,
    Munge,
    Munged,
    MungeAttempt,
    apply_munge,
)
from composer.spec.source.munge.vfs_diff import compute_diff, fs_resolver

_log = logging.getLogger(__name__)

#: Directories that are **not** the project's source, wherever they sit in it.
#:
#: Two uses, and they are the same fact: these are what the working tree is never filled from and
#: what a munge may not touch. ``target`` is a build directory and can run to gigabytes;
#: ``node_modules`` is the TypeScript test tooling most Anchor projects carry; ``.git`` is neither
#: needed nor ours to duplicate; the work directory is where the tree itself goes, so copying it
#: would nest the tree inside itself. Matched at any depth because a workspace member can have its
#: own ``target``, at the cost of also excluding a source directory that happens to be named like
#: one of these.
#:
#: A munge is a modification of *the program under verification*, and containment in the tree does
#: not establish that — confinement puts the run's private ``CARGO_HOME`` under
#: :data:`~composer.sandbox.recipes.SANDBOX_CARGO_DIR`, so every dependency's unpacked source is
#: inside the tree too. Without this, ``munge_function`` would rewrite Anchor for every crate in the
#: graph. The cargo home needs no entry of its own: it lives under :data:`INTERNAL_DIR`.
NOT_PROJECT_SOURCE = (
    "target",
    ".git",
    "node_modules",
    INTERNAL_DIR.name,
    "certora_out",
    ".cvlr_work",
)

#: The tree's record of the project files it holds: each tree-relative path, mapped to the SHA-256
#: of the project's bytes it was last copied from. Kept in the tree, so it goes when the tree does.
PROJECT_RECORD = ".cvlr-project-files.json"


def _excluded_directory(directories: tuple[str, ...]) -> str | None:
    """The first of ``directories`` that is in :data:`NOT_PROJECT_SOURCE`, if any."""
    return next((d for d in directories if d in NOT_PROJECT_SOURCE), None)


def is_project_source(relative: PurePosixPath | str) -> bool:
    """Whether a tree-relative path names a file the project shipped rather than one a build made."""
    parts = PurePosixPath(relative).parts
    return bool(parts) and _excluded_directory(parts[:-1]) is None


@dataclasses.dataclass(frozen=True)
class NotProjectSource:
    """The path is inside the working tree but is not the project's source.

    Almost always a dependency: confinement puts the run's ``CARGO_HOME`` under
    ``.certora_internal/sandbox/cargo``, so every crate the build resolves has its unpacked source
    in the tree, a few directories away from the program.
    """

    path: str
    directory: str

    def describe(self) -> str:
        return f"{self.path} is under {self.directory}, which is not the project's source"


def _NOT_MATERIALIZED(path: PurePath) -> bool:
    """Whether a tree-relative path is under a directory the tree is never filled from."""
    return _excluded_directory(path.parts[:-1]) is not None


def _project_files(project: Path) -> Iterator[PurePosixPath]:
    """Every file of the project the tree holds a copy of, relative to the project.

    A symlink to a file is a file here, copied as the bytes it points at. A symlink to a directory
    is not descended into, which keeps a link to somewhere outside the project out of the tree.
    """
    for directory, subdirs, files in os.walk(project):
        here = PurePosixPath(Path(directory).relative_to(project).as_posix())
        subdirs[:] = [d for d in subdirs if d not in NOT_PROJECT_SOURCE]
        for name in files:
            if (Path(directory) / name).is_file():
                yield here / name


def _write_if_changed(path: Path, content: bytes) -> bool:
    """Write ``content`` unless ``path`` already holds exactly those bytes. Returns whether it wrote.

    Atomic, by way of a scratch file beside ``path`` and ``os.replace``, because the agents' source
    tools read the tree without holding the build permit and must see either version, never half of
    one. The scratch name starts with a dot and does not end in the original's extension, so a
    leftover from a killed process is not collected as a source file.
    """
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        scratch.write_bytes(content)
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)
    return True


def _read_record(path: Path) -> dict[str, str] | None:
    """The record at ``path``, or ``None`` if there is none or it is not one.

    A record of the wrong shape is treated as absent, which costs one full content compare.
    """
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()):
        return None
    return loaded


@dataclasses.dataclass(frozen=True)
class _ProjectRestores:
    """The materializer's base layer: what a file the overlay stops serving is restored to.

    The materializer never copies the project through it. :meth:`SharedTree.materialize` is the one
    thing that copies project files into the tree, so :meth:`dump_to` does nothing and :meth:`list`
    is empty; :meth:`get` is the part the materializer uses.
    """

    project: Path

    def get(self, path: str) -> str | None:
        try:
            # Decoded rather than read as text, so a CRLF file is restored with its line endings.
            return (self.project / path).read_bytes().decode()
        except (OSError, ValueError):
            return None

    def list(self) -> Iterable[str]:
        return ()

    async def dump_to(
        self, target: Path, include_path: Callable[[str], bool] | None = None
    ) -> None:
        return None


@dataclasses.dataclass(frozen=True)
class NotInWorkdir:
    """The path resolves outside the run's working tree."""

    path: str

    def describe(self) -> str:
        return f"{self.path} resolves outside the working tree"


@dataclasses.dataclass(frozen=True)
class UnitEdits:
    """One unit's contribution to the tree's derived layer, read from its graph state."""

    #: Absolute, inside the tree: the unit's harness is a module of the crate under verification.
    module_path: Path
    draft: str
    munges: tuple[Munge, ...] = ()


@dataclasses.dataclass(frozen=True)
class Drifted:
    """A munge in state could not be replayed onto the pristine source.

    The pristine copy is the developer's tree, and it can move between the run that recorded a munge
    and the run that replays it. ``why`` is one of
    :class:`~composer.spec.cvlr.munge.FunctionNotFound`,
    :class:`~composer.spec.cvlr.munge.FunctionAmbiguous`,
    :class:`~composer.spec.cvlr.munge.AlreadyMunged` or — for an extraction, whose region has to
    still read as it did — :class:`~composer.spec.cvlr.munge.SourceDrifted`: the same typed refusals
    the tool uses, which is why replay onto drifted source is detectable at all rather than silently
    mis-applied. Or one of the two path refusals, or prose for the file simply being unreadable.
    """

    munge: Munge
    why: MungeAttempt | NotInWorkdir | NotProjectSource | str

    def describe(self) -> str:
        match self.why:
            case NotInWorkdir() | NotProjectSource() as refusal:
                detail = refusal.describe()
            case str() as prose:
                detail = prose
            case attempt:
                detail = str(attempt)
        return f"{self.munge.edit_id}: {detail}"


@dataclasses.dataclass(frozen=True)
class Reconciled:
    """The munges in state that could not be carried into the tree. Truthy when there are none."""

    drifted: tuple[Drifted, ...]

    def __bool__(self) -> bool:
        return not self.drifted


def replay(source: str, munges: tuple[Munge, ...]) -> tuple[str, tuple[Drifted, ...]]:
    """Apply ``munges`` to pristine ``source``, reporting the ones that did not take.

    Ordered rather than taken as recorded, because the order decides the file's bytes: two munges of
    one function each insert a line immediately above its signature, and bytes that depended on
    which unit happened to stage first would make the crate's fingerprint depend on scheduling.

    Import swaps, module redirects and derive swaps go on first and interact with nothing: they sit
    at a ``use``, a ``mod`` declaration and a ``struct`` or ``enum`` respectively, where the
    remaining two address functions. They are ordered anyway, because "does not interact today" is
    not a property worth depending on for the crate's bytes.

    Attributes go on before extractions, and that is the interesting half of the order. An
    extraction replaces a function with a gated pair, so an attribute applied afterwards would find
    two definitions of one name and refuse; applied first, its ``cfg_attr`` line ends up above the
    ``#[cfg(not(..))]`` the pair opens with — which is the *right* place, because a unit that
    recorded an attribute is a unit building with somebody else's extraction feature off, and the
    half it compiles is the original.
    """
    def _replay_rank(m: Munge) -> int:
        match m:
            case ImportSwap():
                return 0
            case ModuleRedirect():
                return 1
            case DeriveSwap():
                return 2
            case FunctionMunge():
                return 3
            case FunctionExtraction():
                return 4

    drifted: list[Drifted] = []
    for munge in sorted(munges, key=lambda m: (_replay_rank(m), m.edit_id)):
        match apply_munge(source, munge):
            case Munged(source=updated):
                source = updated
            case other:
                drifted.append(Drifted(munge=munge, why=other))
    return source, tuple(drifted)


def munge_diff(pristine: Path, munges: tuple[Munge, ...]) -> str:
    """A unified diff from the project's own source to what ``munges`` make of it.

    Computed from state rather than read off a working tree, which is what makes it both correct and
    durable. Correct, because the tree holds every unit's munges and this is one unit's report;
    durable, because it survives the tree being deleted, which the tree is designed to allow.

    The diff itself is :mod:`composer.spec.source.munge.vfs_diff`, which is chain-neutral despite
    where it lives: it wants an "old" resolver and a "new" overlay, and the overlay here is the
    replay's output. Reusing it keeps one answer to "what does an edit look like in a report".
    """
    overlay: dict[str, str] = {}
    notes: list[str] = []
    for path in dict.fromkeys(m.path for m in munges):
        for_file = tuple(m for m in munges if m.path == path)
        try:
            source = (pristine / path).read_text()
        except OSError as exc:
            notes.append(f"# {path}: could not be diffed ({exc})\n")
            continue
        updated, drifted = replay(source, for_file)
        overlay[path] = updated
        notes += [f"# {d.describe()}\n" for d in drifted]
    return compute_diff(fs_resolver(pristine), overlay) + "".join(notes)


@dataclasses.dataclass
class SharedTree:
    """The run's one working copy of the project, and the per-unit edits that derive its contents.

    Mutable and shared by every unit, which is safe because :meth:`reconcile` is only ever called
    while holding the run's build permit — the same permit that serializes the cargo invocation it
    prepares and, for a submission, the Prover's rebuild and upload of the crate.
    """

    #: The developer's project. Read-only to this module: it is the *from* side of every derivation.
    pristine: Path
    #: The tree every unit builds in.
    root: Path
    _units: dict[str, UnitEdits] = dataclasses.field(default_factory=dict)
    #: The munged files, as an overlay above the project: each one's replayed content, by
    #: tree-relative path.
    _derived: DictBackend = dataclasses.field(default_factory=DictBackend)

    @functools.cached_property
    def _materializer(self) -> PersistentMaterializer:
        """The writer of the munged files.

        Content-compared, so a file whose replayed bytes have not changed keeps its mtime and cargo
        does not rebuild for it. A path the overlay stops serving is restored from the project, or
        removed if the project has no such file.
        """
        return PersistentMaterializer(
            _ProjectRestores(self.pristine),
            [self._derived],
            global_exclude=_NOT_MATERIALIZED,
        )

    async def materialize(self) -> tuple[str, ...]:
        """Bring the tree up to date with the project. Returns what a reused tree had out of date.

        Called once per run, before any unit stages. A project file is copied when the project's
        bytes differ from what :data:`PROJECT_RECORD` says the tree was last given, and a file the
        project no longer has is removed from the tree. A new tree is therefore a full copy, and a
        reused one costs reading the project and writing only what the developer, or the run's own
        scaffolding, changed since.

        Judged against the record rather than against the tree's copy, because the build edits
        files the project also has: cargo adds the harness's dependencies to the tree's
        ``Cargo.lock``, and resetting it every run would re-resolve them. A munged or drafted file
        is the same case, and keeping it is what lets an unchanged unit's first reconcile write
        nothing. Until that reconcile, a file a previous run munged still holds its munges.

        The returned paths are the copies and removals made into a tree that already had a record,
        so a new tree returns nothing.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        return await asyncio.to_thread(self._sync_project)

    def _sync_project(self) -> tuple[str, ...]:
        record_path = self.root / PROJECT_RECORD
        previous = _read_record(record_path)
        given = previous or {}
        current: dict[str, str] = {}
        changed: list[str] = []
        for relative in _project_files(self.pristine):
            key = relative.as_posix()
            try:
                content = (self.pristine / relative).read_bytes()
            except OSError:
                continue
            current[key] = hashlib.sha256(content).hexdigest()
            dest = self.root / relative
            if given.get(key) == current[key] and dest.exists():
                continue
            if _write_if_changed(dest, content):
                changed.append(key)
        for key in sorted(given.keys() - current.keys()):
            dest = self.root / key
            if dest.exists():
                dest.unlink()
                changed.append(key)
        _write_if_changed(record_path, json.dumps(current, indent=0, sort_keys=True).encode())
        return tuple(changed) if previous is not None else ()


    def resolve(self, relative: str) -> Path | NotInWorkdir | NotProjectSource:
        """The tree path a munge names, or why it is not one.

        The one answer to that question: the ``munge_function`` tool asks it before recording an
        edit, and :meth:`reconcile` asks it again before replaying one, because by then the record
        has come back from a checkpoint rather than from the call that validated it.

        Two things have to hold and they are not the same one. The path must stay **inside** the
        working tree, which is what keeps a munge from reaching the user's checkout. And it must
        name the **project's own source**, which containment does not establish: confinement puts
        ``CARGO_HOME`` under ``<tree>/.certora_internal/sandbox/cargo``, so every dependency's
        unpacked source is inside the tree too, and a check that stopped at containment would let a
        munge rewrite Anchor for every crate in the graph.
        """
        root = self.root.resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            return NotInWorkdir(relative)
        inside = candidate.relative_to(root)
        if not is_project_source(PurePosixPath(inside)):
            directory = _excluded_directory(inside.parts[:-1]) or inside.parts[0]
            return NotProjectSource(path=relative, directory=directory)
        return candidate

    def pristine_of(self, relative: str) -> Path | NotInWorkdir | NotProjectSource:
        """The developer's copy of the file a munge names — what replay actually reads.

        The tool that records an edit has to look at the same bytes :meth:`reconcile` will replay
        onto, and those are the pristine project's rather than the tree's. For an attribute the
        difference is cosmetic; for an extraction it is not, because the text it captures *is* its
        identity, and capturing a sibling unit's already-derived version of the file would record an
        edit that can never be replayed onto anything.

        Validated by :meth:`resolve`, so there is one answer to which paths a munge may name.
        """
        match self.resolve(relative):
            case Path() as inside:
                return self.pristine / inside.relative_to(self.root.resolve())
            case refusal:
                return refusal

    def run_global_munges(self) -> tuple[Munge, ...]:
        """Every munge in the tree that is **not** scoped to the unit that recorded it.

        The per-unit cargo feature is what makes a sibling's munge dormant, and one kind cannot have
        one: a :class:`~composer.spec.cvlr.munge.ModuleRedirect` of a module in a *local dependency*
        is gated on the shared ``certora`` feature, because a dependency's manifest does not declare
        this program's unit features. Such a munge is
        compiled into every unit's build whoever asked for it, so every unit's judge has to be shown
        it — a judge reviewing only its own unit's munges would be reviewing a program the prover
        did not analyze.

        Ordered by ``edit_id`` rather than by unit, so what a judge reads does not depend on which
        unit happened to stage first.
        """
        return tuple(
            sorted(
                {
                    m.edit_id: m
                    for staged in self._units.values()
                    for m in staged.munges
                    if m.feature == DEFAULT_FEATURE
                }.values(),
                key=lambda m: m.edit_id,
            )
        )

    async def reconcile(self, unit: str, edits: UnitEdits) -> Reconciled:
        """Make the tree agree with state, and report the munges that could not be carried over.

        Writes ``unit``'s draft, then rebuilds the munge overlay from every unit's state and
        materializes it. Each munged file is replayed onto the *project's* copy, so the result
        depends only on state, and a munge dropped from state is a path the overlay stops serving,
        which the materializer restores.

        A draft is written directly rather than through the overlay because it belongs to one unit.
        In the overlay, a later run's first reconcile would drop every unit's draft but its own,
        restoring each sibling's file to the project's placeholder until that sibling reconciled
        and rewrote it. Written directly, a sibling's file is left as it is, which is safe because
        it is ``cfg``'d out of this unit's build.

        The union covers only units that have reconciled in this process. After a restart, a file
        munged by both this unit and a sibling that has not reconciled yet loses the sibling's
        dormant lines until it does; that costs the file one more rewrite, not correctness.

        Raises :class:`ValueError` if the module path is outside the tree, is not project source,
        or is another unit's.
        """
        module = self._module_key(edits.module_path)
        for other, staged in self._units.items():
            if other != unit and self._module_key(staged.module_path) == module:
                raise ValueError(f"units {other!r} and {unit!r} both name {module} as their module")
        self._units[unit] = edits

        drifted: list[Drifted] = []
        derived: dict[str, str] = {}
        by_path: dict[str, list[Munge]] = {}
        for staged in self._units.values():
            for munge in staged.munges:
                by_path.setdefault(munge.path, []).append(munge)
                # A substitute module has no project counterpart to replay onto, so it goes into
                # the overlay whole. Written before the replay loop and never by it: the loop's
                # entries are all derived *from* the project, and this one stands in for it.
                derived.update(munge.created)

        for path, munges in sorted(by_path.items()):
            resolved = self.resolve(path)
            if not isinstance(resolved, Path):
                drifted += [Drifted(munge=m, why=resolved) for m in munges]
                continue
            try:
                source = (self.pristine / path).read_text()
            except OSError as exc:
                drifted += [
                    Drifted(munge=m, why=f"pristine source unreadable ({exc})") for m in munges
                ]
                continue
            updated, file_drift = replay(source, tuple(munges))
            drifted += file_drift
            derived[path] = updated

        self._derived.files = derived
        await self._materializer.dump_to(self.root)
        # After the dump, so that nothing the dump restores can land on top of the draft.
        await asyncio.to_thread(_write_if_changed, self.root / module, edits.draft.encode())

        for drift in drifted:
            _log.warning("cvlr: munge not replayed — %s", drift.describe())
        return Reconciled(drifted=tuple(drifted))

    def _module_key(self, module_path: Path) -> str:
        """``module_path`` relative to the tree, refusing one outside it or outside the source."""
        root = self.root.resolve()
        resolved = module_path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"{module_path} is outside the working tree {self.root}")
        relative = PurePosixPath(resolved.relative_to(root).as_posix())
        if not is_project_source(relative):
            raise ValueError(f"{module_path} is not project source")
        return str(relative)
