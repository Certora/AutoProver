"""The preflight scaffold: the `certora/` shape a CVLR project needs before a rule can exist.

``docs/cvlr-backend-plan.md`` §5.6 is the argument for this module being small. EVM's AutoSetup is a
search problem because making arbitrary Solidity compile under the Prover genuinely is one. The
Solana equivalent is not: a harness module behind a cargo feature, two tuning files, three
``Cargo.toml`` stanzas. That is template work, and §7.4 asked for agent assistance "only where a
template genuinely cannot decide" — the answer this module reports is **nowhere**. Every decision
here is read from ``cargo metadata`` or from the reference set, and the two cases a template must
not decide are refused (:class:`Blocked`) rather than guessed at.

**The shape comes from the recommended starting point, not from a vote.** `Certora/solana-spec-template
<https://github.com/Certora/solana-spec-template>`_ is what Certora tells a new project to clone,
which makes it advice where the client survey is only evidence (``docs/cvlr-capture-plan.md``
§4.7.4); the survey's twelve layouts across fourteen projects would have averaged into somebody's
house style. Where the public examples repo and the template disagree, the template wins and the
disagreement is recorded below.

**Nothing is overwritten, ever.** A project may already be set up, half set up, or set up
differently. Every file write is conditional on absence and every manifest change is computed from
the *parsed* manifest, so a second run is a no-op rather than a duplicated stanza — which is the one
thing the template's own ``certora-setup.py`` gets wrong: it appends to ``Cargo.toml`` blindly and
leaves a ``.orig`` behind, so running it twice produces a manifest cargo will not parse.

Four gaps in the upstream template are worked around here rather than reproduced, all recorded in
``docs/cvlr-backend-plan.md`` §7.4.1:

* its ``sources`` omits ``Cargo.toml``, which the examples include and ``.certora_sources`` wants;
* its ``[workspace.dependencies]`` pins CVLR 0.4 by hand, where the reference set is the one place
  that decides which release "current" means;
* its ``mod.rs`` never declares ``utils.rs``, so that file ships and is never compiled;
* its ``README`` names ``solana_inlining.txt`` / ``solana_summaries.txt`` where the shipped files
  are ``cvlr_*``.
"""

import dataclasses
import json
import logging
import re
import tomllib
from pathlib import Path

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.env_paths import PathDialect, dialect_for
from composer.spec.cvlr import munge
from composer.spec.cvlr_reference import ChainReference

_log = logging.getLogger(__name__)

TEMPLATE_REPO = "https://github.com/Certora/solana-spec-template.git"

#: The vendored canonical tuning files, and the stamp saying which upstream commit they came from.
#: Product data rather than corpus data, hence in the wheel — see
#: :mod:`composer.scripts.refresh_cvlr_envs`, which is the only thing that writes here.
ENV_DIR = Path(__file__).parent / "envs"
PROVENANCE_FILE = "PROVENANCE"

#: Where the harness module goes in the target package. Inside ``src/`` because that is where the
#: template is cloned to and what its ``[package.metadata.certora]`` paths name — unusual for
#: non-Rust files, and not ours to change.
HARNESS_DIR = Path("src") / "certora"
SPECS_DIR = HARNESS_DIR / "specs"
ENVS_DIR = HARNESS_DIR / "envs"

#: Build output the prover leaves in the project, plus this backend's own per-unit workspaces
#: (``composer.spec.cvlr.pipeline.WORK_DIR``). The first three are from the template's
#: ``certora-setup.py``; the last is ours and is ignored for the same reason.
GITIGNORE_LINES = (".certora", ".certora_internal", "certora_out", ".cvlr_work")

#: The crate type a Solana program's library target must have. Without it cargo produces no
#: loadable object and there is nothing for the prover to read.
SHARED_OBJECT_TYPE = "cdylib"

#: The feature that makes a package's own entrypoint disappear so the harness can call handlers
#: directly. Enabled by ``certora`` when the package has it; not invented when it does not, because
#: a package with no entrypoint to suppress does not need one (the examples' ``first_example`` has
#: ``certora = []``).
NO_ENTRYPOINT_FEATURE = "no-entrypoint"


