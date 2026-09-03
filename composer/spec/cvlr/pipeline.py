"""The CVLR backend: the pipeline's Solana-with-the-Prover entry point.

``docs/cvlr-backend-plan.md`` §7.5. Structurally this is ``NullSolanaBackend`` with a real formalizer:
the front half — the Solana model, the analysis and property prompts, ``locate_main`` — is the
ecosystem's and is reused verbatim, which is the whole reason §2 could say the front half was already
built. What this module adds is the four things that differ: a preflight that scaffolds and gates
the workspace, a staged formalizer that declares the harness modules, the per-unit authoring loop,
and verdicts read back from the prover.

**Why the formalizer is staged.** ``specs/mod.rs`` needs a ``mod`` line per unit, and a module
declared without a file is a compile error — so a unit whose sibling has not been written yet would
fail *its own* compile gate for a reason that has nothing to do with it. The declaration therefore
has to happen once, when every unit is known, which is exactly what
:class:`~composer.pipeline.core.StagedFormalizer` exists for. §5.6 predicted this shape ("one shared
certora harness module, many rules") before there was any code to hang it on.

**Why each unit gets its own workdir.** This closes open question 3, and the answer is forced rather
than preferred. The compile gate is whole-crate: ``cargo check`` on the package compiles *every*
unit's module, so in a shared workdir unit A's gate fails whenever unit B's draft is momentarily
broken — a gate that fails for another unit's reason, nondeterministically. That is worse than a slow
gate, because it is not reproducible. The cost is one dependency graph per unit, which makes the
shared read-only cargo cache §5.1 held in reserve load-bearing rather than optional; the alternatives
considered and rejected are in §7.5.2.
"""

import asyncio
import dataclasses
import enum
import logging
import shutil
from pathlib import Path
from typing import Sequence, override

from langchain_core.tools import BaseTool

from composer.cargo.session import CargoSession, WarmFailed
from composer.pipeline.core import (
    ComponentOutcome,
    CorePhases,
    Formalizer,
    PipelineRun,
    PreparedSystem,
    StagedFormalizer,
    SystemAnalysisSpec,
    ToolBinder,
)
from composer.pipeline.ptypes import BackendJob, Curtailed, Delivered, GaveUp
from composer.prover.core import ProverOptions
from composer.sandbox.config import SandboxConfig
from composer.spec.context import CvlrGeneration, WorkflowContext
from composer.spec.cvlr.author import batch_cvlr_generation
from composer.spec.cvlr.conf import DEFAULT_FEATURE, load_base
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE
from composer.spec.cvlr.harness import CvlrArtifactStore, GeneratedHarness, HarnessModule
from composer.spec.cvlr.munge import NOT_PROJECT_SOURCE, FunctionMunge
from composer.spec.cvlr.preflight import CvlrPreflight, gate_workspace, prepare_workspace
from composer.spec.cvlr.prover import Submission
from composer.spec.cvlr.scaffold import ENVS_DIR, SPECS_DIR
from composer.spec.cvlr.tuning import TuningFiles
from composer.spec.cvlr.source_tools import cvlr_source_tools, mount
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY
from composer.spec.cvlr.verify import (
    CexAnalysis,
    HarnessTarget,
    VerifyDeps,
    prover_stamper,
)
from composer.spec.solana.model import (
    SolanaApplication,
    SolanaComponentInstance,
    SolanaProgramInstance,
)
from composer.io.multi_job import TaskInfo
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.report.collect import (
    EvidenceFetcher,
    Formalized,
    RuleEvidence,
    Verdict,
)
from composer.spec.source.munge.vfs_diff import compute_diff, fs_resolver
from composer.spec.source.report.schema import AppliedEditRecord, SourceEditRecord
from composer.spec.source.report_prover import make_prover_fetcher
from composer.spec.source.report.schema import RuleName
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)

#: Where per-unit workdirs go. A real directory rather than a temp dir, because a failed authoring
#: session's workspace is the most useful thing to look at afterwards and a temp dir is gone by the
#: time anyone asks.
#:
#: **Deliberately not under ``.certora_internal``**, which is where this started. The prover's source
#: collector skips paths inside that directory, so a unit whose whole workspace lived there uploaded
#: its ``.so`` and tuning files and *none* of its Rust — silently, with the job still succeeding.
#: Confirmed by moving one project between the two paths with nothing else changed: seven ``.rs``
#: files collected from a plain path, zero from under ``.certora_internal``.
WORK_DIR = Path(".cvlr_work")

