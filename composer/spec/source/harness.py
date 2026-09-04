"""
Harness analysis and prover setup.

Identifies external contracts, classifies them, generates harness files
for contracts needing multiple instances, and runs AutoSetup compilation
to produce a ``Configuration`` for downstream phases.

Two entry points:

``analyze_external_interactions``
    Library entry point: runs the classification agent and returns a
    ``HarnessSetup`` with classifications + generated VFS files.  Uses
    an in-memory checkpointer and no memory tool.

``setup_and_harness_agent``
    Pipeline entry point: runs the classification agent within a
    ``WorkflowContext``, writes harness files to disk, runs AutoSetup
    compilation, and returns a ``Configuration``.
"""

from typing import AsyncIterator, NotRequired, TypedDict, override
from contextlib import ExitStack, asynccontextmanager
from logging import getLogger
from pathlib import Path
import asyncio
import shutil

from pydantic import Field, BaseModel, ValidationError

from langgraph.graph import MessagesState

from composer.prover.core import ProverOptions
from composer.spec.natspec.async_result import AsyncResultTool
from graphcore.graph import FlowInput
from graphcore.tools.schemas import WithInjectedState
from graphcore.tools.vfs import VFSAccessor, VFSState, VFSToolConfig, vfs_tools

from composer.diagnostics.timing import get_run_summary
from composer.spec.graph_builder import run_to_completion, bind_standard
from composer.spec.source.autosetup import run_autosetup, read_autosetup_usage, read_autosetup_prover_usage, SetupFailure, SetupSuccess
from composer.spec.service_host import ServiceHost
from composer.spec.context import WorkflowContext, SourceCode, CacheKey
from composer.spec.key_family import KeyFamily
from composer.spec.util import string_hash
from composer.spec.gen_types import TypedTemplate, certora_relative_to_project, under_project
from composer.spec.system_model import (
    HarnessDefinition, HarnessedApplication, HarnessedExplicitContract,
    SolidityIdentifier, SourceApplication, SourceExternalActor, SourceExplicitContract,
)

_logger = getLogger(__name__)

def system_setup_key(s: SourceApplication) -> CacheKey["ContractSetup", "SystemDescriptionHarnessed"]:
    return CacheKey["ContractSetup", "SystemDescriptionHarnessed"](
        "system-setup-" + string_hash(s.model_dump_json())
    )

class LinkField(BaseModel):
    """
    Expressing a "linking" relationship
    """
    target : list[SolidityIdentifier] = Field(description=(
        "The Solidity identifier(s) of the contract(s) being linked to — must match "
        "the `solidity_identifier` of an entry in the application description's "
        "transitive closure."
    ))
    link_paths: list[str] = Field(description="The list of Solidity storage access paths linking to `target`")


class ClosureContractBase(BaseModel):
    """
    A contract in the transitive closure.
    """
    solidity_identifier: SolidityIdentifier = Field(description=(
        "The Solidity identifier of the contract — must match the `solidity_identifier` "
        "of the corresponding entry in the application description."
    ))
    link_fields: list[LinkField] = Field(description="The linking relationship with other contracts in the closure")

class ClosureContract(ClosureContractBase):
    """
    A contract in the transitive closure.
    """
    num_instances : int | None = Field(description="The number of instances (> 1) of this contract needed to model a non-trivial state; None if a single instance is sufficient")

class HarnessingDetermination(BaseModel):
    """
    A determination that a multiple harness instances are required for this contract
    """
    n : int = Field(description="The number of harnesses needed for a non-trivial state; must be > 1", gt=1)

class TaggedClosureContract(ClosureContractBase):
    __doc__ = ClosureContract.__doc__
    
    harness_determination : HarnessingDetermination | None = Field(description="The harnessing determination for this contract")

    @property
    def num_instances(self) -> int | None:
        return None if self.harness_determination is None else self.harness_determination.n

class HarnessDef(BaseModel):
    harness_of: SolidityIdentifier
    harness_source: str

class HarnessedContract(ClosureContractBase):
    harness_definition : HarnessDef | None
    path: str

class ExternalInterface(BaseModel):
    """
    An external actor interacted through an interface which is NOT included in the transitive closure
    """
    name: str = Field(description="The name of the external actor (taken from the application description)")
    behavioral_spec: str = Field(description="A natural language description of the behavior of the interface expected" \
    " by the contracts in the closure.")