@dataclasses.dataclass(frozen=True)
class EnvFamily:
    """One tuning file, in the layers the template splits it into.

    The split is the whole point: ``core`` and ``anchor`` are canonical content maintained upstream,
    ``package`` starts empty and is the project's own, and the composite is generated from all
    three. A project that needs its own inlining directive — the public examples' shipped file
    carries one for a real program — edits ``package`` and nothing canonical.
    """

    stem: str

    @property
    def core(self) -> str:
        return f"{self.stem}_core.txt"

    @property
    def anchor(self) -> str:
        return f"{self.stem}_anchor.txt"

    @property
    def package(self) -> str:
        return f"{self.stem}_package.txt"

    @property
    def composite(self) -> str:
        """The file the conf names, generated from the three layers."""
        return f"{self.stem}.txt"


INLINING = EnvFamily("cvlr_inlining")
SUMMARIES = EnvFamily("cvlr_summaries")
ENV_FAMILIES = (INLINING, SUMMARIES)

#: The upstream-maintained halves, which :mod:`composer.scripts.refresh_cvlr_envs` vendors.
CANONICAL_ENVS = tuple(name for f in ENV_FAMILIES for name in (f.core, f.anchor))

_GENERATED_HEADER = """;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;; DO NOT EDIT. THIS FILE HAS BEEN AUTOMATICALLY GENERATED
;;; Composed from {core}, {anchor} and {package}.
;;; Edit {package} and recompose; the other two are maintained upstream.
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
"""

_PACKAGE_ENV_HEADER = """; {kind} specific to this package. Empty to start with, and the one file
; here that is yours: the other layers are maintained in {repo}.
"""


# ---------------------------------------------------------------------------------------------
# what a scaffolding run would change


@dataclasses.dataclass(frozen=True)
class NewFile:
    """A file to create. Never a file to replace — see the module docstring."""

    path: Path
    contents: str
    why: str


@dataclasses.dataclass(frozen=True)
class AppendSection:
    """Text to append to an existing file, in full, at the end."""

    path: Path
    contents: str
    why: str


@dataclasses.dataclass(frozen=True)
class InsertInTable:
    """Keys to add to a TOML table that already exists.

    The one change append cannot make: re-opening ``[features]`` at the end of a manifest is a
    duplicate-table error, so a package that already has features needs the key inserted into the
    table it has. ``header`` is matched as a whole line and must appear exactly once, which
    :func:`apply` checks — this is a text edit to a file we only parsed, and it should fail loudly
    rather than land in the wrong table.
    """

    path: Path
    header: str
    contents: str
    why: str


type Change = NewFile | AppendSection | InsertInTable


@dataclasses.dataclass(frozen=True)
class Blocked:
    """Something a template must not decide, stated with what would resolve it.

    Not an error and not a warning: it is the boundary §7.4 asked about, and the answer landing on a
    human rather than on an agent is the point. A plan with any of these applies nothing.
    """

    path: Path
    problem: str
    resolution: str


@dataclasses.dataclass(frozen=True)
class ScaffoldPlan:
    """Everything a run would do to a project, computed without touching it."""

    package: str
    changes: tuple[Change, ...]
    #: What was already in place, in words. Reported because "did nothing" and "found everything
    #: already there" look identical in a diff and mean opposite things.
    satisfied: tuple[str, ...]
    blocked: tuple[Blocked, ...]

    def describe(self) -> str:
        lines = [f"CVLR scaffold for {self.package}:"]
        for change in self.changes:
            verb = {NewFile: "create", AppendSection: "extend", InsertInTable: "edit"}[
                type(change)
            ]
            lines.append(f"  {verb} {change.path} — {change.why}")
        lines += [f"  ok {note}" for note in self.satisfied]
        lines += [f"  BLOCKED {b.path}: {b.problem} — {b.resolution}" for b in self.blocked]
        return "\n".join(lines)


class ScaffoldBlocked(RuntimeError):
    """A plan with unresolved :class:`Blocked` entries was applied."""

    def __init__(self, blocked: tuple[Blocked, ...]) -> None:
        self.blocked = blocked
        super().__init__(
            "the project needs a decision no template can make:\n"
            + "\n".join(f"  {b.path}: {b.problem}\n    {b.resolution}" for b in blocked)
        )


# ---------------------------------------------------------------------------------------------
# the content


