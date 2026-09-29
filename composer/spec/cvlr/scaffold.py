"""The ``certora/`` tree a CVLR project needs before a rule can be written.

The shape is a harness module behind a cargo feature, two tuning files, and a few ``Cargo.toml``
stanzas. What to write is read from ``cargo metadata`` and from the reference set. Two cases are
refused (:class:`Blocked`) instead of guessed: a package that builds no loadable object, and a
CVLR pin that does not match the platform generation the project is already on.

The result, for a program package inside a workspace. Files marked ``*`` are the project's and
are edited. The rest are AutoProver's. Without a ``[workspace]`` the package is the root, and the
root manifest gets no ``[workspace.dependencies]`` pins::

    <workspace>/
    ├── Cargo.toml *                  [workspace.dependencies] pins, [patch.crates-io] forks
    ├── .gitignore *                  prover build output
    ├── <local path dependency>/
    │   └── Cargo.toml *              a `certora` feature the program's forwards to
    └── <package>/
        ├── Cargo.toml *              `certora` feature, CVLR dependencies,
        │                             [package.metadata.certora]
        └── src/
            ├── lib.rs *              #[cfg(feature = "certora")] mod certora;
            └── certora/
                ├── mod.rs
                ├── specs/mod.rs      where authored rules land
                └── envs/
                    ├── cvlr_inlining.txt     generated from the starting configuration
                    └── cvlr_summaries.txt    the same

What the files under ``envs/`` mean is :mod:`composer.spec.cvlr.tuning`.

AutoProver's files are rewritten whenever they differ from what the scaffold would write
(:class:`Write`), so a harness left by an earlier run or by hand is replaced, and a newer
starting configuration reaches the build. Nothing else under ``src/certora/`` is touched.

The project's files are edited, never replaced. Each edit is planned only when what it adds is
missing, so a second run changes nothing. A manifest is edited as a TOML document
(:class:`~composer.cargo.manifest.ManifestEditor`): keys go into the tables that already hold
them, and everything the scaffold does not add comes back as it was, comments included.

``sources`` includes ``Cargo.toml``. ``.certora_sources`` is what the report and the counterexample
analyzer read, and a source tree with no manifest cannot be rebuilt. CVLR versions come from the
reference set.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path

from composer.cargo.features import CargoFeature
from composer.cargo.manifest import (
    AddEntries,
    AddTable,
    Comment,
    Dependency,
    Manifest,
    ManifestAddition,
    ManifestConflict,
    ManifestEditor,
    TableItem,
    TomlValue,
    read_manifest,
)
from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.env_paths import PathDialect, dialect_for
from composer.spec.cvlr import forks
from composer.spec.cvlr.tuning import ENV_FAMILIES, INLINING, SUMMARIES, compose_env
from composer.spec.cvlr_reference import ChainReference

#: Where the harness module goes in the target package.
HARNESS_DIR = Path("src") / "certora"
SPECS_DIR = HARNESS_DIR / "specs"
ENVS_DIR = HARNESS_DIR / "envs"

#: Build output the prover leaves in the project, and ``.cvlr_work``, this backend's per-unit work
#: directory.
GITIGNORE_LINES = (".certora", ".certora_internal", "certora_out", ".cvlr_work")

#: The crate type a Solana program's library target must have. Without it cargo produces no
#: loadable object, and the prover has nothing to read.
SHARED_OBJECT_TYPE = "cdylib"

#: The Solana convention for compiling a program without its ``entrypoint!``, which exports an
#: ``entrypoint`` symbol and installs a global allocator. Two programs linked into one object
#: collide on both, so a local dependency that is itself a program needs it on. For the verified
#: program it keeps the instruction dispatch, and the allocator and panic handler the macro
#: installs, out of the build. Enabled by ``certora`` when the package already has it. Not added
#: when it does not: a package with no entrypoint to suppress does not need one. The examples'
#: ``first_example`` has ``certora = []``.
NO_ENTRYPOINT_FEATURE = CargoFeature("no-entrypoint")


# ---------------------------------------------------------------------------------------------
# what a scaffolding run would change


@dataclass(frozen=True)
class Write:
    """One of AutoProver's files, planned whenever the file on disk differs from ``contents``.

    Whatever is at the path is replaced.
    """

    path: Path
    contents: str
    why: str


@dataclass(frozen=True)
class AppendSection:
    """Text appended at the end of one of the project's files, which is created if absent."""

    path: Path
    contents: str
    why: str