#: Never copied into a unit's workdir. ``target`` is regenerable and enormous; ``.git`` is neither
#: needed nor ours to duplicate; :data:`WORK_DIR` is where the copies themselves go, so copying it
#: would nest one unit's workspace inside another's.
#:
#: The list is :data:`~composer.spec.cvlr.munge.NOT_PROJECT_SOURCE`, because it is the same fact
#: read twice: what a copy of the project leaves out is what a munge of the project may not touch.
#: Sharing it also keeps the two from drifting, which matters in one direction — a directory added
#: here and not there would become munge-able.
_NOT_COPIED = shutil.ignore_patterns(*NOT_PROJECT_SOURCE)


class CvlrPhase(enum.Enum):
    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    PREFLIGHT = "preflight"
    FORMALIZATION = "formalization"
    REPORT = "report"


def _munge_diff(project: Path, workdir: Path, munges: tuple[FunctionMunge, ...]) -> str:
    """A unified diff from the project's own source to the munged copy, munged files only.

    The diff itself is :mod:`composer.spec.source.munge.vfs_diff`, which is chain-neutral despite
    where it lives — it wants an "old" resolver and a "new" overlay, and this backend's "new" is
    files on disk in the unit's workdir rather than a VFS. Reusing it rather than reaching for
    ``difflib`` directly keeps one answer to "what does an edit look like in a report": the two
    were written independently first and produced byte-identical output, which is the argument.

    Reading the munged side is best-effort. A workdir that has been cleaned up loses the diff and
    says so, rather than raising — the report is the last thing in the run, and losing all of it to
    one missing file would cost every other record in it.
    """
    overlay: dict[str, str] = {}
    notes: list[str] = []
    for path in dict.fromkeys(m.path for m in munges):
        try:
            overlay[path] = (workdir / path).read_text()
        except OSError as exc:
            notes.append(f"# {path}: could not be diffed ({exc})\n")
    return compute_diff(fs_resolver(project), overlay) + "".join(notes)


def _copy_workspace(source: Path, dest: Path) -> None:
    """A unit's own copy of the workspace.

    Synchronous and called off the event loop by the caller. Re-copying an existing workdir is
    skipped rather than merged: a resumed run should find the workspace it left, including whatever
    the last session staged."""
    if dest.exists():
        _log.info("cvlr: reusing the existing workdir at %s", dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=_NOT_COPIED, symlinks=True)


@dataclasses.dataclass(frozen=True)
class CvlrDeps:
    """What every unit's authoring session needs, decided once for the run."""

    store: CvlrArtifactStore
    prover_opts: ProverOptions
    sandbox: SandboxConfig
    preflight: CvlrPreflight
    #: The verified package's directory, relative to the project root.
    package_dir: Path
    #: ``cvlr 0.6.1, cvlr-solana 0.5.0, …`` — stated in the prompt, so a reader of a transcript can
    #: tell which release the advice in it was about.
    versions: str
    crate_tools: tuple[BaseTool, ...]
    #: Where each violated rule's analysis lands, for the report to reshape into findings.
    cex_analysis: CexAnalysisStore