def canonical_env(name: str, dialect: PathDialect = PathDialect()) -> str:
    """One vendored upstream tuning file, spelled for the target's platform generation.

    The default dialect changes nothing, which is the right answer for a caller that only wants to
    see what was vendored — the refresh script's round-trip, and any test comparing against upstream.
    """
    return dialect.render((ENV_DIR / name).read_text())


def env_provenance() -> str:
    """Which upstream commit the vendored files came from.

    Read rather than baked in: the stamp and the files are written together by the refresh script,
    and a constant here could disagree with what is on disk."""
    stamp = ENV_DIR / PROVENANCE_FILE
    return stamp.read_text().strip() if stamp.is_file() else f"{TEMPLATE_REPO} (unrecorded)"


def compose_env(
    family: EnvFamily, *, package_layer: str, dialect: PathDialect = PathDialect()
) -> str:
    """The generated composite: header, then the three layers, the way the template's justfile does.

    ``package_layer`` is passed in rather than read from disk so recomposing after the authoring
    loop has added a directive is the same function call, with no hidden state. It is the one layer
    the dialect does not touch: it is the project's own file, written against the project's own
    symbols, so its paths are already whatever the project resolves.
    """
    header = _GENERATED_HEADER.format(
        core=family.core, anchor=family.anchor, package=family.package
    )
    parts = [header, f";;; Canonical layers vendored from {env_provenance()}\n"]
    if dialect.aliases:
        # Said in the file, because the alternative is a reader diffing it against upstream and
        # concluding it was hand-edited. Which paths moved is the whole subject of §7.5.6.
        parts.append(
            f";;; Platform paths rewritten for this target's generation "
            f"({len(dialect.aliases)} aliases) — see composer/spec/cvlr/env_paths.py\n"
        )
    parts += [
        canonical_env(family.core, dialect),
        canonical_env(family.anchor, dialect),
        package_layer,
    ]
    return "\n".join(p.rstrip("\n") for p in parts) + "\n"


#: The harness module tree. ``specs/`` is where authored rules land (§7.5), which is why it exists
#: empty rather than being created on first use — a module that has to be created is a module an
#: authoring step can forget to declare.
_HARNESS_FILES: dict[str, str] = {
    "mod.rs": (
        "//! Certora verification harness.\n"
        "//!\n"
        "//! Compiled only under the `certora` feature, which `lib.rs` gates this module on.\n"
        "\n"
        "mod log;\n"
        "mod mocks;\n"
        "mod nondet;\n"
        "mod specs;\n"
    ),
    "nondet.rs": (
        "//! Implementations of `cvlr::nondet::Nondet` for this program's own types.\n"
        "//!\n"
        "//! A rule needs a nondeterministic value of every type it quantifies over; the derives in\n"
        "//! `cvlr` cover the primitives, and anything else is declared here.\n"
    ),
    "log.rs": (
        "//! Implementations of `cvlr::log::CvlrLog` for this program's own types.\n"
        "//!\n"
        "//! A counterexample is only as legible as what `clog!` can print, so a type that appears\n"
        "//! in a rule wants an implementation here before it appears in a failure.\n"
    ),
    "specs/mod.rs": (
        "//! The rules. One module per property group; declare each one here.\n"
    ),
    "mocks/mod.rs": (
        "//! Mocks that simplify functionality for verification.\n"
        "//!\n"
        "//! Mirror the original module hierarchy: a function `my_mod::fun` is mocked by\n"
        "//! `certora::mocks::my_mod::fun`.\n"
    ),
}


#: Why each harness file exists, for the plan's own output. Separate from the contents so a
#: reader of a plan sees the intent and a reader of the file sees the code.
_HARNESS_WHY: dict[str, str] = {
    "mod.rs": "the harness module root",
    "nondet.rs": "where this program's types become nondeterministic",
    "log.rs": "where this program's types become printable in a counterexample",
    "specs/mod.rs": "where authored rules land",
    "mocks/mod.rs": "where a simplified stand-in for real code goes",
}


def _lib_declaration() -> str:
    """The line that pulls the harness into the crate.

    Gated at the declaration rather than inside the harness's own ``mod.rs`` — the template does the
    latter, which needs a gate per submodule and gets one wrong every time a module is added. Both
    public examples gate here, and one gate for one subtree is the whole feature's meaning.
    """
    return f'\n#[cfg(feature = "{DEFAULT_FEATURE}")]\nmod certora;\n'