@dataclass(frozen=True)
class Addition:
    """One thing the plan adds to a manifest, and why."""

    edit: ManifestAddition
    why: str


@dataclass(frozen=True)
class EditManifest:
    """One of the project's manifests, with every addition the plan makes to it, in order.

    :func:`apply` makes them against the file as it is then, so a change to it since planning is
    kept. One that already has something the plan adds is :class:`ScaffoldStale`.
    """

    path: Path
    additions: tuple[Addition, ...]


type Change = Write | AppendSection | EditManifest


@dataclass(frozen=True)
class Blocked:
    """A decision the scaffold will not make, with what would resolve it.

    A plan that contains one applies nothing.
    """

    path: Path
    problem: str
    resolution: str


@dataclass(frozen=True)
class ScaffoldPlan:
    """What scaffolding a project would change, computed without touching it."""

    package: str
    changes: tuple[Change, ...]
    #: What was already in place. "Did nothing" and "found everything already there" look the
    #: same in a diff.
    satisfied: tuple[str, ...]
    blocked: tuple[Blocked, ...]
    #: How this target spells platform paths. Computed once, with the plan, so a later compose of
    #: the same tuning files uses the same spelling.
    dialect: PathDialect = PathDialect()

    def describe(self) -> str:
        lines = [f"CVLR scaffold for {self.package}:"]
        for change in self.changes:
            match change:
                case Write(path=path, why=why):
                    lines.append(f"  write {path} — {why}")
                case AppendSection(path=path, why=why):
                    lines.append(f"  extend {path} — {why}")
                case EditManifest(path=path, additions=additions):
                    lines += [f"  edit {path} {a.edit.describe()} — {a.why}" for a in additions]
        lines += [f"  ok {note}" for note in self.satisfied]
        lines += [f"  BLOCKED {b.path}: {b.problem} — {b.resolution}" for b in self.blocked]
        return "\n".join(lines)


class ScaffoldStale(RuntimeError):
    """A manifest the plan edits gained, after planning, something the plan adds."""


class ScaffoldBlocked(RuntimeError):
    """:func:`apply` was called on a plan that still has :class:`Blocked` entries."""

    def __init__(self, blocked: tuple[Blocked, ...]) -> None:
        self.blocked = blocked
        super().__init__(
            "the project needs a decision no template can make:\n"
            + "\n".join(f"  {b.path}: {b.problem}\n    {b.resolution}" for b in blocked)
        )


# ---------------------------------------------------------------------------------------------
# the content


#: What the scaffold writes on every line it adds to a manifest.
_ADDED = "added by AutoProver"


def _harness_source(relative: Path) -> str:
    """What the scaffold writes at ``relative`` under :data:`HARNESS_DIR`.

    Kept under ``harness_files/`` at the same path, named ``<stem>.template.rs``: copied into a
    project as it is, never compiled as part of AutoProver.
    """
    template = relative.with_name(f"{relative.stem}.template{relative.suffix}")
    return files(__package__).joinpath("harness_files", *template.parts).read_text()