class SystemDescriptionBase[T: ClosureContractBase](BaseModel):
    non_trivial_state: str = Field(description="A semi-formal description of a `non-trivial state`.")
    transitive_closure: list[T] = Field(description="The list of contracts in the transitive closure that interact with the main contract")
    erc20_contracts: list[SolidityIdentifier] = Field(description=(
        "A list of the Solidity identifiers (matching `solidity_identifier` "
        "entries in the application description) of the contracts which are ERC20 tokens"
    ))
    external_interfaces: list[ExternalInterface] = Field(description="A list of the external contract actors interacted with by the closure")


class AgentSystemDescription(SystemDescriptionBase[TaggedClosureContract]):
    """
    The result of your analysis
    """

    def needs_harnessing(self) -> bool:
        return any([
            c.num_instances for c in self.transitive_closure
        ])
    
class LocatedClosureContract(ClosureContract):
    path: str

class LocatedSystemDescription(SystemDescriptionBase[LocatedClosureContract]):
    pass

class SystemDescriptionHarnessed(SystemDescriptionBase[HarnessedContract]):
    pass

class HarnessAnalysisParams(TypedDict):
    contract_name: str
    relative_path: str
    context: SourceApplication


class ContractSetup(BaseModel):
    system_description: SystemDescriptionHarnessed
    config: SetupSuccess


def lift_harnessed(
    s: SourceApplication, sys_desc: SystemDescriptionHarnessed,
) -> HarnessedApplication:
    """Re-key harness definitions by harnessed contract and fold them into a
    ``HarnessedApplication`` — each ``SourceExplicitContract`` becomes a
    ``HarnessedExplicitContract`` carrying the harnesses generated for it.

    Declared beside the models because every consumer of the harnessed view
    must build it identically — the prover pipeline, and any offline walker
    re-deriving component cache keys (``composer.cli.cache_autoprove``): the harnessed
    app is part of the unit's ``cache_material``."""
    contract_to_harness: dict[SolidityIdentifier, list[HarnessDefinition]] = {}
    for c in sys_desc.transitive_closure:
        if not c.harness_definition:
            continue
        contract_to_harness.setdefault(c.harness_definition.harness_of, []).append(
            HarnessDefinition(name=c.solidity_identifier, path=c.path)
        )

    comp: list[SourceExternalActor | HarnessedExplicitContract] = []
    for c in s.components:
        if not isinstance(c, SourceExplicitContract):
            comp.append(c)
            continue
        comp.append(HarnessedExplicitContract(
            sort=c.sort,
            name=c.name,
            solidity_identifier=c.solidity_identifier,
            components=c.components,
            description=c.description,
            path=c.path,
            harnesses=contract_to_harness.get(c.solidity_identifier, []),
        ))
    return HarnessedApplication(
        application_type=s.application_type, description=s.description, components=comp,
    )


HarnessAnalysis = TypedTemplate[HarnessAnalysisParams]("state_analysis.j2")

HARNESS_ANALYSIS_KEY = CacheKey[SystemDescriptionHarnessed, AgentSystemDescription]("harness-analysis")

async def classifier_agent(
    context: WorkflowContext[SystemDescriptionHarnessed],
    app: SourceApplication,
    source: SourceCode,
    env: ServiceHost,
) -> AgentSystemDescription:
    child = context.child(HARNESS_ANALYSIS_KEY)
    if (cached := await child.cache_get(AgentSystemDescription)) is not None:
        return cached
    class AnalysisState(MessagesState):
        result: NotRequired[AgentSystemDescription]

    bound = HarnessAnalysis.bind({
        "context": app,
        "contract_name": source.contract_name,
        "relative_path": source.relative_path
    })

    external_lkp = {
        c.name: c for c in app.components if isinstance(c, SourceExternalActor)
    }

    contract_lkp = {
        c.solidity_identifier: c for c in app.contract_components
    }

    def result_validator(
        s: AnalysisState,
        res: AgentSystemDescription
    ) -> str | None:
        for ext in res.external_interfaces:
            if ext.name not in external_lkp:
                return f"External interface {ext.name} does not appear in the system description"
            if external_lkp[ext.name].path is None:
                return f"External interface {ext.name} doesn't have a path, and can't be identified as an interface"
        for c in res.transitive_closure:
            if c.solidity_identifier not in contract_lkp:
                return f"Contract {c.solidity_identifier} in the interaction closure doesn't appear in the application description"
        return None

    d = bind_standard(
        builder=env.builder_lite(),
        state_type=AnalysisState,
        validator=result_validator
    ).with_input(
        FlowInput
    ).with_tools(
        [child.get_memory_tool(), *env.source_tools]
    ).inject(
        lambda g: bound.render_to(g.with_initial_prompt_template)
    ).with_sys_prompt_template(
        "state_analysis_system_prompt.j2"
    ).compile_async()

    res = await run_to_completion(
        graph=d,
        context=None,
        description="Harness Analysis",
        recursion_limit=child.recursion_limit,
        input=FlowInput(input=[]),
        thread_id=child.thread_id
    )

    assert "result" in res
    await child.cache_put(res["result"])
    return res["result"]

