"""Submitting a CVLR artifact to the Certora Solana Prover.

The whole point of this module is how little is in it. ``docs/cvlr-backend-plan.md`` §5.3 says half
of counterexample handling is free because ``certoraSolanaProver`` produces the same treeView
reports as ``certoraRun``; this is where that claim gets cashed. Cloud polling
(:mod:`composer.prover.cloud`), the treeView parse (:mod:`composer.prover.results`) and the rule
roll-up (:func:`composer.prover.core.run_prover`) are already chain-neutral and are reused verbatim.
What Solana adds is which CLI takes the conf — one field on
:class:`~composer.prover.core.ProverOptions` — and what has to exist before the conf is written.

That last part is the substance. A submission is three ordered steps, and the order is the design:

1. **Build**, confined, as the pre-submission gate (:mod:`composer.cargo.sbf`). A build failure is a
   compiler error with a span in it; the same failure discovered during submission is a
   ``CertoraUserInputError`` from inside a prover run, which is strictly worse to read and arrives
   after the upload.
2. **Write the build script and the conf**, so the prover reruns exactly the build that just passed.
3. **Submit**, and let the shared machinery do the rest.

Nothing here is LLM-shaped. The authoring loop (§7.5) will wrap this, but the plumbing is complete
and testable without it, which is what makes phase 1b's exit criterion — a hand-written CVLR rule
in, verdicts out — checkable with no agent involved.
"""

import dataclasses
import logging
from pathlib import Path

from composer.cargo.sbf import BUILD_TIMEOUT_S, Built, SbfRun, sbf_build, write_build_script
from composer.cargo.session import CargoSession
from composer.prover.core import (
    CexHandler,
    ProverCallbacks,
    ProverOptions,
    ProverReport,
    UnanalyzedCexHandler,
    run_prover,
)
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    InheritRules,
    RuleSelection,
    RunOverlay,
    cargo_features,
    dump_conf,
    sbf_arch,
    solana_conf,
    tools_version,
)

_log = logging.getLogger(__name__)

#: Where a run's conf lands inside the workdir. The CVL backend's ``certora/confs/<stem>.conf``
#: convention (``docs/formalization-abstraction.md`` §6), kept because the deliverable layout is
#: shared and a reader who knows one backend should not have to learn a second place to look.
CONF_DIR = Path("certora") / "confs"



@dataclasses.dataclass(frozen=True)
class BuildRejected:
    """The pre-submission build failed, so nothing was submitted."""

    build: SbfRun


@dataclasses.dataclass(frozen=True)
class SubmissionFailed:
    """The build succeeded but the prover run did not produce results.

    ``reason`` is whatever the shared runner reported — a certoraRun exception, a cloud job that
    ended in a non-success status, an unparseable treeView. Kept as prose because every one of those
    is a different failure and none of them is actionable by type."""

    build: SbfRun
    reason: str


@dataclasses.dataclass(frozen=True)
class Checked:
    """The prover ran and returned per-rule outcomes. Rules inside may still have failed."""

    build: SbfRun
    report: ProverReport

    @property
    def link(self) -> str:
        return self.report.link


type CvlrOutcome = BuildRejected | SubmissionFailed | Checked


@dataclasses.dataclass(frozen=True)
class Submission:
    """Everything one submission needs that is not the session it runs in.

    ``manifest_path`` names the crate to build — the program's ``Cargo.toml``, not the workspace's,
    since ``cargo certora-sbf`` builds one package's library. ``base_conf`` is the project's own
    prover configuration, already parsed; the run-owned keys on top of it are
    :data:`~composer.spec.cvlr.conf.OVERLAY_OWNED_KEYS` and nothing else.
    """

    manifest_path: Path
    base_conf: dict
    rules: RuleSelection = dataclasses.field(default_factory=InheritRules)
    msg: str = ""
    #: Conf file stem, which is also this submission's identity on disk.
    stem: str = "cvlr"
    #: Cargo features for the build. Empty means "whatever the base conf's ``cargo_features`` says,
    #: or ``certora``" — resolved in :func:`prepare_submission` so the gate build and the prover's
    #: rerun cannot end up with different feature sets. An authoring run always names two: the
    #: harness feature and the unit's own, which is what selects one unit's rules out of a shared
    #: crate (``docs/single-working-tree.md`` §2.1).
    features: tuple[str, ...] = ()
    #: Points-to summary files this submission reads, workdir-relative. One per unit; see
    #: :class:`~composer.spec.cvlr.conf.RunOverlay`.
    summaries: tuple[str, ...] = ()

    def resolved_features(self) -> tuple[str, ...]:
        return self.features or cargo_features(self.base_conf) or (DEFAULT_FEATURE,)


