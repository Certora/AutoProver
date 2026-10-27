"""The CVLR backend: the pipeline's Solana-with-the-Prover entry point.

``docs/cvlr-backend-plan.md`` §7.5. Structurally this is ``NullSolanaBackend`` with a real formalizer:
the front half — the Solana model, the analysis and property prompts, ``locate_main`` — is the
ecosystem's and is reused verbatim, which is the whole reason §2 could say the front half was already
built. What this module adds is the four things that differ: a preflight that scaffolds and gates
the workspace, a staged formalizer that declares the harness modules, the per-unit authoring loop,
and verdicts read back from the prover.

**Why the formalizer is staged.** ``specs/mod.rs`` needs a ``mod`` line per unit and the package
manifest needs a feature per unit, and no single unit knows what the others are — so both have to be
written once, by one writer, when every unit is known, which is exactly what
:class:`~composer.pipeline.core.StagedFormalizer` exists for. §5.6 predicted this shape ("one shared
certora harness module, many rules") before there was any code to hang it on. Getting the manifest
half wrong is the failure ``docs/command-sandbox.md`` §11 item 8 records on the Rust wheel path: a
``--features`` a shared crate does not declare fails with "Package does not contain this feature",
and the unit it belongs to is dropped.

**Why every unit shares one workdir.** ``docs/who-edits-the-program.md`` §14 is the argument; this is the
summary. §7.5.2 answered open question 3 with "per unit, and forced", on the grounds that the compile
gate is whole-crate — unit A's gate would fail whenever unit B's draft was momentarily broken. That
is true of a shared tree and false of this one, because each unit's module is declared behind its own
cargo feature: a module behind a disabled ``cfg`` is never compiled, never enters rustc's dep-info,
and so can neither break nor dirty a sibling's build. What that buys is the whole cost of the old
arrangement — the dependency graph is fetched once and compiled once instead of once per unit, since
an empty unit feature leaves every dependency's resolved feature set identical and only the program
crate's own artifacts vary.

Two things follow. Builds are serialized behind one permit, because every unit shares one
``target/`` — cargo would serialize them anyway on its own build-directory lock, and an explicit
permit is a queue the host can see. And the tree is **derived** rather than state: it is the pristine
project plus each unit's draft plus the staging unit's edits to the program plus the tuning files,
all of which are in the checkpoint, so it can be deleted and rebuilt and a resumed or cache-replayed
run still produces the same submission (:mod:`composer.spec.cvlr.tree`).
"""

import asyncio
import dataclasses
import enum
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Sequence, override


from composer.cargo.sbf import sbf_check
from composer.cargo.session import CargoSession, WarmFailed
from composer.pipeline.core import (
    ComponentOutcome,
    CorePhases,
    Formalizer,
    PipelineRun,
    PreparedSystem,
    SharedUnit,
    StagedFormalizer,
    SystemAnalysisSpec,
    ToolBinder,
)
from composer.pipeline.ptypes import BackendJob, Curtailed, Delivered, GaveUp
from composer.prover.conf import Conf, InheritRules, SelectRules
from composer.prover.core import ProverOptions
from composer.sandbox.config import SandboxConfig
from composer.spec.context import CvlrGeneration, WorkflowContext
from composer.spec.cvlr.author import batch_cvlr_generation
from composer.spec.cvlr.editor import EditingServices, ProgramEditing, program_editing
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    HOST_RUST_TOOLCHAIN,
    PLATFORM_TOOLS_VERSION,
    delivered_conf,
)
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE
from composer.spec.cvlr.harness import CvlrArtifactStore, GeneratedHarness, HarnessModule
from composer.spec.cvlr.preflight import CvlrPreflight, gate_workspace, prepare_workspace
from composer.spec.cvlr.patch import snapshot, write_component_patches, write_patch
from composer.spec.cvlr.prover import Submission
from composer.spec.cvlr.scaffold import ENVS_DIR, SPECS_DIR, declare_unit_features
from composer.spec.cvlr.tree import SharedTree
from composer.spec.cvlr.tuning import SUMMARIES, TuningFiles
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY
from composer.spec.cvlr.verify import (
    CexAnalysis,
    HarnessTarget,
    VerifyDeps,
    prover_stamper,
)
from composer.spec.cvlr.reference import ChainReference
from composer.spec.solana.model import (
    SolanaApplication,
    SolanaComponentInstance,
    SolanaProgramInstance,
    protocol_unit,
)
from composer.io.multi_job import TaskInfo
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.report.collect import (
    EvidenceFetcher,
    Formalized,
    RuleEvidence,
    Verdict,
)
from composer.spec.source.report.schema import (
    AppliedEditRecord, BuildEnvironment, ConfinedBuilds, SourceEditRecord, UnconfinedBuilds,
)
from composer.spec.source.munge.vfs_diff import diff_against_baseline
from composer.spec.source.report_prover import make_run_link_fetcher
from composer.spec.source.report.schema import RuleName
from composer.spec.types import ComponentName, PropertyFormulation

