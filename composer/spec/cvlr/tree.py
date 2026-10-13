"""The one working tree, and reconciling it to what the run's state says it should contain.

A unit is one batch of a run's properties, whose rules are one harness module in the program's
crate. Every unit of a run builds in one shared copy of the project, so the dependency graph is
compiled once per run rather than once per unit. Sharing is safe only because each unit's harness module is
declared behind its own cargo feature, which the caller arranges before the tree is made: a module
behind a disabled ``cfg`` is never compiled, so a sibling's draft cannot break or dirty a build.
What this module does is make the tree **derived**:

.. code-block:: text

    tree = the project + each unit's draft + the union of every unit's munges

Every term after the first is in the run's checkpointed state, so the tree is a build cache rather
than state. Each run starts it fresh, and losing it costs a rebuild and never correctness, which is
what lets a resumed run, or one replayed from cache, pick up where it left off. Three things keep
it so:

* **A munge dropped from state disappears from the file.** Each munged file is rebuilt from the
  project's copy and then replayed, rather than edited in place, so rewinding to an earlier
  checkpoint cannot leave an attribute on disk that nothing in state knows about.
* **The union is replayed, not one unit's share.** A unit's munge is dormant for every other unit
  (:class:`~composer.spec.cvlr.munge.Munge`), so the union is well defined; replaying only the
  staging unit's would delete its siblings' lines and have them re-added on their next gate,
  churning the file and rebuilding the crate for nothing.
* **Writes are content-compared.** Cargo fingerprints on mtime, so rewriting a file with identical
  bytes forces a rebuild. Comparing first is what keeps a unit's gate from recompiling what nothing
  changed.

The tree is a graphcore :class:`~graphcore.tools.vfs.PersistentMaterializer`: the project is its
base, copied once per run by :meth:`SharedTree.materialize`, and the drafts and munged files are
one overlay above it. A path the overlay stops serving is put back to the project's copy.

The report's diff does not need a tree at all. :func:`munge_diff` replays a unit's munges into an
in-memory overlay, so one unit's diff shows only its own edits, and is available after the tree has
been deleted.
"""

import asyncio
import functools
import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath

from graphcore.tools.vfs import DictBackend, DirBackend, PersistentMaterializer

from composer.cargo.metadata import Workspace
from composer.layout import RUST_BUILD_DIRS
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.munge import (
    DeriveSwap,
    FunctionExtraction,
    FunctionMunge,
    ImportSwap,
    ModuleRedirect,
    Munge,
    Munged,
    MungeAttempt,
    apply_munge,
)
from composer.spec.source.munge.vfs_diff import diff_against_baseline, fs_resolver

_log = logging.getLogger(__name__)

#: Directories the working tree is never filled from, wherever they sit in the project.
#:
#: :data:`~composer.layout.RUST_BUILD_DIRS` are build output, which is what the agent's source tools
#: withhold too. The rest are this backend's: ``.git`` is neither needed nor ours to duplicate,
#: ``certora_out`` is the prover's output, and the work directory is where the tree itself goes, so
#: copying it would nest the tree inside itself. Matched at any depth because a workspace member can
#: have its own ``target``, at the cost of also skipping a source directory named like one of these.
NOT_PROJECT_SOURCE = (*RUST_BUILD_DIRS, ".git", "certora_out", ".cvlr_work")


def _excluded_directory(directories: tuple[str, ...]) -> str | None:
    """The first of ``directories`` that is in :data:`NOT_PROJECT_SOURCE`, if any."""
    return next((d for d in directories if d in NOT_PROJECT_SOURCE), None)


@dataclass(frozen=True)
class ProjectSource:
    """The files a munge may rewrite: the sources of the project's own library crates.

    An allowlist, taken from cargo rather than inferred from directory names. A library is the
    project's when cargo resolves its package from a path — a workspace member or a path dependency —
    and its sources are the directory holding its crate root. A dependency cargo fetched is never
    the project's, wherever its source is unpacked; under confinement that is inside the tree, in
    the run's private ``CARGO_HOME``.

    A crate whose root sits at its package root makes the whole package its source directory, build
    output included, so :data:`NOT_PROJECT_SOURCE` is refused inside a source directory as well.
    """

    #: Project-relative.
    roots: tuple[PurePosixPath, ...]

    @classmethod
    def of(cls, workspace: Workspace, project: Path) -> "ProjectSource":
        """The source directories of ``workspace``'s local libraries that are inside ``project``."""
        project = project.resolve()
        roots: set[PurePosixPath] = set()
        for package in workspace.packages:
            if not package.is_local or package.lib is None:
                continue
            directory = package.lib.src_path.parent.resolve()
            if directory.is_relative_to(project):
                roots.add(PurePosixPath(directory.relative_to(project).as_posix()))
        return cls(roots=tuple(sorted(roots)))

    def contains(self, relative: PurePosixPath | str) -> bool:
        """Whether a project-relative path names a file of one of the project's own crates."""
        path = PurePosixPath(relative)
        return (
            bool(path.parts)
            and any(path.is_relative_to(root) for root in self.roots)
            and _excluded_directory(path.parts[:-1]) is None
        )