@dataclasses.dataclass
class CvlrFormalizer(Formalizer[GeneratedHarness, SolanaComponentInstance]):
    deps: CvlrDeps

    @override
    async def formalize(
        self,
        label: str,
        feat: SolanaComponentInstance,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[GeneratedHarness],
        run: PipelineRun,
        extra_tools: ToolBinder[SolanaComponentInstance],
    ) -> GeneratedHarness | Curtailed[GeneratedHarness] | GaveUp:
        identity = HarnessModule(feat.slug)
        project = Path(run.source.project_root)
        workdir = project / WORK_DIR / identity.module
        await asyncio.to_thread(_copy_workspace, project, workdir)

        session = CargoSession(workdir=workdir, sandbox=self.deps.sandbox)
        warmed = await session.warm()
        if isinstance(warmed, WarmFailed):
            # Not a give-up by the agent: nothing it could author would build. Reported as one so
            # the run continues with the other units and the report says why this one has nothing.
            return GaveUp(
                reason=(
                    f"could not fetch the dependency graph for {workdir} "
                    f"(exit {warmed.exit_code}):\n{warmed.diagnostics}"
                )
            )

        package_root = workdir / self.deps.package_dir
        target = HarnessTarget(
            session=session,
            module_path=package_root / SPECS_DIR / identity.artifact_file,
            package=self.deps.preflight.package,
            tuning=TuningFiles(
                envs_dir=package_root / ENVS_DIR,
                dialect=self.deps.preflight.scaffold.dialect,
            ),
        )
        verify = VerifyDeps(
            target=target,
            submission=Submission(
                manifest_path=package_root / "Cargo.toml",
                base_conf=load_base(None),
                msg=f"{self.deps.preflight.package}: {label}",
                stem=identity.stem,
                features=(DEFAULT_FEATURE,),
            ),
            prover_opts=self.deps.prover_opts,
            stamper=prover_stamper(),
            # The heavy tier: reading a counterexample back to a property is the reasoning this
            # backend most needs done well, and a bad account of one sends the author to rewrite a
            # rule that was right.
            analysis=CexAnalysis(llm=run.env.llm_heavy(), store=self.deps.cex_analysis),
        )
        return await batch_cvlr_generation(
            ctx.abstract(CvlrGeneration),
            props=props,
            component=feat,
            env=run.env,
            description=label,
            program=self.deps.preflight.package,
            module=identity.module,
            cvlr_versions=self.deps.versions,
            target=target,
            verify=verify,
            crate_tools=self.deps.crate_tools,
        )

    @override
    async def fetch_verdicts(
        self, formalized: Formalized[GeneratedHarness]
    ) -> dict[RuleName, Verdict]:
        """Verdicts for the report, read from the job the author's stamping run produced.

        Deliberately not carried out of the authoring loop. The loop's results are what the *agent*
        reasoned about; the report should state what the job says now, and where the two disagree the
        job is right.

        The shared fetcher reads nothing but ``run_link``, so one instance serves every backend with
        a prover job behind it; this one is built per call because a formalizer is cheap to make and
        holding an API client on it would outlive the run."""
        return await make_prover_fetcher()(formalized)

    @override
    async def source_edits(
        self,
        outcomes: list[ComponentOutcome[GeneratedHarness, SolanaComponentInstance]],
        run: PipelineRun,
    ) -> list[SourceEditRecord]:
        """The munges each delivered unit's verdicts were earned against.

        Reported through the shared hook rather than a CVLR-specific one, because a munge *is* a
        source edit and the report already has a vocabulary for that — ``SourceEditRecord``'s own
        docstring says its presence means "the component's outcomes are claims about the modified
        code, not the code as shipped", which is exactly the disclosure a munge owes. A second
        channel saying the same thing would be §7.6.7's mistake in a new place.

        The diff is taken against the project tree, which is the pristine copy: every unit works in
        its own copy under :data:`WORK_DIR` and nothing ever writes back. Only the munged files are
        diffed — the harness module is a new file and this unit's own deliverable, not a
        modification of the program.
        """
        records: list[SourceEditRecord] = []
        project = Path(run.source.project_root)
        for outcome in outcomes:
            # Delivered only. A curtailed partial's munges are on disk, but its verdicts are
            # unreliable by construction, and a SourceEditRecord means "these outcomes are claims
            # about the modified code" — which says nothing useful about outcomes that are not
            # claims at all.
            if not isinstance(outcome.result, Delivered):
                continue
            harness = outcome.result.result
            if not harness.munges:
                continue
            workdir = project / WORK_DIR / HarnessModule(outcome.feat.slug).module
            records.append(
                SourceEditRecord(
                    component=outcome.feat.display_name,
                    applied_edits=[
                        AppliedEditRecord(
                            edit_id=m.edit_id,
                            executive_summary=f"{m.function} in {m.path}: {m.kind.describe()}",
                            why_sound=m.why,
                        )
                        for m in harness.munges
                    ],
                    cumulative_diff=await asyncio.to_thread(
                        _munge_diff, project, workdir, tuple(harness.munges)
                    ),
                )
            )
        return records

    @override
    def findings_evidence(self) -> EvidenceFetcher | None:
        """This backend produces findings, because its prover runs analyze what they violated.

        Not unconditional in spirit: what reaches the store is filtered by
        :class:`composer.spec.cvlr.verify._CaptureCallbacks`, so a unit whose only violations were
        the prover running into its own limits contributes evidence for none of them and the report
        says so through the verdicts instead.
        """
        return self._fetch_evidence

    async def _fetch_evidence(self, rule_name: str) -> list[RuleEvidence]:
        # Every instantiation the run analyzed. CVLR's parametric form (``cvlr_rules!`` over several
        # bases) declares one rule per base under distinct names, so this is a list of one today —
        # but the shape is the shared one and narrowing it here would only have to be undone.
        return [
            RuleEvidence(label=r.label, analysis=r.analysis, counterexample=r.counterexample)
            for r in await self.deps.cex_analysis.for_rule(rule_name)
        ]


