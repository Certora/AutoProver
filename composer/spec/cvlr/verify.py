"""The gate tools.

``cargo_check`` compiles the draft on the host and is not a stamp.
``verify_rules`` builds it for the chain, submits the rules the build listed,
and stamps the draft when every one of them verified. A build that fails
reports the compiler error and submits nothing
(:class:`~composer.spec.cvlr.prover.BuildRejected`).
"""

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, override

from langchain_core.tools import BaseTool

from graphcore.graph import tool_return, tool_state_update
from graphcore.tools.schemas import (
    Command,
    WithAsyncDependencies,
    WithInjectedId,
    WithInjectedState,
)

from composer.authoring.state import ValidationStamper
from composer.diagnostics.timing import get_run_summary
from composer.cargo.features import CargoFeature
from composer.cargo.sbf import Built
from composer.cargo.session import CargoSession, CompileFailed, Compiled
from composer.prover.core import (
    CexHandler,
    CexProgressCallbacks,
    ProverCallbacks,
    ProverOptions,
)
from composer.prover.ptypes import RuleResult
from composer.prover.conf import SelectRules
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    CvlrOutcome,
    Prepared,
    Submission,
    SubmissionFailed,
    build_for_submission,
    run_submission,
    write_submission,
)
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing, rule_names_in_elf
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr.state import CvlrGenerationState
from composer.spec.cvlr.tree import SharedTree, UnitEdits
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)


class BuildPermit:
    """Held from staging until the prover reports the job link, or until the slot ends."""

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._held = False

    async def acquire(self) -> None:
        await self._sem.acquire()
        self._held = True

    def release(self) -> None:
        """A second call does nothing."""
        if self._held:
            self._held = False
            self._sem.release()


@dataclasses.dataclass(frozen=True)
class HarnessTarget:
    """Where a draft is staged so the crate compiles with it.

    Staging writes the file ``specs/mod.rs`` declares for :attr:`unit`
    (:meth:`composer.spec.cvlr.harness.CvlrArtifactStore.declare_modules`).
    Every unit shares ``tree`` and ``build_sem``.
    """

    session: CargoSession
    package: str
    package_root: Path
    #: The module name the tree keys edits by, and the cargo feature that selects
    #: this unit.
    unit: HarnessModule
    tree: SharedTree
    build_sem: asyncio.Semaphore

    @property
    def features(self) -> tuple[CargoFeature, ...]:
        """Features for every build of this unit: the harness, and this unit's module."""
        return (DEFAULT_FEATURE, self.unit.feature)

    @property
    def package_dir(self) -> Path:
        """The package directory relative to the session workdir.

        That relative path is how a build is told which package to compile.
        """
        return self.package_root.relative_to(self.session.workdir)

    @property
    def module_path(self) -> Path:
        return self.package_root / SPECS_DIR / self.unit.artifact_file

    @asynccontextmanager
    async def build_slot(self) -> AsyncIterator[BuildPermit]:
        """Serialize staging and the cargo invocation that follows it.

        Cargo's build-directory lock already serializes the builds. A build
        waiting on that lock still holds its sandbox grants, so the permit
        queues them here. A submission holds the permit longer
        (:func:`_stage_and_submit`).
        """
        permit = BuildPermit(self.build_sem)
        await permit.acquire()
        try:
            yield permit
        finally:
            permit.release()

    async def stage(self, draft: str) -> None:
        """Write ``draft`` into this unit's module. Call it while holding :meth:`build_slot`."""
        await self.tree.reconcile(
            self.unit.module,
            UnitEdits(module_path=self.module_path, draft=draft),
        )


class _RunAccounting(ProverCallbacks):
    """Records the prover link, the prover's runtime, and the wall clock.

    ``ProverCallbacks`` defaults do nothing, so an event left on the base class
    records nothing. ``summary.format()``, the per-task link, and
    ``job_info.json``'s ``prover_usage`` read these records.
    """

    def __init__(self) -> None:
        super().__init__()
        self._started_mono: float | None = None
        self._upload_permit: BuildPermit | None = None

    def release_on_upload(self, permit: BuildPermit) -> None:
        """Release ``permit`` when the Prover reports the job's link, which is after the upload."""
        self._upload_permit = permit

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        self._started_mono = time.perf_counter()

    @override
    async def on_prover_link(self, link: str) -> None:
        if self._upload_permit is not None:
            self._upload_permit.release()
        get_run_summary().record_prover_link(link)

    @override
    async def on_prover_runtime(self, ms: int) -> None:
        """The prover's start-to-end time, without queue wait.

        The wall clock in :meth:`on_prover_result` includes time spent waiting
        in the queue.
        """
        get_run_summary().record_prover_runtime(ms)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        if self._started_mono is not None:
            get_run_summary().add_prover_call(time.perf_counter() - self._started_mono)
            self._started_mono = None


