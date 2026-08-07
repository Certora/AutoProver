"""Adapter: wrap a Rust wheel (a :class:`~autoprover_sdk.Backend`) as a
:class:`~composer.pipeline.core.PipelineBackend`.

The Rust wheel is a **passive service** (``docs/rust-applications.md``): Python owns every LLM turn
and calls the wheel's pure callouts (``descriptor`` / ``checks`` / ``author_prompt`` /
``check_syntax`` / ``judge`` / ``finalize``) plus the two blocking ones (``compile`` /
``validate``) that run the toolchain via ``run-confined``. There is no IoC ``resume`` loop and no
``Effects`` protocol.

Authoring itself is the shared session of :mod:`composer.authoring`, assembled for a wheel in
:mod:`composer.rustapp.session`: the agent owns a spec buffer, the wheel's ``validate`` is a tool it
calls, and publishing is gated on stamps over the buffer as it stands. This module is what connects
that session to the pipeline — phases, preflight, the setup spec's cache, and the report.

Three phase objects mirror the CVL / foundry backends:

* :class:`RustBackend`        — ``PipelineBackend`` (guidance, phases, store, ``preflight`` /
  ``prepare_system``).
* :class:`RustPreparedSystem` — builds the formalizer (thin; no app-specific setup).
* :class:`RustFormalizer`     — ``formalize`` runs the loop; ``fetch_verdicts`` reads the verdicts
  ``validate`` baked into the result.

App-specific orchestration (a shared setup spec, workspace prep + its gate, crate assembly) is
descriptor-driven here — no per-application Python package (``docs/rust-applications.md``): the wheel
declares ``preflight`` / ``setup`` / ``workspace_prep`` / ``deliverable_mode=callout`` / ``finalize``
and the generic host runs them.

The two build-shaped steps are overlapped with the LLM steps that don't need them:
:meth:`RustBackend.preflight` (prepare the workspace, then *gate* it — a wheel-authored skeleton
built by the real toolchain) runs alongside system analysis, and the shared setup spec is
authored after extraction, when the properties it must make checkable finally exist.
"""

import asyncio
import enum
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, override


from langgraph.config import get_stream_writer

from composer.io.multi_job import TaskInfo
from composer.pipeline.core import (
    BackendJob,
    ComponentOutcome,
    CorePhases,
    Delivered,
    Formalizer,
    GaveUp,
    PipelineBackend,
    PipelineRun,
    PreparedSystem,
    StagedFormalizer,
    SystemAnalysisSpec,
)
from composer.pipeline.ecosystem import ChainTag, Ecosystem
from composer.sandbox.command import DEFAULT_TIMEOUT_S
from composer.sandbox.config import BackendSpec, SandboxConfig
from composer.rustapp.descriptor import AppDescriptor, PhaseRole, PhaseSpec
from composer.rustapp.result import RustArtifact, RustFormalResult, RustSetupSpec
from composer.rustapp.toolchain import project_toolchain, source_unit
from composer.rustapp.session import (
    live_checks,
    run_session,
    targets_of,
)
from composer.rustapp.wire import (
    AuthorInput,
    CompileOk,
    ComponentGaveUp,
    ComponentInput,
    FinalizeComponent,
    FinalizeInput,
    PreflightInput,
    Property,
    RustAppModule,
    SetupInput,
    parse_compile,
    parse_files,
    parse_checks,
    parse_workspace_prep,
)
# The wheel's per-check verdict and the *report's* per-check verdict are different types with the same
# name (``fetch_verdicts`` maps one to the other), so the wire one is aliased here. Likewise
# ``Delivered``: the pipeline's component outcome and the wire's payload for one.
from composer.rustapp.wire import Delivered as WireDelivered
from composer.rustapp.wire import SkippedProperty as WireSkipped
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import CacheKey, SourceFields, WorkflowContext
from composer.spec.source.report.collect import ReportComponentInput, Verdict
from composer.spec.source.report.schema import RuleName
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import ComponentName, PropertyFormulation
from composer.spec.util import slugify_filename, string_hash

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared loop helpers (used by RustFormalizer.formalize and app setup specs).
# ---------------------------------------------------------------------------