_log = logging.getLogger(__name__)

#: The one tree, in the run's work directory (:func:`run_work_directory`). A subdirectory rather than
#: the work directory itself so that the build output, the sandbox's private ``CARGO_HOME`` and
#: anything a later version keeps beside them are visibly separate things in one place.
BUILD_DIR = "build"

#: Each submission's build script, its command file and its conf, beside :data:`BUILD_DIR` rather
#: than in it: the Prover runs the script unconfined, so nothing the confined build can write may
#: hold it (:func:`composer.cargo.sbf.write_build_script`).
SUBMISSIONS_DIR = "submissions"


@asynccontextmanager
async def run_work_directory() -> AsyncIterator[Path]:
    """The run's work directory, outside the project and deleted when the run ends.

    Everything in it is derived from the project and the run's checkpointed state, so a resumed run
    rebuilds it rather than needing it kept. Never under ``.certora_internal``: the Prover's source
    collector skips paths inside that directory, so a submission built there uploads its ``.so``
    and none of its Rust, and the job still succeeds.
    """
    work = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="cvlr_work_"))
    try:
        yield work
    finally:
        await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)


class CvlrPhase(enum.Enum):
    DISCOVER_DESIGN_DOC = "discover_design_doc"
    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    PREFLIGHT = "preflight"
    FORMALIZATION = "formalization"
    REPORT = "report"


@dataclasses.dataclass(frozen=True)
class CvlrDeps:
    """What every unit's authoring session needs, decided once for the run."""

    store: CvlrArtifactStore
    prover_opts: ProverOptions
    sandbox: SandboxConfig
    preflight: CvlrPreflight
    #: The verified package's directory, relative to the project root.
    package_dir: Path
    #: ``cvlr 0.6.2, cvlr-solana 0.5.1, …`` — stated in the prompt, so a reader of a transcript can
    #: tell which release the advice in it was about.
    versions: str
    #: What the manual gets wrong for this run's generation
    #: (:attr:`~composer.spec.cvlr.reference.ChainReference.caveats`).
    caveats: tuple[str, ...]
    #: Where each violated rule's analysis lands, for the report to reshape into findings.
    cex_analysis: CexAnalysisStore
    #: The source tools every unit's agents read the program through, and the store their edits
    #: to it are committed to.
    editing: ProgramEditing
    #: Where the run's tree and submissions go (:func:`run_work_directory`).
    work_dir: Path


@dataclasses.dataclass(frozen=True)
class SharedBuild:
    """The one working tree, the one cargo session over it, and the permit that serializes them.

    Made once per run in :meth:`CvlrStagedFormalizer.begin`, because every part of it is run-constant
    and because the copy and the dependency fetch are the two costs the shared tree exists to pay
    once instead of once per unit.

    ``warm_failure`` is carried rather than raised. A failed dependency fetch means nothing any unit
    could author would build, and the run should still produce a report saying so for each of them —
    which is what the per-unit ``GaveUp`` did when each unit warmed its own workspace.
    """

    tree: SharedTree
    session: CargoSession
    #: One permit for the whole run. Held across staging, the local cargo invocation and the
    #: Prover's rebuild and upload, and not across the cloud job — see
    #: :meth:`composer.spec.cvlr.verify.HarnessTarget.build_slot`.
    build_sem: asyncio.Semaphore
    #: Where submissions are written; see :data:`SUBMISSIONS_DIR`.
    submissions: Path
    warm_failure: str | None = None


type _Outcome = ComponentOutcome[GeneratedHarness, SolanaComponentInstance]


