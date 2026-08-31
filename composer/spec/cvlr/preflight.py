"""Preflight for a CVLR run: scaffold the project, then prove the scaffold builds.

The seam is ``docs/formalization-abstraction.md`` §2: ``PipelineBackend.preflight`` runs in the same
task group as system analysis, so whichever side fails first cancels the other. That is what makes
a *gate* here worth more than the same check later — a project that cannot compile with the harness
in stops the run having spent at most one partial analysis agent, instead of surfacing as
unfixable compiler errors in the first authored draft after the whole extraction phase.

Two halves, split the way :mod:`composer.rustapp.adapter` splits its own preflight, and for the same
reason: :func:`prepare_workspace` is bookkeeping and file writes, while :func:`gate_workspace` is a
compile. A compile belongs to the run's CPU budget rather than its agent budget, so the backend
wraps only the second in ``run.cpu_runner`` — and a function that did both could not be placed on
either side of that line.

Nothing here is LLM-shaped, which is the finding §7.4 was asking about. The phase was scoped as
"agent-assisted only where a template genuinely cannot decide", and the two places a template must
not decide turn out to be refusals rather than judgements: a package that builds no loadable object,
and a CVLR pin that contradicts the platform generation the project is already on. Both are
:class:`~composer.spec.cvlr.scaffold.Blocked`, and both want a human rather than a model.

There is no ``CvlrBackend`` yet — that is §7.5, with the authoring loop. These functions are
complete and tested without one, the same way phase 1b's submission plumbing was.
"""

import dataclasses
import logging
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
    """The project cannot be prepared for verification, so the run should stop.

    Distinct from :class:`composer.rustapp.adapter.PreflightFailed`, which says the same thing for
    the Rust wheel backend. Two types rather than one shared one because the message is the only
    thing a reader gets and knowing *which* backend gave up on the workspace is part of it.
    """


@dataclasses.dataclass(frozen=True)
class CvlrPreflight:
    """What preflight learned, carried to ``prepare_system`` as the pipeline's opaque ``Pre``.

    The scaffold plan is kept, not just its outcome. A run that reports "verified" against a project
    it silently edited is not reproducible, and the plan is the record of what changed — including
    the case where it changed nothing because the project was already set up.
    """

    workspace_root: Path
    package: str
    #: The package's directory relative to the workspace root — where the harness module goes, and
    #: which crate the build names. Carried rather than recomputed: the scaffold already resolved it
    #: against this same workspace, and a second derivation is a second chance to disagree.
    package_dir: Path
    #: The library target's file stem, which is what the built ``.so`` is named after.
    artifact_stem: str
    scaffold: ScaffoldPlan
    applied: tuple[Path, ...]
    #: The CVLR crates the *scaffolded* graph resolves — read after applying, because before it the
    #: project may not depend on CVLR at all and the answer would be "none".
    sources: CvlrSources
    #: Where the resolved crates and the knowledge corpus's reference set disagree. Reported rather
    #: than corrected: the project's own pin wins (see the scaffold), and recall from the corpus is
    #: what has to be qualified.
    gaps: tuple[VersionGap, ...]

    def describe(self) -> str:
        lines = [self.scaffold.describe()]
        versions = ", ".join(f"{c.name} {c.version}" for c in self.sources.crates)
        lines.append(f"CVLR resolved: {versions or 'nothing'}")
        lines += [f"  gap: {g.describe()}" for g in self.gaps]
        return "\n".join(lines)


def _pick_package(workspace: Workspace, requested: str | None) -> CratePackage:
    """The package to verify.

    Named explicitly whenever there is a choice. Guessing among members is exactly the decision
    §5.6 says belongs outside a template: which program is under verification is a fact about the
    engagement, not about the repository layout."""
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

    Writes into the project it is given, which for a pipeline run is the copy the run owns. The
    scaffold never overwrites, so pointing this at an already-verified project is a read.
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

    # Re-read against the *verification* feature, from the package's own directory. Two reasons,
    # and both were found by running this: applying the scaffold adds CVLR to the manifests, so the
    # graph read before it does not contain the crates every later step resolves versions from; and
    # the scaffold declares them `optional = true`, so even afterwards a default-feature read
    # reports them absent (:func:`composer.cargo.metadata.read_workspace_sync`).
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
    """Prove the scaffolded project compiles with the harness in, or fail the run.

    The fast tier only (``cargo check`` on the host target). The slow SBF build is the
    pre-submission gate and belongs there: what this has to catch is a scaffold that does not
    compile, and paying for a chain build to learn that would make the gate cost more than the
    phase it protects.
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