def emit_event(kind: str, payload: dict) -> None:
    """Stream a domain event to the current task's panel, as the wheel's ``{"type": kind, …}``
    custom-stream payload.

    Callable only from inside a graph run — every emission site is a gate tool's body, where
    LangGraph's stream writer routes the event with the run's own thread and checkpoint."""
    get_stream_writer()({"type": kind, **payload})


@dataclass(frozen=True)
class UnitProperty:
    """A property and the unit whose analysis produced it — the two halves of what identifies a
    property across a run (the report's ``PropertyKey``).

    Titles are unique only within a unit, so wherever properties from more than one unit meet — the
    shared setup spec's input — they travel paired with their unit rather than as bare
    formulations."""

    component: ComponentName
    prop: PropertyFormulation


def unique_slugs(props: list[PropertyFormulation]) -> list[str]:
    """One unique kebab slug per property — what a wheel names that property's check after. A
    collision gets a numeric suffix, whether it comes from punctuation/casing or from two units
    genuinely sharing a title (the setup spec's input spans units, where that is allowed).

    Not the artifact's name: a deliverable file is named from the *component's* slug
    (:meth:`RustBackend.to_artifact_id`), which is a different thing entirely."""
    slugs: list[str] = []
    seen: dict[str, int] = {}
    for p in props:
        base = slugify_filename(p.title) or "inv"
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.append(base if n == 0 else f"{base}_{n}")
    return slugs


def _properties(owned: Sequence[UnitProperty]) -> list[Property]:
    """The wire form of the properties one spec must make checkable — each naming the unit it was
    inferred for, and the host-assigned slug a wheel names its check after (see
    :func:`unique_slugs`)."""
    return [
        Property(
            component=o.component, title=o.prop.title, sort=o.prop.sort,
            description=o.prop.description, slug=slug,
        )
        for o, slug in zip(owned, unique_slugs([o.prop for o in owned]))
    ]


