"""The one working tree, and reconciling it to what the run's state says it should contain.

``docs/single-working-tree.md``. Every unit of a run shares one copy of the project. What keeps that
safe is not this module — it is the per-unit cargo feature (:attr:`HarnessModule.feature`), which
makes a sibling's draft invisible to rustc and therefore unable to break or dirty a build. What this
module does is make the tree **derived**:

.. code-block:: text

    tree = pristine project + each unit's draft + the union of the munges + the tuning files

Every term after the first is in the checkpoint, so the tree is a build cache rather than state.
Losing it costs a rebuild and never correctness, which is the property a resumed or cache-replayed
run needs and which the old "reuse whatever the last session staged" could not provide.

Three things follow from *derived*, and each was a bug in the arrangement this replaces:

* **A munge dropped from state disappears from the file.** Each munged file is rebuilt from the
  pristine copy and then replayed, rather than edited in place — so rewinding to an earlier
  checkpoint no longer leaves an attribute on disk that nothing in state knows about.
* **The union is replayed, not one unit's share.** A unit's munge is dormant for every other unit
  (:class:`~composer.spec.cvlr.munge.FunctionMunge`), so the union is well defined; replaying only
  the staging unit's would delete its siblings' lines and have them re-added on the next gate,
  churning the file and rebuilding the crate for nothing.
* **Writes are content-compared.** Cargo fingerprints on mtime, so rewriting a file with identical
  bytes forces a rebuild. Comparing first is what makes "resume and change nothing" a no-op build
  instead of a cold one.

The report's diff no longer needs a tree at all. :func:`munge_diff` replays a unit's munges into an
in-memory overlay, which is both more accurate — one unit's diff stops showing its siblings' dormant
lines — and available after the tree has been deleted.
"""

import dataclasses
import functools
import logging
from pathlib import PurePath
from pathlib import Path, PurePosixPath

from graphcore.tools.vfs import DictBackend, DirBackend, PersistentMaterializer

from composer.spec.cvlr.munge import (
    FunctionMunge,
    Munged,
    MungeAttempt,
    NOT_PROJECT_SOURCE,
    NotProjectSource,
    apply_munge,
    is_project_source,
)
from composer.spec.source.munge.vfs_diff import compute_diff, fs_resolver

_log = logging.getLogger(__name__)

#: Never copied into the working tree. ``target`` is regenerable and enormous; ``.git`` is neither
#: needed nor ours to duplicate; the work directory is where the copy itself goes, so copying it
#: would nest the tree inside itself.
#:
#: The list is :data:`~composer.spec.cvlr.munge.NOT_PROJECT_SOURCE`, because it is the same fact read
#: twice: what a copy of the project leaves out is what a munge of the project may not touch.
#: Sharing it also keeps the two from drifting, which matters in one direction — a directory added
#: here and not there would become munge-able.
def _NOT_MATERIALIZED(path: PurePath) -> bool:
    """Directories the tree is never filled from, by first path component.

    The same list the munge boundary uses (:data:`NOT_PROJECT_SOURCE`) and for the same reason: a
    build directory, a VCS directory and a previous run's tree are not the project. Copying one into
    the tree would at best waste the space and at worst nest a working copy inside a working copy.
    """
    return bool(path.parts) and path.parts[0] in NOT_PROJECT_SOURCE


@dataclasses.dataclass(frozen=True)
class NotInWorkdir:
    """The path resolves outside the run's working tree."""

    path: str

    def describe(self) -> str:
        return f"{self.path} resolves outside the working tree"


@dataclasses.dataclass(frozen=True)
class UnitEdits:
    """One unit's contribution to the tree's derived layer, read from its graph state."""

    #: Absolute, inside the tree. The harness is a module in the crate, so this is a source file of
    #: the package rather than a spec beside a conf.
    module_path: Path
    draft: str
    munges: tuple[FunctionMunge, ...] = ()


@dataclasses.dataclass(frozen=True)
class Drifted:
    """A munge in state could not be replayed onto the pristine source.

    The pristine copy is the developer's tree, and it can move between the run that recorded a munge
    and the run that replays it. ``why`` is one of
    :class:`~composer.spec.cvlr.munge.FunctionNotFound`,
    :class:`~composer.spec.cvlr.munge.FunctionAmbiguous` or
    :class:`~composer.spec.cvlr.munge.AlreadyMunged` — the same typed refusals the tool uses, which
    is why replay onto drifted source is detectable at all rather than silently mis-applied — or one
    of the two path refusals, or prose for the file simply being unreadable.
    """

    munge: FunctionMunge
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
    """What the tree needed to become what state says, and what could not be carried over."""

    #: Tree-relative paths actually rewritten. Empty means the tree already agreed with state, which
    #: is the case a resumed run should hit and the reason the next build is incremental.
    written: tuple[str, ...]
    drifted: tuple[Drifted, ...]

    def __bool__(self) -> bool:
        return not self.drifted



def replay(source: str, munges: tuple[FunctionMunge, ...]) -> tuple[str, tuple[Drifted, ...]]:
    """Apply ``munges`` to pristine ``source``, reporting the ones that did not take.

    Ordered by :attr:`~composer.spec.cvlr.munge.FunctionMunge.edit_id` rather than by the order they
    were recorded in. Two munges of the same function each insert a line immediately above its
    signature, so the order decides the file's bytes — and bytes that depend on which unit happened
    to stage first would make the crate's fingerprint depend on scheduling.
    """
    drifted: list[Drifted] = []
    for munge in sorted(munges, key=lambda m: m.edit_id):
        match apply_munge(source, munge):
            case Munged(source=updated):
                source = updated
            case other:
                drifted.append(Drifted(munge=munge, why=other))
    return source, tuple(drifted)


