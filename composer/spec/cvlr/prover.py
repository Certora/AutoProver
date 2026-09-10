"""Submit a CVLR artifact to the Certora Solana or Soroban Prover.

``certoraSolanaProver`` and ``certoraSorobanProver`` write the same treeView
reports as ``certoraRun``. Cloud polling (:mod:`composer.prover.cloud`), the
treeView parse (:mod:`composer.prover.results`), and the rule roll-up
(:func:`composer.prover.core.run_prover`) are shared. This module builds the
program, writes the chain's conf, and submits it through that runner.

A submission has three steps:

1. Build, confined: sBPF for Solana (:mod:`composer.cargo.sbf`), wasm for
   Soroban (:mod:`composer.cargo.wasm`). A failure here is a compiler error.
   The same failure during submission is a ``CertoraUserInputError`` after the
   upload.
2. Write the build script and the conf, outside the tree the build can write.
   The prover reruns that build.
3. Submit. The caller passes :class:`~composer.prover.core.ProverOptions`
   with the chain's CLI selected.
"""

from dataclasses import dataclass, field
import logging
from pathlib import Path

from composer.cargo import wasm
from composer.cargo.sbf import (
    Built,
    SbfBuild,
    SbfRun,
    sbf_build,
    write_build_script,
)
from composer.cargo.wasm import WasmBuild, WasmIdentity, WasmRun
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
    CheckVerdicts,
    ConfPurpose,
    TunableConf,
    RunOverlay,
    solana_conf,
    soroban_conf,
)

_log = logging.getLogger(__name__)

#: One slow-tier chain build, whichever chain ran it.
type ChainRun = SbfRun | WasmRun


@dataclass(frozen=True)
class BuildRejected:
    """The pre-submission build failed, so nothing was submitted."""

    build: ChainRun


@dataclass(frozen=True)
class SubmissionFailed:
    """The build succeeded but the prover run did not produce results.

    ``reason`` is the message from :func:`~composer.prover.core.run_prover`: a
    certoraRun exception, a cloud job that did not succeed, or an unparseable
    treeView. The runner reports all of those as one string.
    """

    build: ChainRun
    reason: str


@dataclass(frozen=True)
class Checked:
    """The prover ran and returned per-rule outcomes. Rules inside may still have failed."""

    build: ChainRun
    report: ProverReport

    @property
    def link(self) -> str:
        return self.report.link


type CvlrOutcome = BuildRejected | SubmissionFailed | Checked


@dataclass(frozen=True)
class Submission:
    """One submission, apart from the session it runs in.

    ``manifest_path`` is the program's ``Cargo.toml``, under the session's workdir, which is the
    only tree the build may write. Either build compiles one package's library, not the workspace.
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
    #: unit. See :class:`~composer.spec.cvlr.conf.RunOverlay`. Solana-only:
    #: :func:`~composer.spec.cvlr.conf.soroban_conf` refuses one.
    summaries: tuple[Path, ...] = ()
    purpose: ConfPurpose = CheckVerdicts()
    #: Set for a Soroban submission, which builds wasm; ``None`` builds sBPF for Solana.
    wasm: WasmIdentity | None = None


def _chain_build(session: CargoSession, submission: Submission) -> SbfBuild | WasmBuild:
    """The build used for the pre-submission build and the prover's rerun."""
    manifest_path = session.workdir / submission.manifest_path
    if submission.wasm is not None:
        return WasmBuild(
            manifest_path=manifest_path, identity=submission.wasm, features=submission.features
        )
    return SbfBuild(
        manifest_path=manifest_path,
        features=submission.features,
        tools_version=PLATFORM_TOOLS_VERSION,
    )


async def build_for_submission(session: CargoSession, submission: Submission) -> ChainRun:
    """Run this submission's chain build."""
    match _chain_build(session, submission):
        case WasmBuild() as build:
            return await wasm.wasm_build(session, build)
        case SbfBuild() as build:
            return await sbf_build(session, build)


async def write_submission(session: CargoSession, submission: Submission, *, into: Path) -> Path:
    """Write the build script and the conf into ``into``, and return the conf's path.

    The prover reads the conf and runs the script unconfined, so ``into`` must be
    a directory no build in ``session`` can write
    (:func:`~composer.cargo.sbf.write_build_script`). The caller creates and
    removes it.
    """
    match _chain_build(session, submission):
        case WasmBuild() as build:
            script = await wasm.write_build_script(session, build, into=into, name=submission.stem)
            make_conf = soroban_conf
        case SbfBuild() as build:
            script = await write_build_script(session, build, into=into, name=submission.stem)
            make_conf = solana_conf
    conf = make_conf(
        submission.settings,
        RunOverlay(
            build_script=script,
            rules=submission.rules,
            msg=submission.msg,
            summaries=submission.summaries,
            purpose=submission.purpose,
        ),
    )
    conf_path = script.with_suffix(".conf")
    conf_path.write_text(dump_conf(conf))
    return conf_path


@dataclass(frozen=True)
class Prepared:
    """A built artifact and the conf that will verify it. Nothing has been sent to the cloud.

    ``session`` is the one that built the artifact and wrote the conf. The
    prover reruns the build in it.
    """

    session: CargoSession
    build: ChainRun
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
    return Prepared(session, build, await write_submission(session, submission, into=into))


async def run_submission(
    prepared: Prepared,
    *,
    prover_opts: ProverOptions,
    cex: CexHandler | None,
    callbacks: ProverCallbacks | None = None,
    tool_call_id: str = "cvlr-submit",
) -> CvlrOutcome:
    """Hand the conf to the prover and return the outcome.

    ``prover_opts.app`` must name the submission's chain CLI. Another app fails at
    the first conf key it does not recognize.
    """
    result = await run_prover(
        prepared.session.workdir,
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
    cex: CexHandler | None,
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
        prepared,
        prover_opts=prover_opts,
        callbacks=callbacks,
        cex=cex,
        tool_call_id=tool_call_id,
    )
