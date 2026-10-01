"""Submitting a CVLR artifact to the Certora Solana Prover.

``certoraSolanaProver`` produces the same treeView reports as ``certoraRun``, so cloud polling
(:mod:`composer.prover.cloud`), the treeView parse (:mod:`composer.prover.results`) and the rule
roll-up (:func:`composer.prover.core.run_prover`) are reused as they are. What Solana adds is which
CLI takes the conf, one field on :class:`~composer.prover.core.ProverOptions`, and what has to
exist before the conf is written.

A submission is three ordered steps:

1. **Build**, confined, as the pre-submission gate (:mod:`composer.cargo.sbf`). A build failure
   here is a compiler error with a span in it; the same failure during submission is a
   ``CertoraUserInputError`` from inside a prover run, after the upload.
2. **Write the build script and the conf**, so the prover reruns exactly the build that passed.
3. **Submit**.
"""

import dataclasses
import logging
from pathlib import Path

from composer.cargo.sbf import (
    BUILD_TIMEOUT_S,
    Built,
    SbfBuild,
    SbfRun,
    sbf_build,
    write_build_script,
)
from composer.cargo.session import CargoSession
from composer.layout import CERTORA_DIR
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

#: Where a run's conf lands inside the workdir. The CVL backend's ``certora/confs/<stem>.conf``
#: convention (``docs/formalization-abstraction.md`` §6), kept because the deliverable layout is
#: shared and a reader who knows one backend should not have to learn a second place to look.
CONF_DIR = CERTORA_DIR / "confs"


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
    since ``cargo certora-sbf`` builds one package's library.

    ``target_directory`` is the workspace's cargo target directory, as
    :attr:`~composer.cargo.metadata.Workspace.target_directory` reports it. Each submission builds
    in its own subdirectory of it, named by ``stem``.

    Both paths lie under the session's workdir, the only tree the build may write.
    """

    manifest_path: Path
    target_directory: Path
    settings: TunableConf = TunableConf()
    rules: RuleSelection = dataclasses.field(default_factory=InheritRules)
    msg: str = ""
    #: This submission's identity on disk: it names the conf and the build script, so
    #: submissions sharing a workdir do not overwrite each other's files.
    stem: str = "cvlr"
    #: Cargo features for both the gate build and the prover's rerun of it. Submissions that share
    #: a crate select different rules from it by naming different features.
    features: tuple[str, ...] = (DEFAULT_FEATURE,)
    #: Points-to summary files this submission reads, workdir-relative. One per unit; see
    #: :class:`~composer.spec.cvlr.conf.RunOverlay`.
    summaries: tuple[Path, ...] = ()


def _sbf_build(session: CargoSession, submission: Submission) -> SbfBuild:
    """The build both the gate and the prover's rerun run."""
    return SbfBuild(
        manifest_path=session.workdir / submission.manifest_path,
        target_dir=session.workdir / submission.target_directory / "certora" / submission.stem,
        features=submission.features,
        tools_version=PLATFORM_TOOLS_VERSION,
    )


async def build_for_submission(
    session: CargoSession, submission: Submission, *, timeout_s: int = BUILD_TIMEOUT_S
) -> SbfRun:
    """The slow tier."""
    return await sbf_build(session, _sbf_build(session, submission), timeout_s=timeout_s)


async def write_submission(
    session: CargoSession, submission: Submission, *, timeout_s: int = BUILD_TIMEOUT_S
) -> Path:
    """Write the build script and the conf, and return the conf's path.

    The pair of files is what a developer reruns by hand, from the workdir, so it is useful without
    a submission. The conf names the script relative to the workdir, as a CVL conf names its spec.
    """
    script = await write_build_script(
        session, _sbf_build(session, submission), name=submission.stem, timeout_s=timeout_s
    )
    conf = solana_conf(
        submission.settings,
        RunOverlay(
            build_script=script.relative_to(session.workdir),
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

    Separate from :func:`run_submission` so a caller sharing one tree between submissions can
    serialize this half and run the other, which waits on a cloud job for minutes, concurrently.
    The other half rebuilds too, from the tree's sources as they are then, but into this
    submission's own target directory, so it cannot replace another submission's artifact. The
    sources this submission was built from must stay as they are until the prover has uploaded
    them, which is when :meth:`~composer.prover.core.ProverCallbacks.on_prover_link` fires.
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
    cex: CexHandler,
    callbacks: ProverCallbacks | None = None,
    tool_call_id: str = "cvlr-submit",
) -> CvlrOutcome:
    """The remote half: hand the conf to the prover and shape what comes back.

    ``prover_opts.app`` must select the Solana CLI. A mismatched app fails at the first conf key
    the wrong CLI does not recognize.
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
    prover_opts: ProverOptions,
    cex: CexHandler,
    callbacks: ProverCallbacks | None = None,
    tool_call_id: str = "cvlr-submit",
    build_timeout_s: int = BUILD_TIMEOUT_S,
) -> CvlrOutcome:
    """:func:`prepare_submission` then :func:`run_submission`, for a caller that does not share its
    working tree."""
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