@dataclasses.dataclass(frozen=True)
class VerifyDeps:
    """What the prover gate needs beyond the draft."""

    target: HarnessTarget
    submission: Submission
    #: The directory :func:`composer.spec.cvlr.prover.prepare_submission` writes into.
    #: Outside the tree. The confined build can write the tree, so the conf and
    #: the build script must not live there.
    submissions: Path
    prover_opts: ProverOptions
    stamper: ValidationStamper
    #: One submission at a time per unit. A second run would build into the
    #: same workdir and race the staged module, so the tool refuses it.
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


@tool_display("Compiling the harness", "Compile")
class CargoCheck(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Compile the current draft against the program, on the host target.

    Takes seconds and does not submit a job. Call it after every edit. It
    catches an unknown macro, a wrong signature, or a misused derive before a
    prover run.

    It does not catch errors only the chain build reports. It does not replace
    ``verify_rules``.
    """

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        with self.tool_deps() as target:
            async with target.build_slot():
                await target.stage(draft)
                run = await target.session.check(
                    manifest_dir=target.package_dir, features=target.features
                )
        match run.verdict:
            case Compiled():
                return tool_return(
                    self.tool_call_id,
                    f"Compiled in {run.duration_ms}ms.",
                )
            case CompileFailed(diagnostics=diagnostics):
                return tool_return(
                    self.tool_call_id,
                    f"Does not compile:\n{diagnostics}",
                )


def _unit_submission(deps: VerifyDeps) -> Submission:
    """The unit's submission, compiling this unit's module.

    :attr:`HarnessTarget.features` is ``certora`` plus this unit's feature, the
    same set ``cargo_check`` builds with. The submission's own default is only
    ``certora``, and ``specs/mod.rs`` gates this unit's module on its feature,
    so a build that kept the default would compile the staged file out.
    """
    return dataclasses.replace(deps.submission, features=deps.target.features)


def _submission_for(deps: VerifyDeps, rules: Sequence[str]) -> Submission:
    return dataclasses.replace(_unit_submission(deps), rules=SelectRules(tuple(rules)))


def _unread(err: ManifestMissing | ManifestCorrupt) -> str:
    if isinstance(err, ManifestMissing):
        return (
            "The .so has no `.cvlr_rules` section, so the rules were not listed. "
            "Nothing was submitted."
        )
    if err.offset is None:
        return "The `.cvlr_rules` section could not be read. Nothing was submitted."
    return (
        f"The `.cvlr_rules` section could not be read at offset {err.offset}. "
        "Nothing was submitted."
    )


class RuleStatusReport(CexHandler):
    """Each rule's status, the prover's messages, and its counterexample dump.

    ``flat_rule_feedback.j2`` says the analysis found nothing when a violated
    rule has no explanation. That sentence would describe an analysis that ran.
    """

    @override
    async def analyze(
        self,
        all_results: list[RuleResult],
        tool_call_id: str,
        callbacks: CexProgressCallbacks,
        report_dir: Path,
    ) -> str:
        lines: list[str] = []
        for rule in all_results:
            lines.append(f"{rule.name}: {rule.status}")
            for message in rule.error_messages or ():
                lines.append(f"  {message}")
            if rule.cex_dump:
                lines.append(f"  {rule.cex_dump}")
        return "\n".join(lines)


async def _stage_and_submit(
    deps: VerifyDeps,
    draft: str,
    *,
    callbacks: _RunAccounting,
    tool_call_id: str,
    choose: Callable[[set[str]], Submission | str],
) -> CvlrOutcome | str:
    """Stage ``draft``, build this unit, and submit what ``choose`` returns.

    ``choose`` sees the rule names in the ``.so`` and returns the submission to
    write, or a message. A message, or a missing or unreadable section, writes
    no conf.

    Every SBF build of the crate writes the same ``.so``. ``run_submission``
    rebuilds it into the shared target directory and uploads it. A sibling's
    build in between would replace that ``.so`` with one built for the
    sibling's feature (:func:`composer.spec.cvlr.prover.prepare_submission`).
    The build permit is held from staging until the job's link arrives, after
    the upload, or until ``run_submission`` returns without a link. The
    prover's rebuild happens under that permit, so it lists the same rules and
    the section is not read again.

    The cloud job is not covered. A sibling that stages during it compiles
    behind its own feature.
    """
    async with deps.target.build_slot() as permit:
        await deps.target.stage(draft)
        build = await build_for_submission(deps.target.session, _unit_submission(deps))
        if not isinstance(build.verdict, Built):
            return BuildRejected(build)
        artifact = build.verdict.manifest.artifact
        try:
            names = rule_names_in_elf(artifact)
        except ManifestCorrupt as err:
            # The offset, not the section. A bad record's payload is not a log line.
            _log.warning("could not read .cvlr_rules in %s at offset %s", artifact, err.offset)
            return _unread(err)
        except ManifestMissing as err:
            return _unread(err)
        chosen = choose(names)
        if not isinstance(chosen, Submission):
            return chosen
        prepared = Prepared(
            build, await write_submission(deps.target.session, chosen, into=deps.submissions)
        )
        callbacks.release_on_upload(permit)
        return await run_submission(
            deps.target.session,
            prepared,
            prover_opts=deps.prover_opts,
            cex=RuleStatusReport(),
            callbacks=callbacks,
            tool_call_id=tool_call_id,
        )


@tool_display("Running the Solana Prover", "Prover")
class VerifyRules(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, VerifyDeps],
):
    """Build the program with the harness and check its rules with the Certora Solana Prover.

    This takes minutes and spends prover time. Get ``cargo_check`` green first.
    When every rule the build listed verified, it stamps that draft.
    """

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first."
        with self.tool_deps() as deps:
            if deps.lock.locked():
                return (
                    "A prover run for this unit is already in flight. Wait for it rather than "
                    "starting a second one."
                )
            listed: list[str] = []

            def choose(names: set[str]) -> Submission | str:
                listed[:] = sorted(names)
                if not names:
                    return (
                        "The build listed no rules, so nothing was submitted. A rule is a "
                        "`#[rule]` function or a `cvlr_rules!` invocation."
                    )
                # A conf with no `rule` entry makes the cloud job end in FAILED, with no
                # report. The section holds only this unit's rules: the build enables only
                # this unit's feature.
                return _submission_for(deps, listed)

            async with deps.lock:
                outcome = await _stage_and_submit(
                    deps,
                    draft,
                    callbacks=_RunAccounting(),
                    tool_call_id=self.tool_call_id,
                    choose=choose,
                )
            if isinstance(outcome, str):
                return outcome
            return self._report(outcome, deps, declared=listed)

    def _report(
        self,
        outcome: CvlrOutcome,
        deps: VerifyDeps,
        *,
        declared: Sequence[str],
    ) -> Command | str:
        match outcome:
            case BuildRejected(build=build):
                said = (
                    build.verdict.diagnostics
                    if isinstance(build.verdict, CompileFailed)
                    else "no diagnostics were captured"
                )
                return tool_return(
                    self.tool_call_id,
                    f"The chain build failed, so nothing was submitted:\n{said}",
                )
            case SubmissionFailed(reason=reason):
                return tool_return(
                    self.tool_call_id,
                    f"The prover run did not produce results: {reason}",
                )
            case Checked(report=report):
                status = report.rule_status
                not_verified = [name for name in declared if status.get(name) is not True]
                if not_verified:
                    return tool_state_update(
                        self.tool_call_id,
                        f"{report.result_str}\n\nNot verified: {', '.join(not_verified)}.",
                        prover_link=report.link,
                    )
                return tool_state_update(
                    self.tool_call_id,
                    f"{report.result_str}\n\nEvery declared rule verified. This draft is stamped.",
                    prover_link=report.link,
                    validations=deps.stamper(self.state),
                )


def gate_tools(deps: VerifyDeps) -> list[BaseTool]:
    """``cargo_check`` builds ``deps.target``, the same target ``verify_rules`` submits.

    Neither tool edits the program.
    """
    return [
        CargoCheck.bind(deps.target).as_tool("cargo_check"),
        VerifyRules.bind(deps).as_tool("verify_rules"),
    ]