async def build_for_submission(
    session: CargoSession, submission: Submission, *, timeout_s: int = BUILD_TIMEOUT_S
) -> SbfRun:
    """The slow tier, configured from the conf.

    The conf's ``cargo_tools_version`` and ``solana_sbf_arch`` reach the build from here rather than
    from the prover. On the CLI's own from-sources path they are its build's settings; since this
    backend owns the build, honoring them here is what keeps a project's declaration meaningful
    instead of silently inert."""
    return await sbf_build(
        session,
        manifest_path=submission.manifest_path,
        features=submission.resolved_features(),
        tools_version=tools_version(submission.base_conf),
        arch=sbf_arch(submission.base_conf),
        timeout_s=timeout_s,
    )


async def write_submission(
    session: CargoSession, submission: Submission, *, timeout_s: int = BUILD_TIMEOUT_S
) -> Path:
    """Write the build script and the conf, and return the conf's path.

    Split out from :func:`submit` because it is the whole deliverable of a dry run: the pair of
    files is what a developer reruns by hand, and what the artifact store persists alongside the
    generated Rust."""
    script = await write_build_script(
        session,
        manifest_path=submission.manifest_path,
        features=submission.resolved_features(),
        tools_version=tools_version(submission.base_conf),
        arch=sbf_arch(submission.base_conf),
        timeout_s=timeout_s,
    )
    conf = solana_conf(
        submission.base_conf,
        RunOverlay(
            build_script=str(script),
            rules=submission.rules,
            msg=submission.msg,
            summaries=submission.summaries,
        ),
    )
    conf_path = session.workdir / CONF_DIR / f"{submission.stem}.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(dump_conf(conf))
    return conf_path


@dataclasses.dataclass(frozen=True)
class Prepared:
    """A built ``.so`` and the conf that will verify it — everything before the cloud is involved."""

    build: SbfRun
    conf_path: Path


async def prepare_submission(
    session: CargoSession, submission: Submission, *, timeout_s: int = BUILD_TIMEOUT_S
) -> BuildRejected | Prepared:
    """The local half: build the program with the harness in, then write the conf that checks it.

    Split from :func:`run_submission` because only this half touches the working tree, and the two
    halves want different concurrency. Every unit of a run shares one tree and one ``target/``, so
    this is what a caller holds the run's build permit across
    (``docs/single-working-tree.md`` §2.4); the other half waits on a cloud job for minutes and
    holding a permit across it would serialize the whole run behind one prover.
    """
    build = await build_for_submission(session, submission, timeout_s=timeout_s)
    if not isinstance(build.verdict, Built):
        return BuildRejected(build)
    return Prepared(build, await write_submission(session, submission, timeout_s=timeout_s))


async def run_submission(
    session: CargoSession,
    prepared: Prepared,
    *,
    prover_opts: ProverOptions,
    callbacks: ProverCallbacks | None = None,
    cex: CexHandler | None = None,
    tool_call_id: str = "cvlr-submit",
) -> CvlrOutcome:
    """The remote half: hand the conf to the prover and shape what comes back.

    ``prover_opts.app`` must select the Solana CLI; it is not forced here, because forcing it would
    hide the one case where a caller legitimately disagrees (a Soroban submission reaching this same
    code once §7.9 lands), and a mismatched app fails loudly at the first conf key the wrong CLI does
    not recognize.

    ``cex`` defaults to the no-analysis handler: a caller with no LLM is the normal case at this
    layer, and the alternative — requiring one to get verdicts — is what would make the plumbing
    untestable without an agent.
    """
    result = await run_prover(
        session.workdir,
        [str(prepared.conf_path)],
        tool_call_id,
        prover_opts,
        callbacks if callbacks is not None else ProverCallbacks(),
        cex if cex is not None else UnanalyzedCexHandler(),
    )
    if isinstance(result, str):
        return SubmissionFailed(prepared.build, result)
    return Checked(prepared.build, result)


async def submit(
    session: CargoSession,
    submission: Submission,
    *,
    prover_opts: ProverOptions,
    callbacks: ProverCallbacks | None = None,
    cex: CexHandler | None = None,
    tool_call_id: str = "cvlr-submit",
    build_timeout_s: int = BUILD_TIMEOUT_S,
) -> CvlrOutcome:
    """Build, configure, and verify — the deterministic half of the CVLR backend, end to end.

    The two halves in one call, for a caller with no working tree to share: the deterministic gates
    and the anchor-reach probe both own their workspace outright. The authoring loop calls the
    halves separately so it can hold the run's build permit across the first and not the second.
    """
    prepared = await prepare_submission(session, submission, timeout_s=build_timeout_s)
    if isinstance(prepared, BuildRejected):
        return prepared
    return await run_submission(
        session,
        prepared,
        prover_opts=prover_opts,
        callbacks=callbacks,
        cex=cex,
        tool_call_id=tool_call_id,
    )