def _metadata_section(package_relative_envs: dict[str, str]) -> str:
    inlining = package_relative_envs[INLINING.stem]
    summaries = package_relative_envs[SUMMARIES.stem]
    return (
        "[package.metadata.certora]\n"
        '# "Cargo.toml" is deliberately included: `.certora_sources` is what the report and the\n'
        "# counterexample analyzer read, and a source tree with no manifest cannot be rebuilt.\n"
        'sources = ["Cargo.toml", "src/**/*.rs"]\n'
        f'solana_inlining = ["{inlining}"]\n'
        f'solana_summaries = ["{summaries}"]\n'
    )


# ---------------------------------------------------------------------------------------------
# planning


class MalformedManifest(RuntimeError):
    """A ``Cargo.toml`` could not be parsed, so nothing can be decided about it."""


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

    Raises rather than falling back: every path here comes from ``cargo metadata``, so one that is
    not under the workspace root means the workspace was read from somewhere other than the project
    being scaffolded, and writing anything at that point would be a guess."""
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ScaffoldOutsideProject(f"{path} is not under {root}") from None


class ScaffoldOutsideProject(RuntimeError):
    """A path the planner produced falls outside the project root."""


def _toml_array(values: list[str]) -> str:
    """A TOML array of strings. JSON's string syntax is TOML's, which is why this is not hand-rolled
    quoting — and hand-rolled quoting is how a crate name with an odd character lands unquoted."""
    return json.dumps(values)


def _dependency_stanza(crate: str, *, inherit: bool, version: str) -> str:
    """A ``[dependencies.<crate>]`` sub-table.

    A sub-table rather than an inline entry because it can be *appended* to a manifest that already
    has a ``[dependencies]`` table, where re-opening that table would be a duplicate-table error.
    ``optional`` is what makes ``dep:`` usable in the feature, and what keeps CVLR out of a release
    build entirely."""
    pin = "workspace = true" if inherit else f'version = "={version}"'
    return f"[dependencies.{crate}]\n{pin}\noptional = true\n"


def _generation(version: str) -> str:
    """The platform generation a version belongs to — its major component.

    Coarse on purpose. ``solana-program`` 2.2 and 2.3 are the same generation and interchangeable;
    1.18 and 2.2 are not, and each generation has its own ``AccountInfo`` type."""
    return version.split(".", maxsplit=1)[0]


def _check_platform(workspace: Workspace, reference: ChainReference) -> list[Blocked]:
    """Refuse to pin a CVLR release the project's chain platform cannot be paired with.

    Found by scaffolding a real project rather than reasoned about: a target on ``solana-program``
    1.18 given ``cvlr-solana`` 0.5.0 does not warn, it fails to compile, because the two generations
    have different ``AccountInfo`` types and the chain crate's helpers return the other one. The
    reference set already says a chain crate *implies* a generation
    (:class:`~composer.spec.cvlr_reference.PlatformGeneration`); this is the one place that can
    notice the implication is false for a given project, and it can only notice before writing.

    The first witness the project resolves decides, and no later one is consulted: the witnesses
    are ordered most-specific-first precisely because a target on a newer generation resolves only
    the specific one. Falling through to a broader witness after a specific one has answered would
    re-introduce the hole the ordering exists to close.
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
    workspace: Workspace, reference: ChainReference
) -> tuple[list[Change], list[str]]:
    """Pins in ``[workspace.dependencies]``, when the root manifest has a ``[workspace]``."""
    parsed = _read_toml(workspace.root / "Cargo.toml")
    if "workspace" not in parsed:
        return [], []

    declared = parsed.get("workspace", {}).get("dependencies", {})
    stanzas, satisfied = [], []
    for crate in reference.scaffold_crates():
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


