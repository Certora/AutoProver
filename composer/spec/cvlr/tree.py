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
import json
import logging
import os
import shutil
from pathlib import Path, PurePosixPath

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
_NOT_COPIED = shutil.ignore_patterns(*NOT_PROJECT_SOURCE)

#: Where the tree notes which of the project's files a run has munged, so a later session can restore
#: one whose munge has since been dropped from state. A hint, not state — see
#: :meth:`SharedTree._read_manifest`. Not collected as a prover source: it is neither ``Cargo.toml``
#: nor under ``src/**/*.rs``, and the CLI's own fallback patterns do not include ``.json``.
DERIVED_MANIFEST = ".cvlr-derived.json"


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


def _write_if_changed(path: Path, text: str) -> bool:
    """Write ``text`` only when it differs from what is there. Returns whether it wrote.

    Two properties, both load-bearing.

    **Compared before written**, because cargo's fingerprint is an mtime check against the files
    rustc reported reading: rewriting identical bytes is indistinguishable from an edit and costs a
    full rebuild of the crate. This is what makes "resume and change nothing" a no-op build.

    **Replaced atomically**, because the build permit covers the local cargo invocation but not the
    prover's own rerun of the build script, so another unit's build can be reading this file. The
    *content* is safe to see either way — a munge that is not this build's is a ``cfg_attr`` on a
    feature it does not enable, which contributes no attribute — but a half-written file is not, and
    ``os.replace`` is what makes "either the old one or the new one" the only two possibilities.
    """
    try:
        if path.read_text() == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.cvlr-tmp")
    scratch.write_text(text)
    os.replace(scratch, path)
    return True


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
    #: Every file any unit has *ever* munged, this session or an earlier one. Rebuilt files are
    #: found from this rather than from the current munge set, because a file whose last munge was
    #: dropped from state still has to be restored — and it is exactly then that nothing in state
    #: names it. Persisted in the tree (:data:`DERIVED_MANIFEST`) so that a resumed run restores a
    #: file the session that munged it never got to unmunge.
    _munged_paths: set[str] = dataclasses.field(default_factory=set)

    def materialize(self) -> None:
        """Copy the project into the tree if it is not already there.

        The one thing that is *not* reconciled, because it is not derived from state: everything the
        build generates and the sandbox's private ``CARGO_HOME`` live in this directory, and
        re-copying would throw away the warm cache the shared tree exists to keep. What keeps a
        stale tree honest is that every file the run derives is rewritten from state in
        :meth:`reconcile`.
        """
        if self.root.exists():
            _log.info("cvlr: reusing the working tree at %s", self.root)
            self._munged_paths |= self._read_manifest()
            return
        self.root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.pristine, self.root, ignore=_NOT_COPIED, symlinks=True)

    def _read_manifest(self) -> set[str]:
        """Files an earlier session derived, or nothing if the note is absent or unreadable.

        Deliberately forgiving. This is a *hint* about what to rebuild, never a source of truth: its
        worst case is naming a file no munge touches any more, which costs one restore-and-replay
        that :func:`_write_if_changed` then declines to write. A missing note costs the one thing it
        exists to prevent, so it is written on every reconcile rather than at the end of a run.
        """
        try:
            loaded = json.loads((self.root / DERIVED_MANIFEST).read_text())
        except (OSError, ValueError):
            return set()
        return {p for p in loaded.get("munged", []) if isinstance(p, str)}

    def _write_manifest(self) -> None:
        _write_if_changed(
            self.root / DERIVED_MANIFEST,
            json.dumps({"munged": sorted(self._munged_paths)}, indent=2) + "\n",
        )

    def adopt(self, relative: Path | str, *more: Path | str) -> tuple[str, ...]:
        """Re-sync named files from the pristine project into the tree. Returns what changed.

        For the files the *run* derives before any unit exists — ``specs/mod.rs``, the package
        manifest, the placeholder module files — which are a function of the job list rather than of
        any unit's state, and which are written into the project because they are deliverables. A
        reused tree would otherwise keep the previous run's copy of them, so a resumed run whose
        component set had changed would build a manifest missing a unit's feature and a ``mod.rs``
        missing its module.

        Content-compared like every other derived write, so re-adopting an unchanged file does not
        dirty the crate.
        """
        changed: list[str] = []
        for path in (relative, *more):
            source = self.pristine / path
            if not source.is_file():
                continue
            if _write_if_changed(self.root / path, source.read_text()):
                changed.append(str(path))
        return tuple(changed)

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

    def reconcile(self, unit: str, edits: UnitEdits) -> Reconciled:
        """Make the tree agree with state, and say what it took.

        Records ``unit``'s contribution, then rewrites the derived layer: this unit's harness module,
        and every file any unit has munged. Each is rebuilt from the pristine copy and replayed, so
        the result depends only on state — a munge removed from state is removed from disk, which is
        the case a file with no munges left is restored for.

        Sibling units' harness modules are deliberately left alone. They are ``cfg``'d out of this
        build, so their contents cannot affect it, and each sibling rewrites its own before its own
        gate.
        """
        self._units[unit] = edits
        written: list[str] = []
        drifted: list[Drifted] = []

        if _write_if_changed(edits.module_path, edits.draft):
            written.append(self._relative(edits.module_path))

        by_path: dict[str, list[FunctionMunge]] = {}
        for staged in self._units.values():
            for munge in staged.munges:
                by_path.setdefault(munge.path, []).append(munge)
        self._munged_paths |= by_path.keys()
        self._write_manifest()

        for path in sorted(self._munged_paths):
            munges = by_path.get(path, [])
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
            if _write_if_changed(resolved, updated):
                written.append(path)

        for drift in drifted:
            _log.warning("cvlr: munge not replayed — %s", drift.describe())
        return Reconciled(written=tuple(written), drifted=tuple(drifted))

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)