def _harness_files(dialect: PathDialect) -> tuple[Write, ...]:
    """AutoProver's files, with paths relative to the package root.

    ``mod.rs`` declares ``specs`` as ``pub``. ``cvlr::mock_fn(with = crate::certora::specs::…)``
    expands in the program's own file, outside ``certora``, so the path has to be visible from
    there. ``certora`` itself stays private. Under the feature gate the module exists only in a
    verification build, and it adds nothing to the crate's public API.

    ``specs/mod.rs`` is written empty so the module exists before any rule file does. A module
    created later is one a later step can forget to declare.
    """
    return (
        Write(
            path=HARNESS_DIR / "mod.rs",
            contents=_harness_source(Path("mod.rs")),
            why="the harness module root",
        ),
        Write(
            path=SPECS_DIR / "mod.rs",
            contents=_harness_source(Path("specs") / "mod.rs"),
            why="where authored rules land",
        ),
        *(
            Write(
                path=ENVS_DIR / family.composite,
                contents=compose_env(family, dialect=dialect),
                why=f"the {family.kind.lower()} the build reports to the prover",
            )
            for family in ENV_FAMILIES
        ),
    )


def _lib_declaration() -> str:
    """The line that pulls the harness into the crate.

    The ``cfg`` is on this declaration, so the harness's submodules need no gate of their own.
    """
    return f'\n#[cfg(feature = "{DEFAULT_FEATURE}")]\nmod certora;\n'


def _metadata_table(*, inlining: Path, summaries: Path) -> tuple[TableItem, ...]:
    """``[package.metadata.certora]``. Tuning-file paths are relative to the package root."""
    return (
        Comment('"Cargo.toml" is included: `.certora_sources` is what the report and the'),
        Comment("counterexample analyzer read, and a source tree with no manifest cannot be"),
        Comment("rebuilt."),
        ("sources", ["Cargo.toml", "src/**/*.rs"]),
        ("solana_inlining", [str(inlining)]),
        ("solana_summaries", [str(summaries)]),
    )


# ---------------------------------------------------------------------------------------------
# planning


_MOD_CERTORA = re.compile(r"^[ \t]*(?:pub[ \t]+)?mod[ \t]+certora[ \t]*;", re.MULTILINE)


def _project_relative(path: Path, root: Path) -> Path:
    """``path`` spelled against ``root``.

    Raises when ``path`` is outside ``root``. Every path here comes from ``cargo metadata``, so
    one outside the workspace means the workspace was read from somewhere other than the project
    being scaffolded.
    """
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ScaffoldOutsideProject(f"{path} is not under {root}") from None


class ScaffoldOutsideProject(RuntimeError):
    """A path in the plan is outside the project root."""


def _dependency(*, inherit: bool, version: str) -> Mapping[str, str | bool]:
    """A CVLR dependency entry. ``optional`` is what makes ``dep:`` usable in the feature, and
    what keeps CVLR out of a release build."""
    pin: dict[str, str | bool] = {"workspace": True} if inherit else {"version": f"={version}"}
    return {**pin, "optional": True}