def _plan_package_manifest(
    package: CratePackage, relative: Path, reference: ChainReference, *, inherit: bool
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
    wanted = reference.scaffold_crates()
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
        appended.append(_metadata_section({f.stem: str(ENVS_DIR / f.composite) for f in ENV_FAMILIES}))

    if appended:
        changes.append(
            AppendSection(
                path=manifest_rel,
                contents=_section_banner() + "\n".join(appended),
                why="the dependencies, feature and metadata a verification build reads",
            )
        )
    return changes, satisfied, blocked


def _plan_harness(package: CratePackage, relative: Path) -> tuple[list[Change], list[str]]:
    changes: list[Change] = []
    satisfied: list[str] = []
    for name, contents in _HARNESS_FILES.items():
        target = HARNESS_DIR / name
        if (package.root / target).exists():
            satisfied.append(f"{relative / target} already exists")
            continue
        changes.append(NewFile(path=relative / target, contents=contents, why=_HARNESS_WHY[name]))

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


def _plan_envs(
    package: CratePackage, relative: Path, dialect: PathDialect
) -> tuple[list[Change], list[str]]:
    changes: list[Change] = []
    satisfied: list[str] = []
    for family in ENV_FAMILIES:
        kind = "Inlining directives" if family is INLINING else "Points-to summaries"
        layer_path = package.root / ENVS_DIR / family.package
        package_layer = (
            layer_path.read_text()
            if layer_path.is_file()
            else _PACKAGE_ENV_HEADER.format(kind=kind, repo=TEMPLATE_REPO)
        )
        planned = (
            (
                family.core,
                canonical_env(family.core, dialect),
                f"canonical {kind.lower()}, from upstream",
            ),
            (
                family.anchor,
                canonical_env(family.anchor, dialect),
                f"Anchor {kind.lower()}, from upstream",
            ),
            (family.package, package_layer, f"this package's own {kind.lower()} — yours to edit"),
            (
                family.composite,
                compose_env(family, package_layer=package_layer, dialect=dialect),
                "the composite the build reports to the prover",
            ),
        )
        for name, contents, why in planned:
            target = ENVS_DIR / name
            if (package.root / target).exists():
                satisfied.append(f"{relative / target} already exists")
                continue
            changes.append(NewFile(path=relative / target, contents=contents, why=why))
    return changes, satisfied


def _plan_munge(workspace: Workspace) -> tuple[list[Change], list[str], list[Blocked]]:
    """Point the target at the verification forks of its dependencies.

    A workspace-manifest append rather than its own step, because ``[patch.crates-io]`` is a
    workspace-level table and the scaffold already owns one review-then-apply cycle. Anchor is the
    only case today, and it is not optional: without it a rule that reaches a handler cannot be
    analyzed at all (:mod:`composer.spec.cvlr.munge`).

    A version the fork does not cover becomes a :class:`Blocked` on the manifest, so the run stops
    with a sentence about Anchor coverage instead of proceeding to a build that looks fine and then
    reports a pointer-analysis error.
    """
    plan = munge.plan_munge(workspace)
    blocked = [
        Blocked(path=Path("Cargo.toml"), problem=b.problem, resolution=b.resolution)
        for b in plan.blocked
    ]
    if blocked or not plan.overrides:
        note = (
            [f"{c} is not a dependency of this project" for c in plan.inapplicable]
            if not blocked
            else []
        )
        return [], note, blocked

    existing = (workspace.root / "Cargo.toml").read_text()
    already = [o for o in plan.overrides if f"[patch.crates-io.{o.crate}]" in existing]
    if already:
        return (
            [],
            [f"{o.crate} is already redirected in Cargo.toml" for o in already],
            [],
        )
    return (
        [
            AppendSection(
                path=Path("Cargo.toml"),
                contents=munge.manifest_additions(plan),
                why=(
                    "verify against the forks that can be analyzed: "
                    + ", ".join(f"{o.crate} {o.version} -> {o.branch}" for o in plan.overrides)
                ),
            )
        ],
        [],
        [],
    )


def _plan_gitignore(workspace: Workspace) -> tuple[list[Change], list[str]]:
    path = workspace.root / ".gitignore"
    header = "# Certora Prover build output\n"
    lines = "".join(f"{line}\n" for line in GITIGNORE_LINES)
    if not path.is_file():
        return [
            NewFile(
                path=Path(".gitignore"),
                contents=header + lines,
                why="keep prover build output out of the project's history",
            )
        ], []
    existing = {line.strip() for line in path.read_text().splitlines()}
    absent = [line for line in GITIGNORE_LINES if line not in existing]
    if not absent:
        return [], ["prover build output is already gitignored"]
    return [
        AppendSection(
            path=Path(".gitignore"),
            contents="\n" + header + "".join(f"{line}\n" for line in absent),
            why=f"ignore {', '.join(absent)}",
        )
    ], []


def plan_scaffold(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> ScaffoldPlan:
    """What scaffolding ``package`` would change, without changing anything.

    Every path is relative to ``workspace.root``, which is also what :func:`apply` writes under —
    one origin, so a plan can be printed, reviewed and applied without a reader having to track
    which of two roots each line is measured against.
    """
    relative = _project_relative(package.root, workspace.root)
    inherit = "workspace" in _read_toml(workspace.root / "Cargo.toml")

    changes: list[Change] = []
    satisfied: list[str] = []
    for planned, notes in (
        _plan_workspace_manifest(workspace, reference),
        _plan_harness(package, relative),
        _plan_envs(package, relative, dialect_for(workspace, reference)),
        _plan_gitignore(workspace),
    ):
        changes += planned
        satisfied += notes

    munge_changes, munge_notes, munge_blocked = _plan_munge(workspace)
    changes += munge_changes
    satisfied += munge_notes

    manifest_changes, manifest_notes, blocked = _plan_package_manifest(
        package, relative, reference, inherit=inherit
    )
    # Only when the scaffold would write a *reference-set* pin. A project that already pins CVLR
    # has made its own pairing decision — the scaffold inherits that pin, so the reference set's
    # platform says nothing about what will be built, and checking it here would refuse a project
    # whose own pin is perfectly consistent.
    if _introduced(workspace, package, reference):
        blocked += _check_platform(workspace, reference)
    blocked += munge_blocked

    return ScaffoldPlan(
        package=package.name,
        changes=tuple(changes + manifest_changes),
        satisfied=tuple(satisfied + manifest_notes),
        blocked=tuple(blocked),
    )


def _introduced(
    workspace: Workspace, package: CratePackage, reference: ChainReference
) -> tuple[str, ...]:
    """The reference set's crates this scaffold would pin *at the reference version*.

    A project may already pin CVLR in ``[workspace.dependencies]`` without any member depending on
    it yet, in which case the resolved graph does not mention it at all — so this reads the
    manifests rather than the graph. Getting that wrong is how the platform gate refuses a project
    whose own pin is consistent, which is what it did on the first project it was pointed at.
    """
    root = _read_toml(workspace.root / "Cargo.toml")
    pinned = set(root.get("workspace", {}).get("dependencies", {}))
    pinned |= set(_read_toml(package.root / "Cargo.toml").get("dependencies", {}))
    return tuple(c.name for c in reference.scaffold_crates() if c.name not in pinned)


# ---------------------------------------------------------------------------------------------
# applying


def _insert_in_table(text: str, header: str, addition: str) -> str:
    """``addition`` placed immediately after ``header``'s line.

    ``header`` must appear exactly once. This is a text edit to a file that was parsed, not
    reserialized — the alternative is a style-preserving TOML writer, and reserializing somebody's
    manifest would rewrite their comments and ordering to make one change."""
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


def apply(plan: ScaffoldPlan, root: Path) -> tuple[Path, ...]:
    """Carry out ``plan`` under ``root``, returning the paths it touched, in order.

    Refuses a plan with anything :class:`Blocked`: a partial scaffold is worse than none, because
    the next step is a build whose failure would then have two candidate causes.
    """
    if plan.blocked:
        raise ScaffoldBlocked(plan.blocked)

    touched: list[Path] = []
    for change in plan.changes:
        target = root / change.path
        # An assertion about this module, not a security boundary: every path in a plan is built
        # from constants and from `cargo metadata`, never from a model. A plan that escaped the root
        # would be a planner bug, and writing into somebody's home directory is the wrong way to
        # find out about it.
        if not target.resolve().is_relative_to(root.resolve()):
            raise ScaffoldOutsideProject(f"{target} escapes {root}")
        match change:
            case NewFile(contents=contents):
                if target.exists():
                    # Re-planned against a project that changed underneath: still never overwrite.
                    _log.info("scaffold: %s appeared since planning; left alone", change.path)
                    continue
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
