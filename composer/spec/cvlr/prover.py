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

#: The cargo feature that compiles the verification module into the program. Every surveyed project
#: and the recommended starting point agree on the name (``certora = ["no-entrypoint", "dep:cvlr",
#: …]``); it is a default rather than a constant because a project is free to call it something else
#: and the conf's ``cargo_features`` is where it would say so.
DEFAULT_FEATURE = "certora"


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
    #: or ``certora``" — resolved in :func:`submit` so the gate build and the prover's rerun cannot
    #: end up with different feature sets.
    features: tuple[str, ...] = ()

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
        RunOverlay(build_script=str(script), rules=submission.rules, msg=submission.msg),
    )
    conf_path = session.workdir / CONF_DIR / f"{submission.stem}.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(dump_conf(conf))
    return conf_path


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

    ``prover_opts.app`` must select the Solana CLI; it is not forced here, because forcing it would
    hide the one case where a caller legitimately disagrees (a Soroban submission reaching this same
    code once §7.9 lands), and a mismatched app fails loudly at the first conf key the wrong CLI does
    not recognize.

    ``cex`` defaults to the no-analysis handler: a caller with no LLM is the normal case at this
    layer, and the alternative — requiring one to get verdicts — is what would make the plumbing
    untestable without an agent.
    """
    build = await build_for_submission(session, submission, timeout_s=build_timeout_s)
    if not isinstance(build.verdict, Built):
        return BuildRejected(build)

    conf_path = await write_submission(session, submission, timeout_s=build_timeout_s)
    result = await run_prover(
        session.workdir,
        [str(conf_path)],
        tool_call_id,
        prover_opts,
        callbacks if callbacks is not None else ProverCallbacks(),
        cex if cex is not None else UnanalyzedCexHandler(),
    )
    if isinstance(result, str):
        return SubmissionFailed(build, result)
    return Checked(build, result)