_HARNESS_CHECK_TIMEOUT_S = 900


class ForgeDiagnostic(BaseModel):
    """One diagnostic of a ``forge build`` report."""
    severity: str
    message: str = ""
    formatted_message: str = Field(default="", alias="formattedMessage")

    @property
    def rendered(self) -> str:
        """The diagnostic as solc rendered it: the source excerpt and the notes
        that go with it, falling back to the bare message if forge gave none."""
        return self.formatted_message or self.message


class ForgeReport(BaseModel):
    """A ``forge build --json`` report.

    ``errors`` holds every diagnostic, warnings included; ``severity`` is what
    separates them. The exit code carries no information — forge exits 0
    whether or not the sources compiled.
    """
    errors: list[ForgeDiagnostic] = Field(default_factory=list)

    @property
    def compile_errors(self) -> list[str]:
        return [d.rendered for d in self.errors if d.severity == "error"]


@asynccontextmanager
async def _materialized[S](accessor: VFSAccessor[S], state: S) -> AsyncIterator[str]:
    """The agent's filesystem view as a real directory, for the block's
    duration. The copy and its teardown run in a worker thread: materializing a
    whole project is blocking IO that would otherwise stall the event loop."""
    stack = ExitStack()
    project_dir = await asyncio.to_thread(stack.enter_context, accessor.materialize(state))
    try:
        yield project_dir
    finally:
        await asyncio.to_thread(stack.close)


