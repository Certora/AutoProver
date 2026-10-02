"""Submit a CVLR artifact to the Certora Solana Prover.

``certoraSolanaProver`` writes the same treeView reports as ``certoraRun``.
Cloud polling (:mod:`composer.prover.cloud`), the treeView parse
(:mod:`composer.prover.results`), and the rule roll-up
(:func:`composer.prover.core.run_prover`) are shared. This module builds the
program, writes the Solana conf, and submits it through that runner.

A submission has three steps:

1. Build, confined (:mod:`composer.cargo.sbf`). A failure here is a compiler
   error. The same failure during submission is a ``CertoraUserInputError``
   after the upload.
2. Write the build script and the conf, outside the tree the build can write.
   The prover reruns that build.
3. Submit. The caller passes :class:`~composer.prover.core.ProverOptions`
   with the Solana CLI selected.
"""

from dataclasses import dataclass, field
import logging
from pathlib import Path

from composer.cargo.sbf import (
    Built,
    SbfBuild,
    SbfRun,
    sbf_build,
    write_build_script,
)
from composer.cargo.session import CargoSession
from composer.prover.core import (
    CexHandler,
    ProverCallbacks,
    ProverOptions,
    ProverReport,
    run_prover,
)
from composer.prover.conf import InheritRules, RuleSelection, dump_conf
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    PLATFORM_TOOLS_VERSION,
    TunableConf,
    RunOverlay,
    solana_conf,
)

_log = logging.getLogger(__name__)

@dataclass(frozen=True)
class BuildRejected:
    """The pre-submission build failed, so nothing was submitted."""

    build: SbfRun


@dataclass(frozen=True)
class SubmissionFailed:
    """The build succeeded but the prover run did not produce results.

    ``reason`` is the message from :func:`~composer.prover.core.run_prover`: a
    certoraRun exception, a cloud job that did not succeed, or an unparseable
    treeView. The runner reports all of those as one string.
    """

    build: SbfRun
    reason: str


@dataclass(frozen=True)
class Checked:
    """The prover ran and returned per-rule outcomes. Rules inside may still have failed."""

    build: SbfRun
    report: ProverReport

    @property
    def link(self) -> str:
        return self.report.link


type CvlrOutcome = BuildRejected | SubmissionFailed | Checked


@dataclass(frozen=True)
class Submission:
    """One submission, apart from the session it runs in.

    ``manifest_path`` is the program's ``Cargo.toml``, under the session's workdir, which is the
    only tree the build may write. ``cargo certora-sbf`` builds one package's library, not the
    workspace.
    """

    manifest_path: Path
    #: Names the conf and the build script. Submissions that share a directory for
    #: them keep separate files.
    stem: str
    msg: str
    settings: TunableConf = TunableConf()
    rules: RuleSelection = field(default_factory=InheritRules)
    #: Cargo features for the gate build and the prover's rerun. Submissions that
    #: share a crate select different rules by naming different features.
    features: tuple[str, ...] = (DEFAULT_FEATURE,)
    #: Points-to summary files this submission reads, workdir-relative. One per
    #: unit. See :class:`~composer.spec.cvlr.conf.RunOverlay`.
    summaries: tuple[Path, ...] = ()


def _sbf_build(session: CargoSession, submission: Submission) -> SbfBuild:
    """The ``SbfBuild`` used for the pre-submission build and the prover's rerun."""
    return SbfBuild(
        manifest_path=session.workdir / submission.manifest_path,
        features=submission.features,
        tools_version=PLATFORM_TOOLS_VERSION,
    )


async def build_for_submission(session: CargoSession, submission: Submission) -> SbfRun:
    """Run this submission's ``cargo certora-sbf`` build."""
    return await sbf_build(session, _sbf_build(session, submission))


async def write_submission(session: CargoSession, submission: Submission, *, into: Path) -> Path:
    """Write the build script and the conf into ``into``, and return the conf's path.

    The prover reads the conf and runs the script unconfined, so ``into`` must be
    a directory no build in ``session`` can write
    (:func:`~composer.cargo.sbf.write_build_script`). The caller creates and
    removes it.
    """
    script = await write_build_script(
        session, _sbf_build(session, submission), into=into, name=submission.stem
    )
    conf = solana_conf(
        submission.settings,
        RunOverlay(
            build_script=script,
            rules=submission.rules,
            msg=submission.msg,
            summaries=submission.summaries,
        ),
    )
    conf_path = script.with_suffix(".conf")
    conf_path.write_text(dump_conf(conf))
    return conf_path


@dataclass(frozen=True)
class Prepared:
    """A built ``.so`` and the conf that will verify it. Nothing has been sent to the cloud."""

    build: SbfRun
    conf_path: Path


async def prepare_submission(
    session: CargoSession, submission: Submission, *, into: Path
) -> BuildRejected | Prepared:
    """Build the program, harness included, then write the conf that checks it into ``into``.

    Split from :func:`run_submission` so a caller that shares one tree can wait
    on several submissions' cloud jobs at once. The two halves are not
    independent, though. :func:`run_submission` reruns this build, from the
    sources as they are then and into the target directory every build of the
    crate shares, and uploads the sources and the ``.so``. So from the start of
    this call until :meth:`~composer.prover.core.ProverCallbacks.on_prover_link`
    fires, nothing else may build the crate or change a file it compiles. The
    conf is not one of those: the prover reads it, the build does not. If
    ``run_submission`` returns first, the CLI failed before uploading, and the
    crate is free again.
    """
    build = await build_for_submission(session, submission)
    if not isinstance(build.verdict, Built):
        return BuildRejected(build)
    return Prepared(build, await write_submission(session, submission, into=into))


async def run_submission(
    session: CargoSession,
    prepared: Prepared,
    *,
    prover_opts: ProverOptions,
    cex: CexHandler,
    callbacks: ProverCallbacks | None = None,
    tool_call_id: str = "cvlr-submit",
) -> CvlrOutcome:
    """Hand the conf to the prover and return the outcome.

    ``prover_opts.app`` must name the Solana CLI. Another app fails at the first
    conf key it does not recognize.
    """
    result = await run_prover(
        session.workdir,
        [str(prepared.conf_path)],
        tool_call_id,
        prover_opts,
        callbacks if callbacks is not None else ProverCallbacks(),
        cex,
    )
    if isinstance(result, str):
        return SubmissionFailed(prepared.build, result)
    return Checked(prepared.build, result)


async def submit(
    session: CargoSession,
    submission: Submission,
    *,
    into: Path,
    prover_opts: ProverOptions,
    cex: CexHandler,
    callbacks: ProverCallbacks | None = None,
    tool_call_id: str = "cvlr-submit",
) -> CvlrOutcome:
    """:func:`prepare_submission`, then :func:`run_submission`.

    For a caller whose working tree is not shared with another submission.
    """
    prepared = await prepare_submission(session, submission, into=into)
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