def confined_target(root: Path, rel: str) -> Path:
    """Join a wheel-supplied relative path under ``root``, rejecting absolute paths / ``..``
    traversal — mirrors the Rust ``confined_join`` so host-written deliverable/prep files stay
    inside the project (the wheel is trusted, but defense-in-depth is cheap).

    Public because the toolchain half of a workspace prep lives outside this module
    (:class:`~composer.rustapp.toolchain.ProjectToolchain`) and whatever it writes must be confined
    exactly as the host's own writes are."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe file path {rel!r}: absolute or traverses outside the workdir")
    return root / p


def source_unit_of(
    ecosystem: Ecosystem[Any, Any, Any], source: SourceFields
) -> dict[str, Any]:
    """The ``AuthorInput.source_unit`` field — where the code under analysis lives as a unit of its
    own build system — from the chain's registered toolchain
    (:func:`composer.rustapp.toolchain.source_unit`).

    A wheel that must *depend on* the analyzed code (Crucible's harness path-depends on the program
    under test) reads this instead of deriving a directory or package name from
    ``source.contract_name``, which is only the analysis identifier. Empty when the chain has no
    toolchain, when the language has no such unit (Solidity), or when the layout couldn't be read —
    all three mean the same thing to the wheel, which then applies its own convention.
    """
    return source_unit(ecosystem.name, source)


def _setup_identity(input: SetupInput) -> str:
    """A cache key for the shared setup spec: a hash of what it is authored *from*.

    Exactly the inputs the wheel renders the artifact from — the program, the project facts (where its
    code lives, and what the prep established, which is what decides where its types come from), the
    analyzed model, and the properties it has to make checkable. Deliberately NOT the whole input:
    ``args`` also carries run knobs (a fuzz budget) that don't change what gets authored, and keying
    on those would throw the artifact away for no reason.
    """
    material = {
        "program": input.program,
        "source_unit": input.source_unit,
        "prep_facts": input.prep_facts,
        "model": input.model,
        "props": [p.model_dump() for p in input.props],
    }
    return string_hash(json.dumps(material, sort_keys=True, default=str))


async def run_workspace_prep(
    module: RustAppModule,
    input: AuthorInput,
    *,
    chain: ChainTag,
    source: SourceFields,
    sandbox: SandboxConfig | None,
    command_timeout_s: int,
) -> dict[str, Any]:
    """Execute the wheel's pure ``workspace_prep`` plan (``docs/rust-applications.md`` §7): write the
    declared files (path-confined) under ``source.project_root``, then hand the plan's
    ``toolchain_request`` to ``chain``'s registered
    :class:`~composer.rustapp.toolchain.ProjectToolchain`. Returns what the prep established, which
    the caller reports back to the wheel as ``AuthorInput.prep_facts`` — empty when the plan only
    placed files.

    The split is the seam: writing files is the same in every ecosystem, while preparing a *project*
    means driving a build system the host does not understand, which only something that knows the
    chain can do (see the toolchain module for why the framework carries no implementation). Either
    way the wheel supplies only file contents + a request its chain's toolchain understands, never a
    command line, so the network posture stays Python-owned.

    The whole ``source`` goes through rather than just its root: an implementation resolves its own
    project facts from it (Solana reads the crate that owns ``relative_path`` to fill in an IDL's
    program id), which is knowledge the framework would otherwise have to hold a shape for."""
    workdir = Path(source.project_root)
    plan = parse_workspace_prep(module.workspace_prep(input.model_dump_json()))
    for rel, contents in plan.files.items():
        target = confined_target(workdir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    if not plan.needs_toolchain:
        return {}
    facts = await project_toolchain(chain).prepare(
        plan, input, source=source, sandbox=sandbox, timeout_s=command_timeout_s
    )
    if facts:
        _log.info("workspace prep established %s", facts)
    return facts


class PreflightFailed(RuntimeError):
    """The prepared workspace does not build, or its skeleton artifact does not run — established
    before any property or authored artifact exists.

    Terminal by construction: what fails here is the *workspace* (a dependency graph that won't
    resolve, a unit that won't link, codegen the generator rejects, a built program that won't load),
    and none of that is something an authoring agent can fix — it doesn't own the project's build
    files. Re-authoring against it only burns the revise budget on errors the model can't address,
    which is exactly what this gate exists to prevent."""


async def run_preflight_gate(
    module: RustAppModule,
    input: AuthorInput,
    *,
    workdir: Path,
    sandbox_dict: BackendSpec,
) -> None:
    """Gate the prepared workspace with a ``kind="preflight"`` ``compile`` — the wheel's own
    skeleton artifact, built by the real toolchain under the real sandbox.

    There is no ``spec`` at all — ``None``, not an empty one: nothing has been authored yet (this
    runs alongside system analysis), so the wheel renders the smallest artifact that still exercises
    what an authored one will depend on, and a wheel whose toolchain would take an empty spec file
    for a real one can tell the two apart. Raises :class:`PreflightFailed` with the compiler
    diagnostics the wheel extracted; there is no retry.

    Nothing is streamed as it goes: this is not a graph run, so there is no stream writer to emit
    on, and the one thing worth showing — the diagnostics — is what the exception carries."""
    result = parse_compile(
        await asyncio.to_thread(
            module.compile, input.model_dump_json(), None, str(workdir), json.dumps(sandbox_dict)
        )
    )
    if isinstance(result, CompileOk):
        return
    raise PreflightFailed(
        "the prepared workspace does not build (or its skeleton does not run), before anything has "
        "been authored — a toolchain, dependency or program-build problem, not something the run "
        f"can author its way around:\n{result.errors}"
    )


# ---------------------------------------------------------------------------
# The formalizer.
# ---------------------------------------------------------------------------

class RustFormalizer(Formalizer[RustFormalResult, FeatureUnit]):
    """Drives a Rust :class:`~autoprover_sdk.Backend` through one authoring session per unit.
    Ecosystem-agnostic: the unit is any :class:`FeatureUnit`, marshalled via ``feature_json()``."""

    def __init__(
        self,
        module: RustAppModule,
        descriptor: AppDescriptor,
        *,
        sandbox: SandboxConfig | None = None,
        command_timeout_s: int = DEFAULT_TIMEOUT_S,
        command_sem: asyncio.Semaphore | None = None,
        declared_args: dict[str, Any] | None = None,
        setup_result: str | None = None,
        project: "ProjectFacts | None" = None,
    ):
        super().__init__(RustFormalResult, descriptor.backend_tag)
        self._module = module
        self._descriptor = descriptor
        self._sandbox = sandbox
        self._command_timeout_s = command_timeout_s
        self._command_sem = command_sem
        # The run's values for the wheel's own declared flags, put on every component's input.
        self._declared_args = declared_args or {}
        # The compiled setup spec (Crucible's fixture): on every component's input, and forwarded
        # to ``finalize`` so a callout-mode wheel can render the whole deliverable. A wheel that
        # declares a ``setup`` step reaches here only through :class:`RustStagedFormalizer`, which
        # authors the artifact before constructing this.
        self._setup_result = setup_result
        # What the preflight established about the project (see :class:`ProjectFacts`), carried on
        # every ``AuthorInput`` and mirrored into ``finalize`` — what ships must name the same
        # dependency the gated builds did.
        self._project = project or ProjectFacts()

    async def _sandbox_spec(self, workdir: Path) -> BackendSpec:
        if self._sandbox is None or not self._sandbox.enabled:
            return {"argv_prefix": [], "timeout_s": self._command_timeout_s}
        return await self._sandbox.backend_spec(workdir, timeout_s=self._command_timeout_s)

    # -- the session -------------------------------------------------------

    @override
    async def formalize(
        self,
        label: str,
        feat: FeatureUnit,
        props: list[PropertyFormulation],
        ctx: WorkflowContext[RustFormalResult],
        run: PipelineRun,
    ) -> RustFormalResult | GaveUp:
        workdir = Path(run.source.project_root)
        input = ComponentInput(
            program=str(run.source.contract_name),
            source_unit=self._project.source_unit,
            unit=feat.feature_json(),
            props=_properties([UnitProperty(feat.display_name, p) for p in props]),
            setup=self._setup_result,
            prep_facts=self._project.prep_facts,
            args=self._declared_args,
        )
        # Pure and pre-authoring: the report's shape is fixed before the first turn, so it never
        # depends on what the model happened to write. The session hands these to the author as the
        # names it must produce, and to the publish gate as the names it checks the mapping against.
        checks = parse_checks(self._module.checks(input.model_dump_json()))
        outcome = await run_session(
            module=self._module,
            input=input,
            kind="component",
            checks=checks,
            titles=[p.title for p in props],
            env=run.env,
            ctx=ctx,
            run=run,
            workdir=workdir,
            sandbox_dict=await self._sandbox_spec(workdir),
            descriptor=self._descriptor,
            emit=emit_event,
            command_sem=self._command_sem,
            description=label,
        )
        if isinstance(outcome, GaveUp):
            return outcome
        return RustFormalResult(
            commentary=outcome.commentary,
            artifact_text=outcome.spec,
            checks=outcome.property_checks,
            skipped=outcome.skipped,
            verdicts=outcome.verdicts,
            # The targets the gating run covered, in the order the host ran them — what a
            # callout-mode wheel keys its deliverable sections on. Derived from the checks that were
            # live at publish, so a skipped property's target is not claimed as checked.
            targets=[t.name for t in targets_of(live_checks(checks, outcome.skipped))],
        )

    @override
    async def fetch_verdicts(
        self, inp: ReportComponentInput[RustFormalResult]
    ) -> dict[RuleName, Verdict]:
        formalized = inp.formalized
        if formalized is None:
            return {}
        return {
            name: Verdict(
                outcome=v.outcome,
                line=v.line,
                duration_seconds=v.duration_seconds,
                unit_file=v.unit_file or formalized.unit_file,
                message=v.detail,
            )
            for name, v in formalized.result.verdicts.items()
        }

    @override
    async def finalize(
        self, outcomes: list[ComponentOutcome[RustFormalResult, FeatureUnit]], run: PipelineRun
    ) -> None:
        components = [
            FinalizeComponent(name=o.feat.display_name, outcome=ComponentGaveUp())
            if not isinstance(o.result, Delivered)
            # A callout-mode wheel renders the whole deliverable from these (Crucible: folds each
            # section into the shared crate, keyed by its property_checks feature) — including the
            # targets each check ran under, which its sections and declared features key on.
            else FinalizeComponent(
                name=o.feat.display_name,
                outcome=WireDelivered(
                    unit_file=o.result.unit_file,
                    run_link=o.result.run_link,
                    artifact_text=o.result.result.artifact_text,
                    property_checks=o.result.result.property_checks(),
                    skipped=[
                        WireSkipped(property_title=sk.property_title, reason=sk.reason)
                        for sk in o.result.result.skipped
                    ],
                    targets=list(o.result.result.targets),
                ),
            )
            for o in outcomes
        ]
        payload = FinalizeInput(
            program=str(run.source.contract_name),
            source_unit=self._project.source_unit,
            prep_facts=self._project.prep_facts,
            components=components,
            setup=self._setup_result,
        )
        raw = await asyncio.to_thread(self._module.finalize, payload.model_dump_json())
        if not raw:
            return
        files = parse_files(raw)
        root = Path(run.source.project_root)
        for rel, contents in files.items():
            target = confined_target(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)


@dataclass(frozen=True)
class ProjectFacts:
    """What the host established about the project under analysis — the outcome of
    :meth:`RustBackend.preflight`, handed forward to ``prepare_system`` and carried on every callout
    from there.

    Both fields are inputs every later callout needs, and neither follows from the analyzed model.
    Both are also **chain-shaped**: this is the one part of the seam whose vocabulary belongs to the
    analyzed project's build system rather than to the framework, so the host transports them without
    a schema (see :mod:`composer.rustapp.toolchain`). Carried rather than recomputed so the gated
    preflight build, every authoring turn, and the delivered artifact all agree on what they are
    building against."""

    #: Where the analyzed source lives as a unit of its own build system, from the chain's toolchain
    #: (:func:`source_unit_of`). Empty = nothing resolved, and the wheel applies its own convention.
    source_unit: dict[str, Any] = field(default_factory=dict)
    #: What the workspace prep established (:func:`run_workspace_prep`). Empty = it established
    #: nothing, which is what the wheel reads to decide how it sources the program's types.
    prep_facts: dict[str, Any] = field(default_factory=dict)


# Authors the shared setup spec for a run, given the properties it must make checkable. Built by
# :class:`RustPreparedSystem` and called from :meth:`RustStagedFormalizer.begin` — see there for why
# it runs between extraction and the per-unit fan-out rather than during prep or on first use.
type SetupAuthor = Callable[[list[UnitProperty], PipelineRun], Awaitable[str]]


class RustStagedFormalizer(StagedFormalizer[RustFormalResult, FeatureUnit]):
    """The formalizer for a wheel that declares a ``setup`` step, before its shared spec exists.

    ``author`` writes and compiles the artifact from the properties it must make checkable;
    ``build`` turns that artifact into the :class:`RustFormalizer` (see
    :meth:`RustPreparedSystem.prepare_formalization`, which closes over everything else the
    formalizer needs). Splitting it this way means the artifact is never assigned onto a live
    formalizer — the only formalizer that exists already has it."""

    def __init__(self, author: SetupAuthor, build: Callable[[str], RustFormalizer]):
        self._author = author
        self._build = build

    @override
    async def begin(
        self, jobs: Sequence[BackendJob[FeatureUnit]], run: PipelineRun
    ) -> RustFormalizer:
        """Author the shared setup spec from **every** unit's properties, and hand back the
        formalizer built around it.

        Two constraints fix this point in the run. It cannot happen in ``prepare_formalization``
        (which overlaps property extraction, so no properties exist yet), and it cannot happen
        lazily on first ``formalize`` (whichever unit won the race would decide the artifact the
        rest are then told to work within — see :class:`StagedFormalizer` and
        docs/crucible-component-units.md (PR3) §8.2). The driver calls this exactly between the two."""
        union = [UnitProperty(job.feat.display_name, prop) for job in jobs for prop in job.props]
        return self._build(await self._author(union, run))


@dataclass
class RustPreparedSystem(PreparedSystem[RustFormalResult, FeatureUnit, Any]):
    """Generic prepared system, descriptor-driven: author the optional shared ``setup`` artifact and
    build a formalizer carrying the injected context.

    The workspace itself was prepared and gated *before* analysis, by
    :meth:`RustBackend.preflight` — its outcome arrives here as :attr:`preflight`.

    Descriptor-driven throughout (``docs/rust-applications.md``), so an application needing a shared
    fixture, per-run serialization, or a context-thread of the fixture + declared args declares them
    rather than subclassing."""

    backend: "RustBackend"
    preflight: ProjectFacts
    analyzed: BaseApplication | None = None

    @override
    async def prepare_formalization(
        self, run: PipelineRun
    ) -> Formalizer[RustFormalResult, FeatureUnit] | StagedFormalizer[RustFormalResult, FeatureUnit]:
        """A wheel that declares no ``setup`` step gets its formalizer here. One that does gets a
        :class:`RustStagedFormalizer` instead — this method overlaps property extraction, so the
        properties its artifact must be authored from do not exist yet."""
        b = self.backend
        descriptor = b.descriptor
        workdir = Path(run.source.project_root)
        program = str(run.source.contract_name)
        # One shared workspace / build dir → serialize the toolchain runs (declared by the wheel).
        command_sem = asyncio.Semaphore(1) if descriptor.serialize_toolchain else None

        analyzed_json = self.analyzed.model_dump(mode="json") if self.analyzed is not None else {}
        project = self.preflight

        def build(setup_result: str | None) -> RustFormalizer:
            """The formalizer, around a shared setup spec that is either already authored or
            not called for."""
            return RustFormalizer(
                b.module, b.descriptor, sandbox=b.sandbox,
                command_timeout_s=b.command_timeout_s,
                command_sem=command_sem, declared_args=b.declared_args,
                setup_result=setup_result, project=project,
            )

        setup = descriptor.step(PhaseRole.SETUP)
        if setup is None:
            return build(None)
        # The base for the setup spec's own input; ``author_setup`` adds the properties.
        prep_input = SetupInput(
            program=program, source_unit=project.source_unit, model=analyzed_json,
            prep_facts=project.prep_facts, args=b.declared_args,
        )

        async def author_setup(props: list[UnitProperty], run: PipelineRun) -> str:
            # The properties are what the artifact must make checkable, so they are part of both
            # the prompt and the cache identity
            setup_input = prep_input.with_props(_properties(props))
            # Cached like a formalization result (and skipped entirely on a hit): authoring +
            # compiling this is a full LLM loop, and on a large program the longest single step
            # of a run — so a re-run after a failure downstream must not pay for it twice. Keyed
            # by what it is authored *from*, so a changed model, program crate, type source
            # (crate vs IDL) or property set re-authors it. As with the driver's other caches, a
            # change to the *prompt* does not invalidate — clear the namespace for that.
            setup_ctx: WorkflowContext[RustSetupSpec] = run.ctx.child(
                CacheKey(f"{descriptor.name}-setup-{_setup_identity(setup_input)}")
            )
            if (hit := await setup_ctx.cache_get(RustSetupSpec)) is not None:
                return hit.source
            sandbox_dict = await b.sandbox_spec(workdir)
            fixture = await run.runner(
                b.task_info(setup),
                lambda: run_session(
                    module=b.module,
                    input=setup_input,
                    kind="setup",
                    checks=[],
                    titles=[o.prop.title for o in props],
                    env=run.env,
                    ctx=setup_ctx,
                    run=run,
                    workdir=workdir,
                    sandbox_dict=sandbox_dict,
                    descriptor=descriptor,
                    emit=emit_event,
                    command_sem=command_sem,
                    description=setup.label,
                ),
            )
            if isinstance(fixture, GaveUp):
                raise RuntimeError(f"{descriptor.name} setup gave up: {fixture.reason}")
            await setup_ctx.cache_put(RustSetupSpec(source=fixture.spec))
            return fixture.spec

        return RustStagedFormalizer(author_setup, build)


@dataclass
class RustBackend(
    PipelineBackend[
        enum.Enum, RustFormalResult, Any, RustArtifact, FeatureUnit, Any, BaseApplication,
        ProjectFacts,
    ]
):
    """A :class:`PipelineBackend` backed by a Rust wheel. Ecosystem-agnostic: it locates the main
    and marshals units through the resolved ``ecosystem`` + the ``FeatureUnit`` protocol, so its
    unit / main / app axes stay open (``Any``) where a single-ecosystem backend would pin them.

    Subclass (or replace via ``backend_cls``) when the app needs non-generic prep — e.g.
    Crucible's shared fixture + harness crate."""

    module: RustAppModule
    descriptor: AppDescriptor
    #: The phase enum synthesized from the descriptor — the *same* class object the frontend's
    #: ``phase_labels`` are keyed by, since the lookup is by member identity. Public because the
    #: prepared system and the formalizer need to tag their own tasks with a declared phase; reach
    #: it through :meth:`task_info` rather than indexing it.
    phase: type[enum.Enum]
    #: The four core slots of :attr:`phase`, as the driver's own mapping.
    core_phases: CorePhases
    store: ArtifactStore[Any, RustFormalResult]
    ecosystem: Ecosystem[Any, Any, Any]
    # Wall-clock ceiling for a single compile/validate (a first build can be minutes).
    command_timeout_s: int = DEFAULT_TIMEOUT_S
    # How to confine every toolchain run (docs/command-sandbox.md). None → unsandboxed.
    sandbox: SandboxConfig | None = None
    # Parsed values of the descriptor's declared CLI args, put on every component's
    # ``AuthorInput.args`` (e.g. Crucible's ``fuzz_timeout``). Set by the entry point.
    declared_args: dict[str, Any] = field(default_factory=dict)

    # Both are the wheel's to state, so they are derived from its descriptor rather than passed.
    backend_guidance: str = field(init=False)
    analysis_spec: SystemAnalysisSpec = field(init=False)

    def __post_init__(self) -> None:
        self.backend_guidance = self.descriptor.backend_guidance
        self.analysis_spec = SystemAnalysisSpec(self.descriptor.analysis_key, "rust-properties")

    @property
    @override
    def artifact_store(self) -> ArtifactStore[Any, RustFormalResult]:
        return self.store

    def task_info(self, phase: PhaseSpec) -> TaskInfo[enum.Enum]:
        """The task a step-declaring phase runs as: an id from its role, the wheel's label, and the
        phase *member* itself.

        The one way to turn a declared phase into a member. The enum is synthesized per application,
        so a caller can't name a member statically — and resolving it here keeps the member the
        driver emits identical to the one the frontend's labels are keyed by."""
        return TaskInfo(
            f"{self.descriptor.name}-{phase.role.value}", phase.label, self.phase[phase.key]
        )

    @override
    async def preflight(self, run: PipelineRun) -> ProjectFacts:
        """Prepare the wheel's workspace and gate it — everything buildable before the program has
        been analyzed, run concurrently with system analysis (``docs/rust-applications.md`` §4.2).

        Two steps, both *declared* by the wheel and executed here (``docs/rust-applications.md`` §7):

        1. :func:`run_workspace_prep` — place the wheel's build files, and (through the chain's
           :class:`~composer.rustapp.toolchain.ProjectToolchain`) carry out whatever preparing the
           analyzed project takes. Already the run's slowest non-LLM step.
        2. :func:`run_preflight_gate`, when the descriptor declares a ``preflight`` — build a
           skeleton artifact *the wheel authors itself* through the real toolchain, in the real
           sandbox. This is what turns step 1 from "we placed some build files" into "this workspace
           compiles": warming a dependency cache resolves a graph but compiles nothing, and its
           failures are deliberately non-fatal. Without the gate the first *check* of the workspace
           is the first authored draft's build — after the whole extraction phase, and reported as
           compiler errors an authoring agent cannot fix because it does not own the build files.

        Neither step reads the analyzed model or any property, which is what makes the overlap safe.
        A failure raises (:class:`PreflightFailed` from the gate, or the workspace toolchain's own
        error), and the driver cancels the analysis racing it."""
        descriptor = self.descriptor
        gate = descriptor.step(PhaseRole.PREFLIGHT)
        workdir = Path(run.source.project_root)
        # Resolved once per run and carried on every AuthorInput from here on: the wheel renders its
        # build files from this, so prep, every gated build, and the deliverable agree on what they
        # are building against.
        unit = source_unit_of(self.ecosystem, run.source)
        # Declared args are in scope from the start: prep may need one (Crucible reads
        # ``program_idl`` when deciding how to source the program's types).
        prep_input = PreflightInput(
            program=str(run.source.contract_name),
            source_unit=unit, args=dict(self.declared_args),
        )

        async def prep() -> ProjectFacts:
            prep_facts = await run_workspace_prep(
                self.module, prep_input, chain=self.ecosystem.name, source=run.source,
                sandbox=self.sandbox, command_timeout_s=self.command_timeout_s,
            )
            result = ProjectFacts(source_unit=unit, prep_facts=prep_facts)
            if gate is not None:
                # The gate renders the same workspace the prep just set up — including whatever it
                # established — so it must see the reported facts.
                await run_preflight_gate(
                    self.module,
                    prep_input.with_prep_facts(result.prep_facts),
                    workdir=workdir,
                    sandbox_dict=await self.sandbox_spec(workdir),
                )
            return result

        if gate is None:
            # Nothing to show a task for: the prep is silent (it always was) and there is no gate.
            return await prep()
        # This is a build, not an agent: it belongs to the run's CPU budget, not to the
        # ``--max-concurrent`` agent slots it would otherwise hold for the whole of system analysis.
        return await run.cpu_runner(self.task_info(gate), prep)

    async def sandbox_spec(self, workdir: Path) -> BackendSpec:
        """The confinement prefix the wheel's blocking callouts prepend, or the trusted empty one."""
        if self.sandbox is not None and self.sandbox.enabled:
            return await self.sandbox.backend_spec(workdir, timeout_s=self.command_timeout_s)
        return {"argv_prefix": [], "timeout_s": self.command_timeout_s}

    @override
    async def prepare_system(
        self, analyzed: BaseApplication, run: PipelineRun, preflight: ProjectFacts
    ) -> PreparedSystem[RustFormalResult, FeatureUnit, Any]:
        return RustPreparedSystem(
            self.ecosystem.locate_main(analyzed, run.source), self, preflight, analyzed
        )

    @override
    def to_artifact_id(self, c: FeatureUnit) -> RustArtifact:
        return RustArtifact(
            c.slug,
            self.descriptor.artifact_layout.artifact_prefix,
            self.descriptor.artifact_layout.artifact_extension,
        )