@dataclass(frozen=True)
class NotProjectSource:
    """The path is inside the project but is not the source of one of its crates.

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
            f"(their source directories: {roots})"
        )


def _NOT_MATERIALIZED(path: PurePath) -> bool:
    """Whether a tree-relative path is, or is under, a directory the tree is never filled from.

    Asked of directories as well as files, which is what lets the copy skip a directory whole.
    """
    return _excluded_directory(path.parts) is not None


@dataclass(frozen=True)
class NotInProject:
    """The path resolves outside the project."""

    path: str

    def describe(self) -> str:
        return f"{self.path} resolves outside the project"


def project_file(
    pristine: Path, source: ProjectSource, relative: str
) -> Path | NotInProject | NotProjectSource:
    """The project's file a munge names — what replay reads — or why a munge may not name it.

    The one answer to that question: whatever records a munge asks it first, and replay asks again,
    because by then the record has come back from a checkpoint rather than from the call that
    validated it. Replay writes the tree's file at the same relative path, so a path inside the
    project is inside the tree too.

    Two things have to hold and they are not the same one. The path must stay **inside** the
    project, which is what keeps a munge from reaching anything else on the machine. And it must
    name the **project's own source** (``source``), which containment does not establish:
    confinement puts ``CARGO_HOME`` under ``.certora_internal/sandbox/cargo``, so every dependency's
    unpacked source is inside the tree, and a check that stopped at containment would let a munge
    rewrite Anchor for every crate in the graph.
    """
    root = pristine.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        return NotInProject(relative)
    inside = candidate.relative_to(root)
    if not source.contains(PurePosixPath(inside.as_posix())):
        return NotProjectSource(path=relative, source=source)
    return pristine / inside


@dataclass(frozen=True)
class UnitEdits:
    """One unit's contribution to the tree's derived layer, read from its graph state."""

    #: Absolute, inside the tree: the unit's harness is a module of the crate under verification.
    module_path: Path
    draft: str
    munges: tuple[Munge, ...] = ()


@dataclass(frozen=True)
class Drifted:
    """A munge in state could not be replayed onto the pristine source.

    The pristine copy is the developer's tree, and it can move between the run that recorded a munge
    and the run that replays it. ``why`` is one of the typed refusals applying a munge returns
    (:data:`~composer.spec.cvlr.munge.MungeAttempt`) — the function or import is gone or ambiguous,
    the edit is already there, or, for a kind located by the text it captured,
    :class:`~composer.spec.cvlr.munge.SourceDrifted` — the same refusals the tool uses, which is why replay onto drifted source is detectable at all rather than silently
    mis-applied. Or one of the two path refusals, or prose for the file simply being unreadable.
    """

    munge: Munge
    why: MungeAttempt | NotInProject | NotProjectSource | str

    def describe(self) -> str:
        match self.why:
            case NotInProject() | NotProjectSource() as refusal:
                detail = refusal.describe()
            case str() as prose:
                detail = prose
            case attempt:
                detail = str(attempt)
        return f"{self.munge.edit_id}: {detail}"


@dataclass(frozen=True)
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


def replay_files(
    pristine: Path, source: ProjectSource, munges: Iterable[Munge]
) -> tuple[dict[str, str], tuple[Drifted, ...]]:
    """Each file ``munges`` name, replayed onto the project's copy, by project-relative path; and
    the munges that could not be carried into it.

    A module redirect's substitute is in the result too, whole: it has no project copy to replay
    onto, so it is the munge's own content rather than something derived from the project.
    """
    by_path: dict[str, list[Munge]] = {}
    for munge in munges:
        by_path.setdefault(munge.path, []).append(munge)
    read = fs_resolver(pristine)
    replayed: dict[str, str] = {}
    drifted: list[Drifted] = []
    for path, for_file in sorted(by_path.items()):
        match project_file(pristine, source, path):
            case NotInProject() | NotProjectSource() as refusal:
                drifted += [Drifted(munge=m, why=refusal) for m in for_file]
                continue
            case Path():
                pass
        text = read(path)
        if text is None:
            drifted += [
                Drifted(munge=m, why="the project's copy is unreadable or not UTF-8")
                for m in for_file
            ]
            continue
        updated, file_drift = replay(text, tuple(for_file))
        replayed[path] = updated
        drifted += file_drift
        for munge in for_file:
            if isinstance(munge, ModuleRedirect):
                replayed.update(munge.created)
    return replayed, tuple(drifted)


def munge_diff(pristine: Path, source: ProjectSource, munges: tuple[Munge, ...]) -> str:
    """A unified diff from the project's own source to what ``munges`` make of it.

    Computed from state rather than read off a working tree, which is what makes it both correct and
    durable. Correct, because the tree holds every unit's munges and this is one unit's report;
    durable, because it survives the tree being deleted, which the tree is designed to allow.
    """
    replayed, drifted = replay_files(pristine, source, munges)
    notes = "".join(f"# {d.describe()}\n" for d in drifted)
    return diff_against_baseline(replayed, pristine) + notes


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
    #: What a munge may rewrite and where a unit's module may go.
    source: ProjectSource
    _units: dict[str, UnitEdits] = field(default_factory=dict)
    #: Every known unit's draft and every munged file's replayed content, as an overlay above the
    #: project, by tree-relative path.
    _derived: DictBackend = field(default_factory=DictBackend)

    @functools.cached_property
    def _materializer(self) -> PersistentMaterializer:
        """The writer of the tree.

        Content-compared, so a file whose replayed bytes have not changed keeps its mtime and cargo
        does not rebuild for it. A path the overlay stops serving is restored from the project, or
        removed if the project has no such file.
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

        Raises :class:`ValueError` if the tree is the project or contains it, since this deletes it.
        """
        if self.pristine.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"the working tree {self.root} is or contains the project {self.pristine}")
        if self.root.exists():
            await asyncio.to_thread(shutil.rmtree, self.root)
        await self._materializer.dump_to(self.root)

    def pristine_of(self, relative: str) -> Path | NotInProject | NotProjectSource:
        """The developer's copy of the file a munge names — what replay actually reads.

        The tool that records an edit has to look at the same bytes :meth:`reconcile` will replay
        onto, and those are the pristine project's rather than the tree's. For an attribute the
        difference is cosmetic; for an extraction it is not, because the text it captures *is* its
        identity, and capturing a sibling unit's already-derived version of the file would record an
        edit that can never be replayed onto anything.
        """
        return project_file(self.pristine, self.source, relative)

    def run_global_munges(self) -> tuple[Munge, ...]:
        """Every munge in the tree that is **not** scoped to the unit that recorded it.

        The per-unit cargo feature is what makes a sibling's munge dormant, and one kind cannot have
        one: a :class:`~composer.spec.cvlr.munge.ModuleRedirect` of a module in a *local dependency*
        is gated on the shared ``certora`` feature, because a dependency's manifest does not declare
        this program's unit features. Such a munge is compiled into every unit's build whoever asked
        for it, so every unit's judge has to be shown it — a judge reviewing only its own unit's
        munges would be reviewing a program the prover did not analyze.

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

        Rebuilds the overlay from every known unit's state — each draft, and each munged file
        replayed onto the *project's* copy — and materializes it. The result depends only on state,
        and a munge dropped from state is a path the overlay stops serving, which the materializer
        restores.

        A unit is known once it has reconciled. Until then its module is the project's placeholder
        and its munges are absent, which is safe because both are ``cfg``'d out of every other
        unit's build.

        Raises :class:`ValueError` if the module path is outside the tree, is not project source,
        or is another unit's.
        """
        module = self._module_key(edits.module_path)
        for other, staged in self._units.items():
            if other != unit and self._module_key(staged.module_path) == module:
                raise ValueError(f"units {other!r} and {unit!r} both name {module} as their module")
        self._units[unit] = edits

        replayed, drifted = replay_files(
            self.pristine, self.source, (m for staged in self._units.values() for m in staged.munges)
        )
        drafts = {
            self._module_key(staged.module_path): staged.draft for staged in self._units.values()
        }
        self._derived.files = {**replayed, **drafts}
        await self._materializer.dump_to(self.root)

        for drift in drifted:
            _log.warning("cvlr: munge not replayed — %s", drift.describe())
        return Reconciled(drifted=drifted)

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
