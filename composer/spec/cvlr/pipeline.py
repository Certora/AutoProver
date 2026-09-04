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

**Why every unit shares one workdir.** ``docs/single-working-tree.md`` is the argument; this is the
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
project plus each unit's draft plus the union of the munges plus the tuning files, all of which are
in the checkpoint, so it can be deleted and rebuilt and a resumed or cache-replayed run still
produces the same submission (:mod:`composer.spec.cvlr.tree`).
"""

import asyncio
import dataclasses
import enum
import logging
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
from composer.spec.cvlr.preflight import CvlrPreflight, gate_workspace, prepare_workspace
from composer.spec.cvlr.prover import Submission
from composer.spec.cvlr.scaffold import ENVS_DIR, SPECS_DIR, SUMMARIES, declare_unit_features
from composer.spec.cvlr.tree import SharedTree, munge_diff
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
from composer.spec.source.report.schema import AppliedEditRecord, SourceEditRecord
from composer.spec.source.report_prover import make_prover_fetcher
from composer.spec.source.report.schema import RuleName
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)

#: Where the run's working copy goes. A real directory rather than a temp dir, because a failed
#: authoring session's workspace is the most useful thing to look at afterwards and a temp dir is
#: gone by the time anyone asks.
#:
#: **Deliberately not under ``.certora_internal``**, which is where this started. The prover's source
#: collector skips paths inside that directory, so a run whose whole workspace lived there uploaded
#: its ``.so`` and tuning files and *none* of its Rust — silently, with the job still succeeding.
#: Confirmed by moving one project between the two paths with nothing else changed: seven ``.rs``
#: files collected from a plain path, zero from under ``.certora_internal``.
WORK_DIR = Path(".cvlr_work")

#: The one tree, under :data:`WORK_DIR`. Named rather than being :data:`WORK_DIR` itself so that the
#: build output, the sandbox's private ``CARGO_HOME`` and anything a later version keeps beside them
#: are visibly separate things in one place.
BUILD_DIR = "build"


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
    #: ``cvlr 0.6.1, cvlr-solana 0.5.0, …`` — stated in the prompt, so a reader of a transcript can
    #: tell which release the advice in it was about.
    versions: str
    crate_tools: tuple[BaseTool, ...]
    #: Where each violated rule's analysis lands, for the report to reshape into findings.
    cex_analysis: CexAnalysisStore


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
    #: One permit for the whole run. Held across staging and the local cargo invocation, and not
    #: across a prover run — see :meth:`composer.spec.cvlr.verify.HarnessTarget.build_slot`.
    build_sem: asyncio.Semaphore
    warm_failure: str | None = None


@dataclasses.dataclass
class CvlrFormalizer(Formalizer[GeneratedHarness, SolanaComponentInstance]):
    deps: CvlrDeps
    build: SharedBuild

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
            tuning=tuning,
            unit=identity,
            tree=self.build.tree,
            build_sem=self.build.build_sem,
        )
        verify = VerifyDeps(
            target=target,
            submission=Submission(
                manifest_path=package_root / "Cargo.toml",
                base_conf=load_base(None),
                msg=f"{self.deps.preflight.package}: {label}",
                stem=identity.stem,
                # The harness feature and this unit's own: what compiles exactly one unit's rules
                # out of the shared crate (docs/single-working-tree.md §2.1).
                features=(DEFAULT_FEATURE, identity.feature),
                # Workdir-relative, which is how certoraSolanaProver reads a conf path.
                summaries=(
                    str(
                        self.deps.package_dir
                        / ENVS_DIR
                        / SUMMARIES.unit_composite(identity.module)
                    ),
                ),
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
            pristine=self.build.tree.pristine,
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

        The diff is computed from the munge records rather than read off the working tree
        (:func:`composer.spec.cvlr.tree.munge_diff`). Two reasons, and both are consequences of the
        tree being shared: the tree holds every unit's munges, so reading it would show one unit's
        report its siblings' dormant lines; and a diff derived from state survives the tree being
        deleted, which ``docs/single-working-tree.md`` §4 makes a routine thing to do. Only the
        munged files are diffed — the harness module is a new file and this unit's own deliverable,
        not a modification of the program.
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
                        munge_diff, project, tuple(harness.munges)
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

        tree = SharedTree(pristine=project, root=project / WORK_DIR / BUILD_DIR)
        await asyncio.to_thread(tree.materialize)
        # A reused tree predates the two declarations above, so a resumed run whose component set
        # changed would build against a manifest missing a unit's feature and a `mod.rs` missing its
        # module. Content-compared, so an unchanged set costs nothing.
        adopted = await asyncio.to_thread(
            tree.adopt, manifest, *(p.relative_to(project) for p in declared)
        )
        if adopted:
            _log.info("cvlr: re-synced %s into the working tree", ", ".join(adopted))
        session = CargoSession(workdir=tree.root, sandbox=self.deps.sandbox)
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