class _Manifests:
    """The manifests a plan reads and adds to, keyed by project-relative path.

    Several steps can add to one file: a package at the workspace root gets the workspace pins,
    the forks, and its own entries in one ``Cargo.toml``. Each file becomes one
    :class:`EditManifest`.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._read: dict[Path, Manifest] = {}
        self._additions: dict[Path, list[Addition]] = {}

    def read(self, relative: Path) -> Manifest:
        if relative not in self._read:
            self._read[relative] = read_manifest(self._root / relative)
        return self._read[relative]

    def add(self, relative: Path, edit: ManifestAddition, why: str) -> None:
        self._additions.setdefault(relative, []).append(Addition(edit, why))

    def changes(self) -> list[Change]:
        return [
            EditManifest(path=relative, additions=tuple(additions))
            for relative, additions in self._additions.items()
        ]


def _generation(version: str) -> str:
    """The platform generation a version belongs to: its major component.

    ``solana-program`` 2.2 and 2.3 are the same generation. 1.18 and 2.2 are not, and each
    generation has its own ``AccountInfo`` type."""
    return version.split(".", maxsplit=1)[0]


def _check_platform(workspace: Workspace, reference: ChainReference) -> list[Blocked]:
    """Refuse to pin a CVLR release the project's platform generation cannot use.

    A target on ``solana-program`` 1.18 given ``cvlr-solana`` 0.5.0 does not warn. It fails to
    compile, because the two generations have different ``AccountInfo`` types and the chain
    crate's helpers return the other one. The reference set already records which generation a
    chain crate requires (:class:`~composer.spec.cvlr_reference.PlatformGeneration`). This is
    where a project that is on a different one is caught, and it has to be caught before the pin
    is written.

    The first witness the project resolves decides. Later ones are not consulted. The list is
    most-specific first because a target on a newer generation resolves only the specific crate.
    Falling through to a broader witness after a specific one has answered would undo that order.
    Every copy of that witness has to be on the generation: one that is not still meets CVLR's
    types wherever its dependents hand an account to a helper.
    """
    for witness in reference.platform.witnesses:
        copies = workspace.resolved(witness.name)
        if not copies:
            continue
        off = [c.version for c in copies if _generation(c.version) != _generation(witness.line)]
        if not off:
            return []
        builds = ", ".join(off)
        return [
            Blocked(
                path=Path("Cargo.toml"),
                problem=(
                    f"this project builds {witness.name} {builds}, but the CVLR "
                    f"releases the reference set names are bound to {reference.platform.label} — "
                    f"and each generation has its own AccountInfo type, so the pairing does not "
                    f"compile rather than merely warning. The scaffold itself would still build; "
                    f"what fails is the first authored rule that hands one of this project's "
                    f"accounts to a CVLR helper"
                ),
                resolution=(
                    f"either move the project to {witness.name} {witness.line}, or move the "
                    f"reference set (composer/spec/cvlr_reference.py) to the CVLR line that "
                    f"matches {builds} — picking one of those is a decision about the "
                    f"project, not about the scaffold"
                ),
            )
        ]
    return []


@dataclass(frozen=True)
class _Pinned:
    """The project states a version requirement for a CVLR crate."""

    requirement: str


@dataclass(frozen=True)
class _Unpinned:
    """The project names a CVLR crate without a version: a git or path dependency.

    Which release that checkout is cannot be read from the manifest, and the gate cannot pass
    something it cannot read.
    """

    #: How the manifest names it, as the phrase that goes in the refusal — "as a git dependency".
    how: str


def _declaration(
    workspace: Workspace, package: CratePackage, crate: str
) -> _Pinned | _Unpinned | None:
    """How this project declares ``crate``, or ``None`` when it does not.

    Both manifests that can name a dependency are consulted, and ``workspace = true`` is followed
    to the root's ``[workspace.dependencies]``. A crate declared only in that table is still a
    declaration: no member depends on it yet, so the resolved graph does not mention it, but the
    scaffold is about to make a member inherit it.
    """
    spec = read_manifest(package.root / "Cargo.toml").dependencies.get(crate)
    if spec is None or spec.workspace:
        spec = read_manifest(workspace.root / "Cargo.toml").workspace_dependencies.get(crate)
    match spec:
        case None:
            return None
        case Dependency(version=str(version)):
            return _Pinned(version)
        case Dependency(git=str()):
            return _Unpinned("as a git dependency")
        case Dependency(path=str()):
            return _Unpinned("as a path dependency")
        case _:
            return _Unpinned("without a version")


def _check_pins(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> list[Blocked]:
    """Refuse a project that is on a CVLR release other than the one this build is pinned to.

    One line is supported at a time — the one :mod:`composer.spec.cvlr_reference` names — and
    everything this scaffold writes belongs to it: the pins, the specializations added beside
    them, and the env files :mod:`composer.spec.cvlr.tuning` composes. A project already on
    another line cannot be given those without putting two CVLR generations in one graph, which
    does not compile. This scaffold used to resolve that by deferring to the project's pin and
    withholding the specializations; a run set up that way is on a configuration nothing else
    here is built for, so it is refused instead.

    Two readings, because neither alone covers the project. The resolved graph is exact and
    settles a crate some member already depends on. The manifests settle a crate declared in
    ``[workspace.dependencies]`` that no member depends on yet — absent from the graph, and about
    to be inherited by the member this scaffold is setting up.

    A crate the project does not name at all is not checked. That is
    :class:`~composer.spec.cvlr.crates.Absent`, the ordinary state of a specialization the project
    has no use for, and refusing it would refuse every project this scaffold exists to set up.
    """
    blocked: list[Blocked] = []
    for release in reference.crates():
        supported = (
            f"this build supports {release.name} {release.version} and no other release: the "
            f"pins, the specializations and the env files the scaffold writes are all that line's"
        )
        fix = (
            f"either move the project to {release.name} {release.version}, or move the reference "
            f"set (composer/spec/cvlr_reference.py) to the line this project is on — which line "
            f"is supported is not a scaffold's call, and not a per-project one either"
        )
        off = [c.version for c in workspace.resolved(release.name) if c.version != release.version]
        if off:
            builds = ", ".join(off)
            blocked.append(
                Blocked(
                    path=Path("Cargo.toml"),
                    problem=(
                        f"this project builds {release.name} {builds}, and {supported}, "
                        f"so pinning them beside {builds} would put two CVLR "
                        f"generations in one graph"
                    ),
                    resolution=fix,
                )
            )
            continue
        match _declaration(workspace, package, release.name):
            case _Pinned(requirement) if requirement.removeprefix("=") != release.version:
                blocked.append(
                    Blocked(
                        path=Path("Cargo.toml"),
                        problem=(
                            f"this project declares {release.name} {requirement}, which no member "
                            f"depends on yet, so cargo has not resolved it — but the member this "
                            f"scaffold sets up is about to inherit it, and {supported}"
                        ),
                        resolution=fix,
                    )
                )
            case _Unpinned(how):
                blocked.append(
                    Blocked(
                        path=Path("Cargo.toml"),
                        problem=(
                            f"this project declares {release.name} {how}, so which release it is "
                            f"cannot be read from the manifest, and {supported} — a gate cannot "
                            f"pass a version it cannot see"
                        ),
                        resolution=(
                            f"declare {release.name} as a registry dependency at "
                            f"{release.version}, or move the reference set "
                            f"(composer/spec/cvlr_reference.py) to whatever that checkout is"
                        ),
                    )
                )
            case _:
                pass
    return blocked


def _plan_workspace_manifest(manifests: _Manifests, reference: ChainReference) -> list[str]:
    """Pins in ``[workspace.dependencies]``, when the root manifest has a ``[workspace]``."""
    path = Path("Cargo.toml")
    root = manifests.read(path)
    if root.workspace is None:
        return []

    declared = root.workspace.dependencies
    pins: list[tuple[str, TomlValue]] = []
    satisfied = []
    for crate in reference.scaffold_crates():
        if crate.name in declared:
            satisfied.append(
                f"{crate.name} is already a workspace dependency at the supported release — "
                f"_check_pins has already refused anything else, so this is left as it is"
            )
            continue
        pins.append((crate.name, {"version": f"={crate.version}"}))
    if pins:
        manifests.add(
            path,
            AddEntries(("workspace", "dependencies"), tuple(pins)),
            "pin the CVLR releases the reference set names, for the whole workspace",
        )
    return satisfied


def local_dependencies(workspace: Workspace, package: CratePackage) -> tuple[CratePackage, ...]:
    """The workspace crates ``package`` depends on by path, in manifest order.

    Read from the manifest's ``path =`` entries, not from the resolved graph. The question is
    which crates this project owns. A registry crate that a patch table resolves to a workspace
    member is still somebody else's code.
    """
    declared = read_manifest(package.root / "Cargo.toml").dependencies
    named = [name for name, spec in declared.items() if spec.path is not None]
    return tuple(
        found for name in named if (found := workspace.member(name)) is not None
    )


def _plan_feature_forwarding(
    workspace: Workspace, package: CratePackage, reference: ChainReference, manifests: _Manifests
) -> list[str]:
    """Give every local path dependency a ``certora`` feature.

    A verification-only edit inside a dependency has to be gated on a feature that dependency
    declares. Forwarding per-unit features (``unit_x = ["library/unit_x"]``) would give every
    dependency a different feature set per unit. Those features are empty so they do not do that
    (:func:`declare_unit_features`). Forwarding the one shared ``certora`` feature keeps a single
    resolved feature set. An edit gated that way is then on for every unit, not only the one that
    needed it.

    Declared up front. Adding a feature to a second crate after a build has resolved the graph
    does not change the feature set that build used.
    """
    satisfied: list[str] = []
    for dep in local_dependencies(workspace, package):
        path = dep.root.resolve().relative_to(workspace.root.resolve()) / "Cargo.toml"
        manifest = manifests.read(path)
        if DEFAULT_FEATURE in manifest.features:
            satisfied.append(f"{dep.name} already declares a `{DEFAULT_FEATURE}` feature")
            continue
        wanted = reference.scaffold_crates()
        missing = [c for c in wanted if c.name not in manifest.dependencies]
        enables = [f"dep:{c.name}" for c in wanted]
        if NO_ENTRYPOINT_FEATURE in dep.features:
            enables.insert(0, NO_ENTRYPOINT_FEATURE)
        manifests.add(
            path,
            AddEntries(("features",), ((DEFAULT_FEATURE, enables),)),
            f"so a verification-only edit inside {dep.name} can be gated — the program's "
            f"`{DEFAULT_FEATURE}` forwards to it",
        )
        if missing:
            manifests.add(
                path,
                AddEntries(
                    ("dependencies",),
                    tuple((c.name, _dependency(inherit=False, version=c.version)) for c in missing),
                ),
                f"the CVLR crates {dep.name}'s `{DEFAULT_FEATURE}` feature enables",
            )
    return satisfied


def _plan_package_manifest(
    workspace: Workspace,
    package: CratePackage,
    relative: Path,
    reference: ChainReference,
    manifests: _Manifests,
    *,
    inherit: bool,
) -> tuple[list[str], list[Blocked]]:
    manifest_rel = relative / "Cargo.toml"
    manifest = manifests.read(manifest_rel)
    satisfied: list[str] = []
    blocked: list[Blocked] = []

    if package.lib is None or not package.lib.builds_shared_object:
        blocked.append(
            Blocked(
                path=manifest_rel,
                problem=(
                    f"{package.name} builds no {SHARED_OBJECT_TYPE}, so cargo produces no loadable "
                    f"object and the prover has nothing to read"
                ),
                resolution=(
                    f'add `[lib]` with `crate-type = ["{SHARED_OBJECT_TYPE}"]` if this package '
                    f"really is the on-chain program, or scaffold the package that is — changing a "
                    f"library's crate type changes how it builds everywhere, which is not a "
                    f"scaffold's call"
                ),
            )
        )

    wanted = reference.scaffold_crates()
    missing = [c for c in wanted if c.name not in manifest.dependencies]
    satisfied += [
        f"{c.name} is already a dependency of {package.name}" for c in wanted if c not in missing
    ]

    features = manifest.features
    if DEFAULT_FEATURE in features:
        satisfied.append(
            f"the `{DEFAULT_FEATURE}` feature already exists as {features[DEFAULT_FEATURE]!r}"
        )
        if len(missing) == len(wanted):
            blocked.append(
                Blocked(
                    path=manifest_rel,
                    problem=(
                        f"`{DEFAULT_FEATURE}` is already a feature but no CVLR crate is a "
                        f"dependency, so the name means something else in this package"
                    ),
                    resolution=(
                        "rename that feature, or add the CVLR dependencies to it by hand and "
                        "re-run — extending a feature that already has a meaning is not a "
                        "scaffold's call"
                    ),
                )
            )
    else:
        enables = [f"dep:{c.name}" for c in wanted]
        if NO_ENTRYPOINT_FEATURE in package.features:
            enables.insert(0, NO_ENTRYPOINT_FEATURE)
        # Forwarded so an edit inside a local dependency has a feature to gate on.
        # See :func:`_plan_feature_forwarding` for why this is the shared feature.
        enables += [
            f"{dep.name}/{DEFAULT_FEATURE}" for dep in local_dependencies(workspace, package)
        ]
        manifests.add(
            manifest_rel,
            AddEntries(("features",), ((DEFAULT_FEATURE, enables),)),
            f"the feature that compiles the harness in ({', '.join(enables)})",
        )

    if missing:
        manifests.add(
            manifest_rel,
            AddEntries(
                ("dependencies",),
                tuple((c.name, _dependency(inherit=inherit, version=c.version)) for c in missing),
            ),
            "the CVLR dependencies a verification build compiles",
        )

    if "certora" in manifest.package_metadata:
        satisfied.append("[package.metadata.certora] already declares sources and tuning files")
    else:
        manifests.add(
            manifest_rel,
            AddTable(
                ("package", "metadata", "certora"),
                _metadata_table(
                    inlining=ENVS_DIR / INLINING.composite,
                    summaries=ENVS_DIR / SUMMARIES.composite,
                ),
            ),
            "the sources and tuning files the prover reads",
        )
    return satisfied, blocked


def _plan_harness(
    package: CratePackage, relative: Path, dialect: PathDialect
) -> tuple[list[Change], list[str]]:
    changes: list[Change] = []
    satisfied: list[str] = []
    for file in _harness_files(dialect):
        on_disk = package.root / file.path
        if on_disk.is_file() and on_disk.read_text() == file.contents:
            satisfied.append(f"{relative / file.path} is current")
        else:
            changes.append(replace(file, path=relative / file.path))

    if package.lib is not None:
        lib_rel = _project_relative(package.lib.src_path, package.root)
        source = package.lib.src_path.read_text() if package.lib.src_path.is_file() else ""
        if _MOD_CERTORA.search(source):
            satisfied.append(f"{relative / lib_rel} already declares the harness module")
        else:
            changes.append(
                AppendSection(
                    path=relative / lib_rel,
                    contents=_lib_declaration(),
                    why="pull the harness into the crate, gated on the feature",
                )
            )
    return changes, satisfied


def _plan_forks(workspace: Workspace, manifests: _Manifests) -> tuple[list[str], list[Blocked]]:
    """Add ``[patch.crates-io]`` entries for the verification forks.

    The table is workspace-level, so it goes on the workspace manifest with the rest of the plan.
    Without the Anchor fork, a rule that reaches a handler cannot be analyzed
    (:mod:`composer.spec.cvlr.forks`). A version the fork does not cover becomes a
    :class:`Blocked` on that manifest. The run stops instead of building a project that later
    fails with a pointer-analysis error.

    Crates the manifest already redirects are left alone. :func:`forks.already_patched` reads the
    patch table. The resolved graph is checked too, because a redirect shows up there as a git
    source.
    """
    path = Path("Cargo.toml")
    plan = forks.plan_overrides(
        workspace, already_redirected=forks.already_patched(manifests.read(path))
    )
    blocked = [
        Blocked(path=path, problem=b.problem, resolution=b.resolution) for b in plan.blocked
    ]
    if blocked:
        return [], blocked
    for override, table in forks.patch_tables(plan):
        manifests.add(
            path,
            table,
            f"verify {override.crate} {override.version} against {override.branch} of the fork",
        )
    return plan.notes(), []


def _plan_gitignore(workspace: Workspace) -> tuple[list[Change], list[str]]:
    path = workspace.root / ".gitignore"
    existing = path.read_text() if path.is_file() else None
    ignored = {line.strip() for line in (existing or "").splitlines()}
    absent = [line for line in GITIGNORE_LINES if line not in ignored]
    if not absent:
        return [], ["prover build output is already gitignored"]
    return [
        AppendSection(
            path=Path(".gitignore"),
            contents=("" if existing is None else "\n")
            + "# Certora Prover build output\n"
            + "".join(f"{line}\n" for line in absent),
            why=f"ignore {', '.join(absent)}",
        )
    ], []


def plan_scaffold(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> ScaffoldPlan:
    """What scaffolding ``package`` would change, without changing anything.

    Every path is relative to ``workspace.root``, which is also what :func:`apply` writes under.
    """
    relative = _project_relative(package.root, workspace.root)
    manifests = _Manifests(workspace.root)
    inherit = manifests.read(Path("Cargo.toml")).workspace is not None
    dialect = dialect_for(workspace, reference)

    changes: list[Change] = []
    satisfied = _plan_workspace_manifest(manifests, reference)
    for planned, notes in (
        _plan_harness(package, relative, dialect),
        _plan_gitignore(workspace),
    ):
        changes += planned
        satisfied += notes
    satisfied += _plan_feature_forwarding(workspace, package, reference, manifests)

    fork_notes, fork_blocked = _plan_forks(workspace, manifests)
    satisfied += fork_notes

    manifest_notes, blocked = _plan_package_manifest(
        workspace, package, relative, reference, manifests, inherit=inherit
    )
    # Unconditional, both of them: the scaffold always writes the reference-set pin now, so the
    # reference set's platform generation always describes what will be built.
    blocked += _check_pins(workspace, package, reference)
    blocked += _check_platform(workspace, reference)
    blocked += fork_blocked

    return ScaffoldPlan(
        package=package.name,
        changes=tuple(changes + manifests.changes()),
        satisfied=tuple(satisfied + manifest_notes),
        blocked=tuple(blocked),
        dialect=dialect,
    )


# ---------------------------------------------------------------------------------------------
# applying


def declare_unit_features(
    manifest: Path, features: Sequence[CargoFeature]
) -> tuple[CargoFeature, ...]:
    """Declare one empty cargo feature per name, and return the ones this call added.

    ``--features certora,unit_x`` fails with "Package does not contain this feature" unless
    ``unit_x`` is declared. The features are empty. A feature that enabled a dependency feature
    would change that dependency's resolved feature set and rebuild it per unit. Empty means only
    this crate's own code varies with the feature.

    A feature that is already declared is left as it is.
    """
    edited = ManifestEditor.read(manifest)
    wanted = [f for f in dict.fromkeys(features) if f not in edited.manifest.features]
    if not wanted:
        return ()
    edited.apply(AddEntries(("features",), tuple((f, []) for f in wanted)), note=_ADDED)
    manifest.write_text(edited.text())
    return tuple(wanted)


def apply(plan: ScaffoldPlan, root: Path) -> tuple[Path, ...]:
    """Carry out ``plan`` under ``root``, returning the paths it touched, in order.

    Refuses a plan that still has a :class:`Blocked` entry. A partial scaffold leaves the next
    build with two possible causes.
    """
    if plan.blocked:
        raise ScaffoldBlocked(plan.blocked)

    touched: list[Path] = []
    for change in plan.changes:
        target = root / change.path
        # Every path in a plan comes from constants and from cargo metadata. A path outside the
        # project root is a planner bug. Do not write it.
        if not target.resolve().is_relative_to(root.resolve()):
            raise ScaffoldOutsideProject(f"{target} escapes {root}")
        match change:
            case Write(contents=contents):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
            case AppendSection(contents=contents):
                existing = target.read_text() if target.is_file() else ""
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(existing + contents)
            case EditManifest(additions=additions):
                edited = ManifestEditor.read(target)
                for addition in additions:
                    try:
                        edited.apply(addition.edit, note=_ADDED)
                    except ManifestConflict as exc:
                        raise ScaffoldStale(
                            f"{change.path} changed after the scaffold was planned: {exc}"
                        ) from exc
                target.write_text(edited.text())
        touched.append(change.path)
    return tuple(touched)
