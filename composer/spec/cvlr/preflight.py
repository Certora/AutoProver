"""Scaffold a CVLR project, then check that the scaffold compiles.

:meth:`composer.pipeline.core.PipelineBackend.preflight` runs in the same task group as system
analysis (``docs/formalization-abstraction.md`` §2), so a failure here cancels the analysis. A
project that cannot compile with the harness in is not handed on.

:func:`prepare_workspace` writes files. :func:`gate_workspace` compiles. The compile sits on the
run's CPU budget (``PipelineRun.cpu_runner``), the same split as
:mod:`composer.rustapp.adapter`. One function that did both could not be placed on either side
of that line.

Two outcomes are refusals, both :class:`~composer.spec.cvlr.scaffold.Blocked`: a package that
builds no loadable object, and a CVLR pin that does not match the platform generation the
project is already on.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from composer.cargo.metadata import CargoUnavailable, CratePackage, Workspace, read_workspace
from composer.cargo.session import CargoSession, CompileFailed, Compiled, WarmFailed
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.crates import CvlrSources, VersionGap, resolve
from composer.spec.cvlr.scaffold import (
    ScaffoldBlocked,
    ScaffoldPlan,
    apply,
    plan_scaffold,
)
from composer.spec.cvlr_reference import reference_for

_log = logging.getLogger(__name__)


class PreflightFailed(RuntimeError):
    """The project cannot be prepared for verification, so the run stops.

    Separate from :class:`composer.rustapp.adapter.PreflightFailed`, which says the same thing
    for the Rust wheel. The message is what a reader sees, and it names this backend.
    """


@dataclass(frozen=True)
class CvlrPreflight:
    """What preflight learned. The pipeline carries this to ``prepare_system`` as ``Pre``.

    The scaffold plan is kept with the outcome. It records what was written, including a run
    that wrote nothing because the project was already set up.
    """

    workspace_root: Path
    package: str
    #: The package directory relative to the workspace root. The harness module is written here,
    #: and the build names this crate. Taken from the workspace the scaffold already used.
    package_dir: Path
    #: The library target's file stem. The built ``.so`` is named after it.
    artifact_stem: str
    scaffold: ScaffoldPlan
    applied: tuple[Path, ...]
    #: The CVLR crates the scaffolded graph resolves. Read after applying. Before that the
    #: project may not depend on CVLR at all.
    sources: CvlrSources
    #: Where the resolved crates and the reference set disagree. Reported, not corrected. The
    #: project's own pin wins, and corpus recall has to be read against that version.
    gaps: tuple[VersionGap, ...]

    def describe(self) -> str:
        lines = [self.scaffold.describe()]
        versions = ", ".join(f"{c.name} {c.version}" for c in self.sources.crates)
        lines.append(f"CVLR resolved: {versions or 'nothing'}")
        lines += [f"  gap: {g.describe()}" for g in self.gaps]
        return "\n".join(lines)


def _pick_package(workspace: Workspace, requested: str | None) -> CratePackage:
    """The package to verify.

    An explicit name is required when more than one member has a library target. Which program
    is under verification is not something the repository layout decides."""
    if requested is not None:
        member = workspace.member(requested)
        if member is None:
            raise PreflightFailed(
                f"{requested!r} is not a member of the workspace at {workspace.root} "
                f"(members: {', '.join(m.name for m in workspace.members)})"
            )
        return member
    verifiable = [m for m in workspace.members if m.lib is not None]
    if len(verifiable) == 1:
        return verifiable[0]
    raise PreflightFailed(
        f"the workspace at {workspace.root} has {len(verifiable)} packages with a library target "
        f"({', '.join(m.name for m in verifiable)}); name the one to verify"
    )


@dataclass(frozen=True)
class SelectedPackage:
    """Which package will be verified, resolved before any files are written.

    :func:`prepare_workspace` reaches the same package and then scaffolds. This only reads the
    workspace, so a workspace that needs a package named fails before anything is written.
    """

    workspace_root: Path
    name: str
    #: The package directory relative to the workspace root, the same relation as
    #: :attr:`CvlrPreflight.package_dir`. Both come from one workspace read.
    package_dir: Path


async def select_package(
    project_root: Path, package: str | None = None, *, main_source: Path | None = None
) -> SelectedPackage:
    """Resolve ``package`` against the workspace at ``project_root``.

    An explicit name wins. Otherwise the member that owns the main program's source file wins,
    which is cargo's own view of which member a path belongs to. A workspace with several
    library crates, which :func:`_pick_package` refuses on its own, needs no second flag.
    When neither a name nor a source file is available, the single-library rule applies,
    including its refusal.
    """
    workspace = await _workspace_at(project_root)
    owner = workspace.owning(main_source) if main_source is not None else None
    member = (
        owner
        if package is None and owner is not None and owner.lib is not None
        else _pick_package(workspace, package)
    )
    return SelectedPackage(
        workspace_root=workspace.root,
        name=member.name,
        package_dir=member.root.resolve().relative_to(workspace.root.resolve()),
    )


async def _workspace_at(root: Path, *, features: tuple[str, ...] = ()) -> Workspace:
    try:
        workspace = await read_workspace(root, features=features)
    except CargoUnavailable as exc:
        raise PreflightFailed(str(exc)) from exc
    if workspace is None:
        raise PreflightFailed(
            f"cargo could not resolve a workspace at {root}. What cargo said is logged as a "
            f"warning by composer.cargo.metadata — most often a manifest that does not parse, a "
            f"`workspace = true` dependency the root does not declare, or a graph that needs the "
            f"network"
        )
    return workspace


async def prepare_workspace(
    project_root: Path, *, package: str | None = None, chain: str = "solana"
) -> CvlrPreflight:
    """Scaffold ``project_root`` and report what a run needs to know about it.

    Writes into the project it is given. For a pipeline run that is the copy the run owns. The
    scaffold never overwrites, so an already-scaffolded project is only read.
    """
    reference = reference_for(chain)
    workspace = await _workspace_at(project_root)
    member = _pick_package(workspace, package)

    plan = plan_scaffold(workspace, member, reference)
    _log.info("%s", plan.describe())
    try:
        applied = apply(plan, workspace.root)
    except ScaffoldBlocked as exc:
        raise PreflightFailed(str(exc)) from exc

    # Re-read with the verification feature, from the package directory. The scaffold adds CVLR
    # to the manifests, so the graph from before that does not contain those crates. They are
    # optional, so a default-feature read still reports them absent
    # (:func:`composer.cargo.metadata.read_workspace_sync`). Features resolve against the package
    # cargo considers current.
    resolved_in = await _workspace_at(member.root, features=(DEFAULT_FEATURE,))
    fresh = resolved_in.member(member.name) or member
    if fresh.lib is None:
        raise PreflightFailed(f"{fresh.name} has no library target to build")

    sources = resolve(resolved_in)
    return CvlrPreflight(
        workspace_root=resolved_in.root,
        package=fresh.name,
        package_dir=fresh.root.resolve().relative_to(resolved_in.root.resolve()),
        artifact_stem=fresh.lib.artifact_stem,
        scaffold=plan,
        applied=applied,
        sources=sources,
        gaps=sources.gaps(reference),
    )


async def gate_workspace(
    pre: CvlrPreflight,
    *,
    sandbox: SandboxConfig,
    features: tuple[str, ...] = (DEFAULT_FEATURE,),
) -> None:
    """Check that the scaffolded project compiles with the harness in, or fail the run.

    Host-target ``cargo check`` only. What this has to catch is a scaffold that does not compile.
    The SBF build is a later gate, and running it here would cost more than the failure it would
    be catching.
    """
    session = CargoSession(workdir=pre.workspace_root, sandbox=sandbox)
    warmed = await session.warm()
    if isinstance(warmed, WarmFailed):
        raise PreflightFailed(
            f"could not fetch the dependency graph for {pre.workspace_root} "
            f"(exit {warmed.exit_code}):\n{warmed.diagnostics}"
        )
    run = await session.check(package=pre.package, features=features)
    _log.info(
        "preflight gate: %s in %dms%s",
        "ok" if run.ok else "FAILED",
        run.duration_ms,
        "" if run.confined else " (UNCONFINED)",
    )
    match run.verdict:
        case Compiled():
            return
        case CompileFailed(diagnostics=diagnostics):
            raise PreflightFailed(
                f"the scaffolded {pre.package} does not compile with --features "
                f"{','.join(features)}:\n{diagnostics}"
            )