def munge_diff(pristine: Path, munges: tuple[FunctionMunge, ...]) -> str:
    """A unified diff from the project's own source to what ``munges`` make of it.

    Computed from state rather than read off a working tree, which is what makes it both correct and
    durable. Correct, because the tree holds every unit's munges and this is one unit's report;
    durable, because it survives the tree being deleted — the case
    ``docs/single-working-tree.md`` §4 makes routine.

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
    while holding the run's build semaphore — the same permit that serializes the cargo invocation
    it prepares. Nothing else writes here.
    """

    #: The developer's project. Read-only to this module: it is the *from* side of every derivation.
    pristine: Path
    #: The tree every unit builds in.
    root: Path
    _units: dict[str, UnitEdits] = dataclasses.field(default_factory=dict)
    #: The derived layer, as an overlay above the pristine project. Everything this tree contains
    #: that the developer did not write is served from here, and the materializer's own note of what
    #: it last held is what restores a file whose munges have since been dropped — including across
    #: sessions, which is the case nothing in *state* can name (``docs/the-tree-is-a-vfs.md`` §5).
    _derived: DictBackend = dataclasses.field(default_factory=DictBackend)
    #: Paths :meth:`adopt` put in the overlay. Kept apart from the rest because the two have
    #: opposite lifetimes: a reconcile rebuilds its own contribution from scratch every time, while
    #: an adopted file stays until somebody adopts it again. Folding them together makes a dropped
    #: munge un-droppable, since its file would look adopted the moment state stopped naming it.
    _adopted: set[str] = dataclasses.field(default_factory=set)

    @functools.cached_property
    def _materializer(self) -> PersistentMaterializer:
        """The tree's one writer.

        A base and one overlay: the project is copied in once and never re-read, and the derived
        files are content-compared on every reconcile. Both halves matter and neither is incidental
        — an unchanged file that gets rewritten costs a rebuild of everything downstream of it, and
        re-comparing a checkout that does not change is the copy the shared tree exists to avoid.

        What is *not* materialized is everything the build generates: ``target/``, the sandbox's
        private ``CARGO_HOME``, the lock file cargo updates. A persistent target accumulates them
        and the materializer touches only what it put there.
        """
        return PersistentMaterializer(
            DirBackend(self.pristine, cache_listing=False),
            [self._derived],
            global_exclude=_NOT_MATERIALIZED,
        )

    async def materialize(self) -> None:
        """Put the project into the tree, if this is the first time anyone has.

        Idempotent by way of the materializer's manifest rather than by an existence check: a tree
        that is already there is one the base copy skips, and one that is half-written is one it
        completes.
        """
        self.root.parent.mkdir(parents=True, exist_ok=True)
        await self._materializer.dump_to(self.root)

    def adopt(self, relative: Path | str, *more: Path | str) -> tuple[str, ...]:
        """Re-sync named files from the pristine project into the tree. Returns what changed.

        For the files the *run* derives before any unit exists — ``specs/mod.rs``, the package
        manifest, the placeholder module files — which are a function of the job list rather than of
        any unit's state, and which are written into the project because they are deliverables. A
        reused tree would otherwise keep the previous run's copy of them, so a resumed run whose
        component set had changed would build a manifest missing a unit's feature and a ``mod.rs``
        missing its module.

        Seeds the derived overlay rather than writing: the base copy runs once, so a *changed*
        project file would otherwise never reach an existing tree. Materialization is the caller's
        next step and is what makes it so — and content-compares it, so re-adopting an unchanged
        file does not dirty the crate.
        """
        seeded: list[str] = []
        for path in (relative, *more):
            source = self.pristine / path
            if not source.is_file():
                continue
            key = str(PurePosixPath(Path(path).as_posix()))
            self._derived.files[key] = source.read_text()
            self._adopted.add(key)
            seeded.append(key)
        return tuple(seeded)

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
            return NotProjectSource(path=relative, directory=inside.parts[0])
        return candidate

    async def reconcile(self, unit: str, edits: UnitEdits) -> Reconciled:
        """Make the tree agree with state, and say what it took.

        Records ``unit``'s contribution, then rebuilds the whole derived overlay from every unit's
        state and materializes it: each munged file is replayed onto the *pristine* source, so the
        result depends only on state. A munge dropped from state is a path the overlay stops
        serving, and the materializer restores it from the base — which is the case a file with no
        munges left is restored for, and it needs nothing remembered about which files were once
        munged, in this session or an earlier one.

        Every unit's draft is in the overlay, not just this one's. A sibling is ``cfg``'d out of
        this build so its content cannot affect it, but omitting it would make the overlay stop
        serving it and the materializer restore the placeholder over a draft its own unit is still
        working on.
        """
        self._units[unit] = edits
        drifted: list[Drifted] = []
        derived: dict[str, str] = {}

        for staged in self._units.values():
            derived[self._relative(staged.module_path)] = staged.draft

        by_path: dict[str, list[FunctionMunge]] = {}
        for staged in self._units.values():
            for munge in staged.munges:
                by_path.setdefault(munge.path, []).append(munge)

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

        # What `adopt` seeded stays: those are run-derived too, and dropping them here would have
        # the materializer restore the previous run's copy over them.
        adopted = {
            k: self._derived.files[k]
            for k in self._adopted
            if k in self._derived.files and k not in derived
        }
        before = dict(self._derived.files)
        self._derived.files = {**adopted, **derived}
        await self._materializer.dump_to(self.root)
        written = [p for p, text in self._derived.files.items() if before.get(p) != text]

        for drift in drifted:
            _log.warning("cvlr: munge not replayed — %s", drift.describe())
        return Reconciled(written=tuple(written), drifted=tuple(drifted))

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)