async def _compile_check(project_dir: str, harness_paths: list[str]) -> str | None:
    """Type-check the named harnesses within a materialized copy of the project.

    Returns the compiler's error output, or None when they compile — or when
    the project offers nothing to check them with (not a foundry project, no
    forge binary, or a build that never produced a report), in which case the
    candidates are accepted unchecked.
    """
    root = Path(project_dir)
    forge = shutil.which("forge")
    if forge is None or not (root / "foundry.toml").is_file():
        _logger.info("Harness compile check skipped: needs forge and a foundry project")
        return None
    proc = await asyncio.create_subprocess_exec(
        forge, "build", "--json", *sorted(harness_paths),
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_HARNESS_CHECK_TIMEOUT_S
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        _logger.warning(
            "Harness compile check skipped: forge build exceeded %ds", _HARNESS_CHECK_TIMEOUT_S
        )
        return None

    try:
        report = ForgeReport.model_validate_json(stdout_b.decode())
    except ValidationError:
        _logger.warning(
            "Harness compile check skipped: forge emitted no report: %s", stderr_b.decode()[-500:]
        )
        return None
    if not report.compile_errors:
        return None
    return "\n".join(report.compile_errors)


class GeneratedHarness(BaseModel):
    """A generated harness file that creates a uniquely-named contract extending an external contract."""
    path: str = Field(description="Path to the harness definition")
    harness_name: SolidityIdentifier = Field(description="The Solidity identifier of the contract defined in the harness file")

class GeneratedHarnessSource(GeneratedHarness):
    source: str

class HarnessAgentResult(BaseModel):
    """
    The results of your harness generation
    """
    identifier_to_source: dict[SolidityIdentifier, list[GeneratedHarness]] = Field(description=(
        "A map from each target contract's `solidity_identifier` (exactly as given in "
        "the input list) to the harnesses chosen for it."
    ))

class HarnessResult(BaseModel):
    identifier_to_source: dict[SolidityIdentifier, list[GeneratedHarnessSource]]

class HarnessInput(BaseModel):
    path: str
    n_harnesses: int
    solidity_identifier: SolidityIdentifier

class HarnessGenParams(TypedDict):
    to_harness: list[HarnessInput]

_HarnessGenerationPrompt = TypedTemplate[HarnessGenParams]("harness_generation_prompt.j2")

def _system_setup_key(s: SourceApplication) -> str:
    return "system-setup-" + string_hash(s.model_dump_json())

SYSTEM_SETUP_KEY = KeyFamily(ContractSetup, SystemDescriptionHarnessed, _system_setup_key)

def _harness_generation_key(instructions: AgentSystemDescription) -> str:
    return string_hash(instructions.model_dump_json())

HARNESS_GENERATION_KEY = KeyFamily(
    SystemDescriptionHarnessed, HarnessResult, _harness_generation_key
)

async def generate_harnesses(
    context: WorkflowContext[SystemDescriptionHarnessed],
    env: ServiceHost,
    source: SourceCode,
    application: SourceApplication,
    instructions: AgentSystemDescription
) -> HarnessResult:
    child = await context.child(HARNESS_GENERATION_KEY(instructions), instructions.model_dump())
    if (cached := await child.cache_get(HarnessResult)) is not None:
        return cached

    tool_conf = VFSToolConfig(
        fs_layer=source.project_root,
        immutable=False,
        put_doc_extra="You may only write into the `certora/harnesses` directory",
        forbidden_write="^(?!certora/harnesses)",
        forbidden_read=source.forbidden_read
    )

    class GenerationState(MessagesState, VFSState):
        result: NotRequired[HarnessAgentResult]
    
    class GenerationInput(FlowInput, VFSState):
        pass

    v_tools, mat = vfs_tools(tool_conf, GenerationState)

    contract_paths = {
        c.solidity_identifier: c.path for c in application.contract_components
    }

    harness_inputs = [
        HarnessInput(
            solidity_identifier=c.solidity_identifier,
            n_harnesses=c.num_instances,
            path=contract_paths[c.solidity_identifier]
        )
        for c in instructions.transitive_closure if c.num_instances is not None
    ]

    bound_template = _HarnessGenerationPrompt.bind({
        "to_harness": harness_inputs
    })

    expected = {
        c.solidity_identifier: c.n_harnesses for c in harness_inputs
    }


    class HarnessResultTool(AsyncResultTool[HarnessAgentResult], WithInjectedState[GenerationState]):
        """Signal the completion of your workflow. Triggers a compile of the
        delivered harnesses against the project; a compile failure is reported
        back to you for a retry.
        """

        @override
        async def validate_result(self, res: HarnessAgentResult) -> str | None:
            check_copy = expected.copy()
            harness_paths: list[str] = []
            for (nm, r) in res.identifier_to_source.items():
                if nm not in check_copy:
                    return f"Delivered result for contract {nm}, but no instructions were given to harness it"
                if len(r) != check_copy[nm]:
                    return f"Delivered {len(r)} harnesses for {nm}, but {check_copy[nm]} were required"
                for res_c in r:
                    if mat.get(self.state, res_c.path) is None:
                        return f"Delivered harness {res_c.harness_name} at {res_c.path} for {nm}, but it doesn't exist on the VFS"
                    harness_paths.append(res_c.path)
                del check_copy[nm]
            if len(check_copy) != 0:
                error = ", ".join(
                    [ f"contract {k} ({n} copies)" for (k,n) in check_copy.items() ]
                )
                return f"Missing harnesses in results: {error}"
            # Compile the agent's view of the project, not the project on disk: the
            # harnesses it wrote live on the VFS, and a helper it wrote but did not
            # deliver still has to be there for their imports to resolve.
            async with _materialized(mat, self.state) as project_dir:
                compile_errors = await _compile_check(project_dir, harness_paths)
            if compile_errors is not None:
                return (
                    "The delivered harnesses do not compile. Repair them and deliver again; "
                    "the compiler reported:\n" + compile_errors
                )
            return None

    g = env.builder_lite().with_input(
        GenerationInput
    ).with_state(
        GenerationState
    ).with_output_key(
        "result"
    ).inject(
        lambda g: bound_template.render_to(g.with_initial_prompt_template)
    ).with_sys_prompt_template(
        "harness_generation_system_prompt.j2"
    ).with_tools(
        v_tools + [HarnessResultTool.as_tool("result")]
    ).with_default_summarizer().compile_async()

    res_state = await run_to_completion(
        graph=g,
        input=GenerationInput(input=[], vfs={}),
        context=None,
        description="Harness Implementation Generation",
        recursion_limit=child.recursion_limit,
        thread_id=child.thread_id
    )

    assert "result" in res_state

    res_dict : dict[SolidityIdentifier, list[GeneratedHarnessSource]] = {}
    for (nm, r) in res_state["result"].identifier_to_source.items():
        generated_source : list[GeneratedHarnessSource] = []
        for gen in r:
            source_code = mat.get(res_state, gen.path)
            assert source_code is not None, gen.path
            generated_source.append(GeneratedHarnessSource(
                path=gen.path,
                harness_name=gen.harness_name,
                source=source_code.decode("utf-8")
            ))
        res_dict[nm] = generated_source
    to_ret = HarnessResult(
        identifier_to_source=res_dict
    )
    await child.cache_put(to_ret)
    return to_ret

def _multi_replace(
    s: list[SolidityIdentifier],
    patch: dict[SolidityIdentifier, list[SolidityIdentifier]]
) -> list[SolidityIdentifier]:
    to_ret = []
    for i in s:
        if i in patch:
            to_ret.extend(patch[i])
        else:
            to_ret.append(i)
    return to_ret

def _patch_links(
    s: list[LinkField],
    patch: dict[SolidityIdentifier, list[SolidityIdentifier]]
) -> list[LinkField]:
    return [
        LinkField(
            link_paths=f.link_paths,
            target=_multi_replace(f.target, patch)
        ) for f in s
    ]

def apply_harness_result(
    s: LocatedSystemDescription,
    harness_result: HarnessResult
) -> SystemDescriptionHarnessed:
    new_contracts : list[HarnessedContract] = []
    forward_link = {
        k: [ h.harness_name for h in v ] for (k, v) in harness_result.identifier_to_source.items()
    }
    for c in s.transitive_closure:
        if not c.num_instances:
            new_contracts.append(HarnessedContract(
                solidity_identifier=c.solidity_identifier,
                link_fields=_patch_links(c.link_fields, forward_link),
                harness_definition=None,
                path=c.path
            ))
            continue
        patched_links = _patch_links(c.link_fields, forward_link)
        for gen in harness_result.identifier_to_source[c.solidity_identifier]:
            new_contracts.append(HarnessedContract(
                harness_definition=HarnessDef(
                    harness_of=c.solidity_identifier,
                    harness_source=gen.source,
                ),
                solidity_identifier=gen.harness_name,
                link_fields=patched_links,
                path=gen.path
            ))
    return SystemDescriptionHarnessed(
        erc20_contracts=s.erc20_contracts,
        external_interfaces=s.external_interfaces,
        non_trivial_state=s.non_trivial_state,
        transitive_closure=new_contracts
    )



async def run_setup_part1(
    context: WorkflowContext[ContractSetup],
    source: SourceCode,
    env: ServiceHost,
    application_desc: SourceApplication
) -> SystemDescriptionHarnessed:
    setup_ctx = await context.child(SYSTEM_SETUP_KEY(application_desc), application_desc.model_dump())
    if (cached := await setup_ctx.cache_get(SystemDescriptionHarnessed)):
        return cached

    analysis_results = await classifier_agent(
        context=setup_ctx,
        app=application_desc,
        env=env,
        source=source
    )

    name_to_path = {
        c.solidity_identifier: c.path for c in application_desc.contract_components
    }

    located_desc = LocatedSystemDescription(
        non_trivial_state=analysis_results.non_trivial_state,
        erc20_contracts=analysis_results.erc20_contracts,
        external_interfaces=analysis_results.external_interfaces,
        transitive_closure=[
            LocatedClosureContract(
                link_fields=c.link_fields,
                solidity_identifier=c.solidity_identifier,
                num_instances=c.num_instances,
                path=name_to_path[c.solidity_identifier]
            ) for c in analysis_results.transitive_closure
        ]
    )

    harnessed_system : SystemDescriptionHarnessed

    if analysis_results.needs_harnessing():
        harness_result = await generate_harnesses(
            application=application_desc,
            context=setup_ctx,
            env=env,
            instructions=analysis_results,
            source=source
        )

        harnessed_system = apply_harness_result(
            located_desc,
            harness_result
        )
    else:
        harnessed_system = SystemDescriptionHarnessed(
            non_trivial_state=analysis_results.non_trivial_state,
            erc20_contracts=analysis_results.erc20_contracts,
            external_interfaces=analysis_results.external_interfaces,
            transitive_closure=[
                HarnessedContract(
                    link_fields=c.link_fields,
                    solidity_identifier=c.solidity_identifier,
                    harness_definition=None,
                    path=c.path
                ) for c in located_desc.transitive_closure
            ]
        )
    await setup_ctx.cache_put(harnessed_system)
    return harnessed_system

async def run_and_apply_part1(
    context: WorkflowContext[ContractSetup],
    source: SourceCode,
    env: ServiceHost,
    application_desc: SourceApplication
) -> SystemDescriptionHarnessed:
    res = await run_setup_part1(context, source, env, application_desc)
    for c in res.transitive_closure:
        if c.harness_definition is not None:
            tgt = Path(source.project_root) / c.path
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(c.harness_definition.harness_source)
    return res

config_key = CacheKey[None, ContractSetup]("config")


# ---------------------------------------------------------------------------
# Split phases.
#
# Harness creation and AutoSetup are exposed as two separate, independently
# cached steps so the pipeline can run AutoSetup in parallel with invariant/bug
# analysis. They share the ``config_key`` parent context, so existing
# harness-creation caches (keyed by ``SYSTEM_SETUP_KEY``) still hit; the
# AutoSetup result is cached under its own key.
# ---------------------------------------------------------------------------

async def run_harness_creation(
    context: WorkflowContext[None],
    source: SourceCode,
    env: ServiceHost,
    application_desc: SourceApplication,
) -> SystemDescriptionHarnessed:
    """Classify external contracts, generate harness files, and write them to
    disk. ``run_and_apply_part1`` re-writes the harness files on every call
    (idempotent), so they are guaranteed present for the AutoSetup phase even on
    a cache hit."""
    config_ctxt = context.child(config_key)
    return await run_and_apply_part1(config_ctxt, source, env, application_desc)


def _autosetup_key(
    app: SourceApplication,
    prover_opts: ProverOptions,
) -> str:
    return "autosetup-" + string_hash(
        app.model_dump_json() + "\x00" + "\x00".join(prover_opts.extra_args)
    )

#: Cache key for the AutoSetup phase. Includes ``prover_opts`` so cloud and
#: local configurations never collide (the old composite ``config_key`` omitted
#: them, which could reuse a stale config across modes).
AUTOSETUP_KEY = KeyFamily(ContractSetup, SetupSuccess, _autosetup_key)


async def run_autosetup_phase(
    context: WorkflowContext[None],
    source: SourceCode,
    sys_desc: SystemDescriptionHarnessed,
    application_desc: SourceApplication,
    prover_opts: ProverOptions,
) -> SetupSuccess:
    """Run AutoSetup compilation against the (already written) harness files and
    return the compilation config + summaries. Depends on harness creation
    having run first: it reads the transitive-closure file paths from disk.

    Cache hits are guarded by the on-disk existence of ``summaries_path``."""
    config_ctxt = context.child(config_key)
    cache = await config_ctxt.child(
        AUTOSETUP_KEY(application_desc, prover_opts),
        application_desc.model_dump(),
    )
    if (cached := await cache.cache_get(SetupSuccess)) is not None:
        if under_project(source.project_root, certora_relative_to_project(cached.summaries_path)).exists():
            return cached

    extra_files = [
        c.path for c in sys_desc.transitive_closure if c.solidity_identifier != source.contract_name
    ]

    setup_result = await run_autosetup(
        Path(source.project_root),
        source.relative_path,
        source.contract_name,
        prover_opts,
        *extra_files,
    )

    if isinstance(setup_result, SetupFailure):
        raise RuntimeError(f"Auto setup failed: {setup_result.error}\nProc stderr:\n{setup_result.stderr}")

    # AutoSetup runs as a subprocess; its LLM token usage never reaches composer's
    # UsageCallback. Fold the counts it wrote to disk into the run summary so they
    # land in job_info.json, the run tag, and the end-of-run table. No task_id:
    # the active task is already AUTOSETUP_TASK_ID, so this attributes to the
    # autosetup phase. Guarded — read_autosetup_usage returns [] if absent. This is
    # only reached on a cache miss (cache hits return above), so usage spent in this
    # process's autosetup run is counted exactly once.
    summary = get_run_summary()
    for usage in read_autosetup_usage(Path(source.project_root)):
        summary.record_token_usage(usage)
    # Likewise fold AutoSetup's subprocess prover runtime (prover-reported, cache hits
    # excluded) into the run's prover_usage under the active AUTOSETUP_TASK_ID. None if
    # absent — guarded so missing external usage can't break the phase.
    if (autosetup_prover_ms := read_autosetup_prover_usage(Path(source.project_root))) is not None:
        summary.record_prover_runtime(autosetup_prover_ms)

    await cache.cache_put(setup_result)
    return setup_result

