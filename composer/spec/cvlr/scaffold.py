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

The project's files are edited, never replaced. Manifest edits are text insertions into the parsed
file, and each is planned only when what it adds is missing, so a second run changes nothing.
Reserializing the manifest would rewrite the project's comments to make one edit. Re-opening an
existing table is a duplicate-table error, so an existing ``[features]`` table is edited in place.

``sources`` includes ``Cargo.toml``. ``.certora_sources`` is what the report and the counterexample
analyzer read, and a source tree with no manifest cannot be rebuilt. CVLR versions come from the
reference set.
"""

import json
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.env_paths import PathDialect, dialect_for
from composer.spec.cvlr import forks
from composer.spec.cvlr.tuning import ENV_FAMILIES, INLINING, SUMMARIES, compose_env
from composer.spec.cvlr_reference import ChainReference, CrateRelease

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

#: The feature that removes a package's entrypoint so the harness can call handlers directly.
#: Enabled by ``certora`` when the package already has it. Not added when it does not: a package
#: with no entrypoint to suppress does not need one. The examples' ``first_example`` has
#: ``certora = []``.
NO_ENTRYPOINT_FEATURE = "no-entrypoint"


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
class InsertInTable:
    """Keys to add to a TOML table that already exists.

    The one change append cannot make. Re-opening ``[features]`` at the end of a manifest is a
    duplicate-table error, so a package that already has the table gets the key inserted into it.
    ``header`` is matched as a whole line and must appear exactly once. :func:`apply` checks that.
    This is a text edit of a file that was only parsed, and a miss has to fail instead of landing
    in the wrong table.
    """

    path: Path
    header: str
    contents: str
    why: str


type Change = Write | AppendSection | InsertInTable


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
            verb = {Write: "write", AppendSection: "extend", InsertInTable: "edit"}[type(change)]
            lines.append(f"  {verb} {change.path} — {change.why}")
        lines += [f"  ok {note}" for note in self.satisfied]
        lines += [f"  BLOCKED {b.path}: {b.problem} — {b.resolution}" for b in self.blocked]
        return "\n".join(lines)


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


#: ``specs`` is ``pub``. ``cvlr::mock_fn(with = crate::certora::specs::…)`` expands in the
#: program's own file, outside ``certora``, so the path has to be visible from there. ``certora``
#: itself stays private. Under the feature gate the module exists only in a verification build,
#: and it adds nothing to the crate's public API.
_HARNESS_ROOT = (
    "//! Certora verification harness.\n"
    "//!\n"
    "//! Compiled only under the `certora` feature, which `lib.rs` gates this module on.\n"
    "\n"
    "pub mod specs;\n"
)

#: Written empty so the module exists before any rule file does. A module created later is one a
#: later step can forget to declare.
_SPECS_ROOT = "//! The rules. One module per property group; declare each one here.\n"


def _harness_files(dialect: PathDialect) -> tuple[Write, ...]:
    """AutoProver's files, with paths relative to the package root."""
    return (
        Write(path=HARNESS_DIR / "mod.rs", contents=_HARNESS_ROOT, why="the harness module root"),
        Write(path=SPECS_DIR / "mod.rs", contents=_SPECS_ROOT, why="where authored rules land"),
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


def _metadata_section(*, inlining: Path, summaries: Path) -> str:
    """Tuning-file paths are relative to the package root."""
    return (
        "[package.metadata.certora]\n"
        '# "Cargo.toml" is included: `.certora_sources` is what the report and the\n'
        "# counterexample analyzer read, and a source tree with no manifest cannot be rebuilt.\n"
        'sources = ["Cargo.toml", "src/**/*.rs"]\n'
        f'solana_inlining = ["{inlining}"]\n'
        f'solana_summaries = ["{summaries}"]\n'
    )


# ---------------------------------------------------------------------------------------------
# planning


class MalformedManifest(RuntimeError):
    """A ``Cargo.toml`` could not be parsed, so the scaffold cannot decide what to write."""


_MOD_CERTORA = re.compile(r"^[ \t]*(?:pub[ \t]+)?mod[ \t]+certora[ \t]*;", re.MULTILINE)


def _section_banner() -> str:
    return "\n\n# === Certora CVLR — added by AutoProver ===\n"


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MalformedManifest(f"{path}: {exc}") from exc


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


def _toml_array(values: list[str]) -> str:
    """A TOML array of strings.

    JSON's string syntax is TOML's, so this is ``json.dumps`` rather than hand-rolled quoting.
    Hand-rolled quoting is how a crate name with an odd character lands unquoted."""
    return json.dumps(values)


def _dependency_stanza(crate: str, *, inherit: bool, version: str) -> str:
    """A ``[dependencies.<crate>]`` sub-table.

    A sub-table can be appended to a manifest that already has ``[dependencies]``. Re-opening
    that table is a duplicate-table error. ``optional`` is what makes ``dep:`` usable in the
    feature, and what keeps CVLR out of a release build."""
    pin = "workspace = true" if inherit else f'version = "={version}"'
    return f"[dependencies.{crate}]\n{pin}\noptional = true\n"


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
    """
    for witness in reference.platform.witnesses:
        resolved = workspace.resolved(witness.name)
        if resolved is None:
            continue
        if _generation(resolved.version) == _generation(witness.line):
            return []
        return [
            Blocked(
                path=Path("Cargo.toml"),
                problem=(
                    f"this project builds {witness.name} {resolved.version}, but the CVLR "
                    f"releases the reference set names are bound to {reference.platform.label} — "
                    f"and each generation has its own AccountInfo type, so the pairing does not "
                    f"compile rather than merely warning. The scaffold itself would still build; "
                    f"what fails is the first authored rule that hands one of this project's "
                    f"accounts to a CVLR helper"
                ),
                resolution=(
                    f"either move the project to {witness.name} {witness.line}, or pin the "
                    f"CVLR line that matches {resolved.version} by hand (the project's own pin is "
                    f"always respected) — picking one of those is a decision about the project, "
                    f"not about the scaffold"
                ),
            )
        ]
    return []


def _plan_workspace_manifest(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> tuple[list[Change], list[str]]:
    """Pins in ``[workspace.dependencies]``, when the root manifest has a ``[workspace]``."""
    parsed = _read_toml(workspace.root / "Cargo.toml")
    if "workspace" not in parsed:
        return [], []

    declared = parsed.get("workspace", {}).get("dependencies", {})
    stanzas, satisfied = [], []
    for crate in _scaffold_pins(workspace, package, reference):
        if crate.name in declared:
            satisfied.append(
                f"{crate.name} is already a workspace dependency — the project's pin wins, and a "
                f"disagreement with the reference set is reported as a version gap rather than "
                f"overridden here"
            )
            continue
        stanzas.append(f'[workspace.dependencies.{crate.name}]\nversion = "={crate.version}"\n')
    if not stanzas:
        return [], satisfied
    return [
        AppendSection(
            path=Path("Cargo.toml"),
            contents=_section_banner() + "\n".join(stanzas),
            why="pin the CVLR releases the reference set names, for the whole workspace",
        )
    ], satisfied


def local_dependencies(workspace: Workspace, package: CratePackage) -> tuple[CratePackage, ...]:
    """The workspace crates ``package`` depends on by path, in manifest order.

    Read from the manifest's ``path =`` entries, not from the resolved graph. The question is
    which crates this project owns. A registry crate that a patch table resolves to a workspace
    member is still somebody else's code.
    """
    declared = _read_toml(package.root / "Cargo.toml").get("dependencies", {})
    named = [
        name for name, spec in declared.items() if isinstance(spec, dict) and "path" in spec
    ]
    return tuple(
        found for name in named if (found := workspace.member(name)) is not None
    )


def _plan_feature_forwarding(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> tuple[list[Change], list[str]]:
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
    changes: list[Change] = []
    satisfied: list[str] = []
    forwards: list[str] = []
    for dep in local_dependencies(workspace, package):
        forwards.append(f"{dep.name}/{DEFAULT_FEATURE}")
        rel = dep.root.resolve().relative_to(workspace.root.resolve())
        parsed = _read_toml(dep.root / "Cargo.toml")
        features = parsed.get("features", {})
        if DEFAULT_FEATURE in features:
            satisfied.append(f"{dep.name} already declares a `{DEFAULT_FEATURE}` feature")
            continue
        wanted = _scaffold_pins(workspace, dep, reference)
        declared = parsed.get("dependencies", {})
        missing = [c for c in wanted if c.name not in declared]
        enables = [f"dep:{c.name}" for c in wanted]
        if NO_ENTRYPOINT_FEATURE in dep.features:
            enables.insert(0, NO_ENTRYPOINT_FEATURE)
        entry = f"{DEFAULT_FEATURE} = {_toml_array(enables)}\n"
        why = (
            f"so a verification-only edit inside {dep.name} can be gated — the program's "
            f"`{DEFAULT_FEATURE}` forwards to it"
        )
        if features:
            changes.append(
                InsertInTable(
                    path=rel / "Cargo.toml", header="[features]", contents=entry, why=why
                )
            )
        else:
            changes.append(
                AppendSection(
                    path=rel / "Cargo.toml",
                    contents=_section_banner() + f"[features]\n{entry}",
                    why=why,
                )
            )
        if missing:
            changes.append(
                AppendSection(
                    path=rel / "Cargo.toml",
                    contents=_section_banner()
                    + "\n".join(
                        _dependency_stanza(c.name, inherit=False, version=c.version)
                        for c in missing
                    ),
                    why=f"the CVLR crates {dep.name}'s `{DEFAULT_FEATURE}` feature enables",
                )
            )
    return changes, satisfied


def _plan_package_manifest(
    workspace: Workspace,
    package: CratePackage,
    relative: Path,
    reference: ChainReference,
    *,
    inherit: bool,
) -> tuple[list[Change], list[str], list[Blocked]]:
    manifest_rel = relative / "Cargo.toml"
    parsed = _read_toml(package.root / "Cargo.toml")
    changes: list[Change] = []
    satisfied: list[str] = []
    blocked: list[Blocked] = []
    #: Appended to the manifest as one block, so the banner appears once whatever else happens.
    appended: list[str] = []

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

    dependencies = parsed.get("dependencies", {})
    wanted = _scaffold_pins(workspace, package, reference)
    missing = [c for c in wanted if c.name not in dependencies]
    satisfied += [
        f"{c.name} is already a dependency of {package.name}" for c in wanted if c not in missing
    ]

    features = parsed.get("features", {})
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
        entry = f"{DEFAULT_FEATURE} = {_toml_array(enables)}\n"
        why = f"the feature that compiles the harness in ({', '.join(enables)})"
        if features:
            changes.append(
                InsertInTable(path=manifest_rel, header="[features]", contents=entry, why=why)
            )
        else:
            appended.append(f"[features]\n{entry}")

    appended += [
        _dependency_stanza(c.name, inherit=inherit, version=c.version) for c in missing
    ]

    if "certora" in parsed.get("package", {}).get("metadata", {}):
        satisfied.append("[package.metadata.certora] already declares sources and tuning files")
    else:
        appended.append(
            _metadata_section(
                inlining=ENVS_DIR / INLINING.composite, summaries=ENVS_DIR / SUMMARIES.composite
            )
        )

    if appended:
        changes.append(
            AppendSection(
                path=manifest_rel,
                contents=_section_banner() + "\n".join(appended),
                why="the dependencies, feature and metadata a verification build reads",
            )
        )
    return changes, satisfied, blocked


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


def _plan_forks(workspace: Workspace) -> tuple[list[Change], list[str], list[Blocked]]:
    """Append ``[patch.crates-io]`` entries for the verification forks.

    The table is workspace-level, so it goes on the workspace manifest with the rest of the plan.
    Without the Anchor fork, a rule that reaches a handler cannot be analyzed
    (:mod:`composer.spec.cvlr.forks`). A version the fork does not cover becomes a
    :class:`Blocked` on that manifest. The run stops instead of building a project that later
    fails with a pointer-analysis error.

    Crates the manifest already redirects are left alone. :func:`forks.already_patched` reads the
    patch table. The resolved graph is checked too, because a redirect shows up there as a git
    source.
    """
    plan = forks.plan_overrides(
        workspace,
        already_redirected=forks.already_patched((workspace.root / "Cargo.toml").read_text()),
    )
    blocked = [
        Blocked(path=Path("Cargo.toml"), problem=b.problem, resolution=b.resolution)
        for b in plan.blocked
    ]
    if blocked or not plan.overrides:
        return [], [] if blocked else plan.notes(), blocked
    return (
        [
            AppendSection(
                path=Path("Cargo.toml"),
                contents=forks.manifest_additions(plan),
                why=(
                    "verify against the forks that can be analyzed: "
                    + ", ".join(f"{o.crate} {o.version} -> {o.branch}" for o in plan.overrides)
                ),
            )
        ],
        plan.notes(),
        [],
    )


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
    inherit = "workspace" in _read_toml(workspace.root / "Cargo.toml")
    dialect = dialect_for(workspace, reference)

    changes: list[Change] = []
    satisfied: list[str] = []
    for planned, notes in (
        _plan_workspace_manifest(workspace, package, reference),
        _plan_harness(package, relative, dialect),
        _plan_gitignore(workspace),
        _plan_feature_forwarding(workspace, package, reference),
    ):
        changes += planned
        satisfied += notes

    fork_changes, fork_notes, fork_blocked = _plan_forks(workspace)
    changes += fork_changes
    satisfied += fork_notes

    manifest_changes, manifest_notes, blocked = _plan_package_manifest(
        workspace, package, relative, reference, inherit=inherit
    )
    # Only when the scaffold would write a reference-set pin. A project that already pins CVLR
    # keeps that pin, so the reference set's platform says nothing about what will be built.
    # Checking it here would refuse a project whose own pairing is consistent.
    if _introduced(workspace, package, reference):
        blocked += _check_platform(workspace, reference)
    blocked += fork_blocked

    return ScaffoldPlan(
        package=package.name,
        changes=tuple(changes + manifest_changes),
        satisfied=tuple(satisfied + manifest_notes),
        blocked=tuple(blocked),
        dialect=dialect,
    )


def _declared(workspace: Workspace, package: CratePackage) -> set[str]:
    """Every crate this project already names, across both manifests that can name one.

    A project can pin CVLR in ``[workspace.dependencies]`` before any member depends on it. The
    resolved graph then does not mention it, so this reads the manifests. Reading the graph would
    make the platform gate refuse a project whose own pin is consistent.
    """
    root = _read_toml(workspace.root / "Cargo.toml")
    return set(root.get("workspace", {}).get("dependencies", {})) | set(
        _read_toml(package.root / "Cargo.toml").get("dependencies", {})
    )


def _scaffold_pins(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> tuple[CrateRelease, ...]:
    """The reference-set crates this scaffold offers to pin.

    All of them, unless the project already declares the chain crate. That project has chosen its
    CVLR line, and the scaffold keeps it. Adding a specialization at the reference version on top
    of an older line would put two generations of ``AccountInfo`` in one graph, and the build
    would not compile. The scaffold either pins the whole reference set or leaves the project's
    pins alone.

    ``cvlr`` and the chain crate are still offered one at a time. A project that pins one and not
    the other has a half-configured manifest, and the platform gate checks the pairing once the
    missing one is added.
    """
    declared = _declared(workspace, package)
    if reference.chain.name in declared:
        return (reference.core, reference.chain)
    return reference.scaffold_crates()


def _introduced(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> tuple[str, ...]:
    """The crates this scaffold would pin at the reference version: the ones not already declared.

    Empty means the project's own pins stand, which is what the platform gate keys on."""
    declared = _declared(workspace, package)
    return tuple(
        c.name for c in _scaffold_pins(workspace, package, reference) if c.name not in declared
    )


# ---------------------------------------------------------------------------------------------
# applying


def _insert_in_table(text: str, header: str, addition: str) -> str:
    """``addition`` placed immediately after ``header``'s line.

    ``header`` must appear exactly once. This edits the text of a file that was parsed.
    Reserializing the manifest would rewrite its comments and ordering to make one change."""
    lines = text.splitlines(keepends=True)
    at = [i for i, line in enumerate(lines) if line.strip() == header]
    if len(at) != 1:
        raise ScaffoldBlocked(
            (
                Blocked(
                    path=Path("Cargo.toml"),
                    problem=f"{header} appears {len(at)} times, so there is no one place to add to",
                    resolution=f"add the entry to {header} by hand and re-run",
                ),
            )
        )
    index = at[0] + 1
    return "".join(lines[:index]) + addition + "".join(lines[index:])


def declare_unit_features(manifest: Path, features: Sequence[str]) -> tuple[str, ...]:
    """Declare one empty cargo feature per name, and return the ones this call added.

    ``--features certora,unit_x`` fails with "Package does not contain this feature" unless
    ``unit_x`` is declared. The features are empty. A feature that enabled a dependency feature
    would change that dependency's resolved feature set and rebuild it per unit. Empty means only
    this crate's own code varies with the feature.

    A feature that is already declared is left as it is.
    """
    parsed = _read_toml(manifest)
    declared = parsed.get("features", {})
    wanted = [f for f in dict.fromkeys(features) if f not in declared]
    if not wanted:
        return ()
    entries = "".join(f"{feature} = []\n" for feature in wanted)
    text = manifest.read_text()
    if declared:
        manifest.write_text(_insert_in_table(text, "[features]", entries))
    else:
        manifest.write_text(text + _section_banner() + f"[features]\n{entries}")
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
            case InsertInTable(header=header, contents=contents):
                target.write_text(_insert_in_table(target.read_text(), header, contents))
        touched.append(change.path)
    return tuple(touched)