def _delivered(outcome: _Outcome) -> GeneratedHarness | None:
    match outcome.result:
        case Delivered(result=harness):
            return harness
        case _:
            return None


def _partial(outcome: _Outcome) -> GeneratedHarness | None:
    match outcome.result:
        case Curtailed(partial=Delivered(result=harness)):
            return harness
        case _:
            return None


def _component_overlays(outcomes: list[_Outcome]) -> dict[str, dict[str, str]]:
    """Harness-module name to the edited files of each unit that edited the program.

    A curtailed partial is included: its verdicts are not claims, but without its edits the draft
    the run kept cannot be rebuilt.
    """
    overlays: dict[str, dict[str, str]] = {}
    for outcome in outcomes:
        harness = _delivered(outcome) or _partial(outcome)
        if harness is not None and harness.applied_edits:
            overlays[HarnessModule(outcome.feat.slug).module] = harness.vfs
    return overlays


@dataclasses.dataclass
class CvlrFormalizer(Formalizer[GeneratedHarness, SolanaComponentInstance]):
    deps: CvlrDeps
    build: SharedBuild
    #: What :meth:`finalize` wrote, for the report to name.
    patch: Path | None = dataclasses.field(default=None, init=False)
    #: Harness-module name to the patch :meth:`finalize` wrote for that unit, relative to the
    #: project. Empty until then, and empty when no unit edited the program.
    component_patches: dict[str, Path] = dataclasses.field(default_factory=dict, init=False)
    #: Component to the conf :meth:`finalize` wrote for it, relative to the project.
    confs: dict[ComponentName, Path] = dataclasses.field(default_factory=dict, init=False)

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
        if self.build.warm_failure is not None:
            # Not a give-up by the agent: nothing it could author would build. Reported as one so
            # the run continues with the other units and the report says why each has nothing.
            return GaveUp(reason=self.build.warm_failure)

        session = self.build.session
        package_root = session.workdir / self.deps.package_dir
        tuning = TuningFiles(
            envs_dir=package_root / ENVS_DIR,
            dialect=self.deps.preflight.scaffold.dialect,
            unit=identity.module,
        )
        # The composite this unit's conf names has to exist before the first submission names it,
        # and the loop may summarize nothing at all. Composing it empty now costs one file and
        # removes a case where the prover refuses a conf for a path that was never written.
        await asyncio.to_thread(tuning.write, ())
        target = HarnessTarget(
            session=session,
            module_path=package_root / SPECS_DIR / identity.artifact_file,
            package=self.deps.preflight.package,
            package_root=package_root,
            tuning=tuning,
            unit=identity,
            tree=self.build.tree,
            build_sem=self.build.build_sem,
        )
        verify = VerifyDeps(
            target=target,
            submission=Submission(
                manifest_path=package_root / "Cargo.toml",
                msg=f"{self.deps.preflight.package}: {label}",
                stem=identity.stem,
                # The harness feature and this unit's own: what compiles exactly one unit's rules
                # out of the shared crate (docs/who-edits-the-program.md §14.1).
                features=(DEFAULT_FEATURE, identity.feature),
                # Workdir-relative, which is how certoraSolanaProver reads a conf path.
                summaries=(
                    self.deps.package_dir / ENVS_DIR / SUMMARIES.unit_composite(identity.module),
                ),
            ),
            submissions=self.build.submissions,
            prover_opts=self.deps.prover_opts,
            stamper=prover_stamper(),
            analysis=CexAnalysis(store=self.deps.cex_analysis),
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
            caveats=self.deps.caveats,
            target=target,
            verify=verify,
            editing=self.deps.editing,
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
        return await make_run_link_fetcher()(formalized)

    @override
    async def source_edits(
        self,
        outcomes: list[ComponentOutcome[GeneratedHarness, SolanaComponentInstance]],
        run: PipelineRun,
    ) -> list[SourceEditRecord]:
        """The edits each delivered unit's verdicts were earned against, as one record per unit.

        Reported through the shared hook rather than a CVLR-specific one, because an edit to the
        program *is* a source edit and the report already has a vocabulary for that —
        ``SourceEditRecord``'s own docstring says its presence means "the component's outcomes are
        claims about the modified code, not the code as shipped". The same shape EVM reports.

        Each unit's diff is its own overlay against the project as the run left it, harness already
        in place, and it survives the working tree being deleted. Two units' overlays are not
        merged. The harness module is never in an overlay — it is a new file and this unit's own
        deliverable, not a modification of the program. The patch that applies to the project as
        shipped is the one :meth:`finalize` wrote for the unit, named here by ``patch_path``.
        """
        # Delivered only. A curtailed partial's verdicts are unreliable by construction, and a
        # SourceEditRecord here means "these outcomes are claims about the modified code" — which
        # says nothing useful about outcomes that are not claims at all.
        return await self._edit_records(outcomes, _delivered, Path(run.source.project_root))

    @override
    async def curtailed_source_edits(
        self, outcomes: list[_Outcome], run: PipelineRun
    ) -> list[SourceEditRecord]:
        """The same records for the partials the budget cut short, so the kept draft can be rebuilt
        from the deliverables."""
        return await self._edit_records(outcomes, _partial, Path(run.source.project_root))

    async def _edit_records(
        self,
        outcomes: list[_Outcome],
        harness_of: Callable[[_Outcome], GeneratedHarness | None],
        project: Path,
    ) -> list[SourceEditRecord]:
        records: list[SourceEditRecord] = []
        for outcome in outcomes:
            harness = harness_of(outcome)
            if harness is None or not harness.applied_edits:
                continue
            stem = HarnessModule(outcome.feat.slug).module
            patch = self.component_patches.get(stem)
            records.append(
                SourceEditRecord(
                    component=outcome.feat.display_name,
                    applied_edits=[
                        AppliedEditRecord(**e.model_dump()) for e in harness.applied_edits
                    ],
                    cumulative_diff=await asyncio.to_thread(
                        diff_against_baseline, harness.vfs, project
                    ),
                    patch_path=None if patch is None else patch.as_posix(),
                )
            )
        return records

    @override
    async def finalize(
        self, outcomes: list[ComponentOutcome[GeneratedHarness, SolanaComponentInstance]],
        run: PipelineRun,
    ) -> None:
        root = Path(run.source.project_root)
        await asyncio.to_thread(self._write_confs, outcomes, root)
        self.patch = await asyncio.to_thread(write_patch, root)
        self.component_patches = await asyncio.to_thread(
            write_component_patches, root, _component_overlays(outcomes)
        )

    def _write_confs(self, outcomes: list[_Outcome], root: Path) -> None:
        """Write each delivered unit's conf, and the summaries file it names into the project.

        The summaries file is otherwise only in the run's working tree. Written before the patches
        are diffed, so both patches carry it.
        """
        confs: dict[HarnessModule, Conf] = {}
        names: dict[HarnessModule, ComponentName] = {}
        for outcome in outcomes:
            harness = _delivered(outcome)
            if harness is None or harness.settings is None:
                continue
            unit = HarnessModule(outcome.feat.slug)
            TuningFiles(
                envs_dir=root / self.deps.package_dir / ENVS_DIR,
                dialect=self.deps.preflight.scaffold.dialect,
                unit=unit.module,
            ).write(tuple(harness.summaries))
            confs[unit] = delivered_conf(
                harness.settings,
                features=(DEFAULT_FEATURE, unit.feature),
                summaries=(ENVS_DIR / SUMMARIES.unit_composite(unit.module),),
                rules=(
                    SelectRules(tuple(harness.declared_rules))
                    if harness.declared_rules
                    else InheritRules()
                ),
                msg=f"{self.deps.preflight.package}: {outcome.feat.display_name}",
            )
            names[unit] = outcome.feat.display_name
        written = self.deps.store.write_confs(confs)
        self.confs = {names[unit]: path for unit, path in written.items()}

    @override
    def deliverable_patch(self) -> str | None:
        return self.patch.as_posix() if self.patch is not None else None

    @override
    def deliverable_confs(self) -> dict[ComponentName, str]:
        return {name: path.as_posix() for name, path in self.confs.items()}

    @override
    def build_environment(self) -> BuildEnvironment:
        """Every verdict in this report came out of a cargo build, so the report says how those ran.

        Answered, never omitted: the ``None`` the base returns means "this backend compiles nothing",
        and a reader who cannot tell that from "nobody recorded it" has no use for the field at all.
        """
        sandbox = self.deps.sandbox
        return (
            ConfinedBuilds(provider=sandbox.provider)
            if sandbox.enabled else UnconfinedBuilds()
        )

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
        """Declare every unit, then make the one tree they share.

        The order is the whole of it. Both declarations — the ``cfg``-gated ``mod`` lines and the
        cargo features they name — are written into the *project*, once, by this one writer, before
        the tree is copied from it. A feature declared later than the tree would not be in the tree;
        a feature not declared at all makes ``--features unit_x`` fail with "Package does not
        contain this feature", which is the failure ``docs/command-sandbox.md`` §11 item 8 records
        the Rust wheel hitting on a shared crate.
        """
        project = Path(run.source.project_root)
        modules = [HarnessModule(job.feat.slug) for job in jobs]
        manifest = self.deps.package_dir / "Cargo.toml"
        declared = await asyncio.to_thread(self.deps.store.declare_modules, modules)
        added = await asyncio.to_thread(
            declare_unit_features, project / manifest, [m.feature for m in modules]
        )
        _log.info(
            "cvlr: declared %d harness module(s) in %s; added %d cargo feature(s)",
            len(modules),
            declared[0],
            len(added),
        )

        tree = SharedTree(
            pristine=project,
            root=self.deps.work_dir / BUILD_DIR,
            source=self.deps.preflight.source,
        )
        await tree.materialize()
        session = CargoSession(
            workdir=tree.root,
            sandbox=self.deps.sandbox,
            toolchain=HOST_RUST_TOOLCHAIN,
            check_with=sbf_check(PLATFORM_TOOLS_VERSION),
        )
        submissions = self.deps.work_dir / SUBMISSIONS_DIR
        submissions.mkdir(parents=True, exist_ok=True)
        warmed = await session.warm()
        failure = (
            f"could not fetch the dependency graph for {tree.root} "
            f"(exit {warmed.exit_code}):\n{warmed.diagnostics}"
            if isinstance(warmed, WarmFailed)
            else None
        )
        build = SharedBuild(
            tree=tree,
            session=session,
            # One permit for the run. Cargo would serialize concurrent builds against this
            # `target/` on its own lock anyway; the permit is the queue the host can see.
            build_sem=asyncio.Semaphore(1),
            submissions=submissions,
            warm_failure=failure,
        )
        return CvlrFormalizer(GeneratedHarness, "prover", self.deps, build)


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
    #: The CVLR releases this run scaffolds the target with. Beside :attr:`package` because it is
    #: the same kind of decision: what this run is pointed at. Narrowed by
    #: :meth:`~composer.spec.cvlr.reference.ChainReference.withholding` when the target is itself
    #: the program one of the :class:`~composer.spec.cvlr.reference.ProgramModel` crates models,
    #: which is the only narrowing there is. One per platform generation; there is no default,
    #: because the right one depends on the target
    #: (:func:`~composer.spec.cvlr.scaffold.select_generation`).
    reference: ChainReference
    #: The run's services for editing the program, finished in :meth:`prepare_system` once
    #: preflight has said which crates are the project's own.
    editing: EditingServices
    #: Where the run's tree and submissions go (:func:`run_work_directory`).
    work_dir: Path
    #: The package to verify. ``None`` lets preflight pick it when there is only one candidate; a
    #: workspace with several is refused rather than guessed at.
    package: str | None = None

    backend_guidance = SOLANA_CVLR_GUIDANCE
    #: A CVLR rule drives the handlers it names; nothing checks a property against every handler
    #: by itself, as a CVL invariant is. Properties several components state go to one unit.
    shared_unit = SharedUnit(
        make=protocol_unit,
        entry_points=lambda unit: [i.name for i in unit.instructions],
    )
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
        await asyncio.to_thread(snapshot, Path(run.source.project_root))
        pre = await prepare_workspace(
            Path(run.source.project_root),
            package=self.package,
            reference=self.reference,
            sandbox=self.sandbox,
        )

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
            caveats=self.reference.caveats,
            cex_analysis=self.cex_analysis,
            editing=program_editing(
                self.editing,
                run.env,
                run.source,
                preflight.source,
                preflight.package_dir,
                SOLANA,
            ),
            work_dir=self.work_dir,
        )
        return CvlrPrepared(SOLANA.locate_main(analyzed, run.source), deps)

    def to_artifact_id(self, c: SolanaComponentInstance) -> HarnessModule:
        return HarnessModule(c.slug)