@dataclasses.dataclass
class CvlrStagedFormalizer(StagedFormalizer[GeneratedHarness, SolanaComponentInstance]):
    """Declares every unit's harness module before any unit authors one."""

    deps: CvlrDeps

    @override
    async def begin(
        self, jobs: Sequence[BackendJob[SolanaComponentInstance]], run: PipelineRun
    ) -> Formalizer[GeneratedHarness, SolanaComponentInstance]:
        modules = [HarnessModule(job.feat.slug) for job in jobs]
        mod_rs = await asyncio.to_thread(self.deps.store.declare_modules, modules)
        _log.info("cvlr: declared %d harness module(s) in %s", len(modules), mod_rs)
        return CvlrFormalizer(GeneratedHarness, "prover", self.deps)


@dataclasses.dataclass
class CvlrPrepared(
    PreparedSystem[GeneratedHarness, SolanaComponentInstance, SolanaProgramInstance]
):
    deps: CvlrDeps

    @override
    async def prepare_formalization(
        self, run: PipelineRun
    ) -> StagedFormalizer[GeneratedHarness, SolanaComponentInstance]:
        return CvlrStagedFormalizer(self.deps)


@dataclasses.dataclass
class CvlrBackend:
    """``PipelineBackend[CvlrPhase, GeneratedHarness, None, HarnessModule, SolanaComponentInstance,
    SolanaProgramInstance, SolanaApplication, CvlrPreflight]`` (P, FormT, H, A, Unit, Main, App, Pre)
    — structural."""

    artifact_store: CvlrArtifactStore
    prover_opts: ProverOptions
    sandbox: SandboxConfig
    #: Run-scoped, because rule names repeat across runs and a shared namespace would let one run's
    #: counterexamples be read as another's. Required rather than defaulted to ``None``: a run that
    #: produces no findings should be somebody's decision, not a forgotten argument.
    cex_analysis: CexAnalysisStore
    #: The package to verify. ``None`` lets preflight pick it when there is only one candidate; a
    #: workspace with several is refused rather than guessed at.
    package: str | None = None

    backend_guidance = SOLANA_CVLR_GUIDANCE
    analysis_spec = SystemAnalysisSpec("solana-analysis", "solana-properties")
    core_phases = CorePhases(
        {
            "analysis": CvlrPhase.ANALYSIS,
            "extraction": CvlrPhase.EXTRACTION,
            "formalization": CvlrPhase.FORMALIZATION,
            "report": CvlrPhase.REPORT,
        }
    )

    async def preflight(self, run: PipelineRun[CvlrPhase, None]) -> CvlrPreflight:
        """Scaffold the project, then prove it compiles with a harness in.

        Shares the driver's task group with system analysis, so a workspace that cannot be prepared
        stops the run having spent at most one partial analysis agent instead of surfacing as
        unfixable compiler errors after the whole extraction phase. The gate is a build, so it goes
        on the run's CPU budget rather than its agent budget."""
        pre = await prepare_workspace(Path(run.source.project_root), package=self.package)

        async def gate() -> None:
            await gate_workspace(pre, sandbox=self.sandbox)

        await run.cpu_runner(
            TaskInfo(
                task_id="cvlr-preflight", label="CVLR preflight", phase=CvlrPhase.PREFLIGHT
            ),
            gate,
        )
        return pre

    async def prepare_system(
        self,
        analyzed: SolanaApplication,
        run: PipelineRun[CvlrPhase, None],
        preflight: CvlrPreflight,
    ) -> PreparedSystem[GeneratedHarness, SolanaComponentInstance, SolanaProgramInstance]:
        # Imported lazily: the ecosystem registry imports the model layer, and importing it at module
        # scope would put a cycle between the backend and the ecosystem that names it.
        from composer.pipeline.ecosystem import SOLANA

        project = Path(run.source.project_root)
        crates = mount(preflight.sources)
        crate_tools = tuple(cvlr_source_tools(crates)) if crates is not None else ()
        if crates is None:
            # Not fatal, but worth a loud line: §9 lists reading the wrong CVLR as worse than
            # reading none, and reading *nothing* is the state where every helper name is a guess.
            _log.warning(
                "cvlr: no CVLR sources resolved for %s — the author will have no crate source to "
                "check against, which is the condition the hallucination risk is about",
                preflight.package,
            )
        versions = ", ".join(f"{c.name} {c.version}" for c in preflight.sources.crates)
        for gap in preflight.gaps:
            _log.info("cvlr: %s", gap.describe())

        deps = CvlrDeps(
            store=self.artifact_store,
            prover_opts=self.prover_opts,
            sandbox=self.sandbox,
            preflight=preflight,
            package_dir=preflight.package_dir,
            versions=versions,
            crate_tools=crate_tools,
            cex_analysis=self.cex_analysis,
        )
        return CvlrPrepared(SOLANA.locate_main(analyzed, run.source), deps)

    def to_artifact_id(self, c: SolanaComponentInstance) -> HarnessModule:
        return HarnessModule(c.slug)
