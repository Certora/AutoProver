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

from typing import NotRequired, TypedDict
from pathlib import Path
import subprocess

from pydantic import Field, BaseModel

from langgraph.graph import MessagesState

from composer.prover.core import ProverOptions
from graphcore.graph import FlowInput
from graphcore.tools.vfs import VFSState, VFSToolConfig, vfs_tools
from graphcore.tools.results import result_tool_generator

from composer.diagnostics.timing import get_run_summary
from composer.spec.graph_builder import run_to_completion, bind_standard
from composer.spec.source.autosetup import run_autosetup, read_autosetup_usage, read_autosetup_prover_usage, SetupFailure, SetupSuccess
from composer.spec.service_host import ServiceHost
from composer.spec.context import WorkflowContext, SourceCode, CacheKey
from composer.spec.util import string_hash
from composer.spec.gen_types import TypedTemplate, certora_relative_to_project, under_project
from composer.spec.system_model import SolidityIdentifier, SourceApplication, SourceExternalActor, SourceExplicitContract

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
    num_instances : int | None = Field(description="The number of instances of this contract needed to model a non-trivial state (None if N/A)")

class HarnessDef(BaseModel):
    harness_of: SolidityIdentifier
    harness_source: str

class HarnessedContract(ClosureContractBase):
    harness_definition : HarnessDef | None
    path: str

class UnstructuredSlotSpec(BaseModel):
    """
    A piece of main-contract state living in "unstructured" storage (assembly
    sload/sstore, `StorageSlot`, ERC-7201 namespaces, constant keccak slots)
    with no public getter, to be exposed via a harness getter.
    """
    getter_name: str = Field(description="The name of the external view getter the harness should expose for this slot")
    slot_derivation: str = Field(description=(
        "How the storage slot is derived: the keccak preimage or namespace string "
        "(e.g. `keccak256(\"river.state.balance\") - 1` or the ERC-7201 namespace id)"
    ))
    value_type: str = Field(description="The Solidity type of the value read from the slot (e.g. `uint256`, `address`)")
    rationale: str = Field(description="One line: why verification needs to observe this state")

class HelperDecomposition(BaseModel):
    """
    A monolithic external function to be decomposed into thin external wrappers
    around the existing internal helpers it calls.
    """
    target_function: str = Field(description="The signature/name of the monolithic external function being decomposed")
    helpers: dict[str, str] = Field(description=(
        "The thin external wrappers to expose: a map from wrapper name to a one-line "
        "behavioral contract of the internal step it wraps"
    ))
    rationale: str = Field(description="One line: why verification needs the decomposed entry points")

class MainHarnessPlan(BaseModel):
    """
    The plan for the main-contract augmentation harness: getters for unstructured
    storage plus helper decompositions of monolithic functions. Purely additive —
    the harness never changes or duplicates protocol behavior.
    """
    unstructured_slots: list[UnstructuredSlotSpec] = Field(description="The unstructured-storage getters to expose (may be empty)")
    decompositions: list[HelperDecomposition] = Field(description="The monolithic-function decompositions to expose (may be empty)")

    def api_lines(self) -> list[str]:
        """Prompt-facing one-liners describing the harness API, for downstream
        property-generation/judge prompts."""
        lines: list[str] = []
        for s in self.unstructured_slots:
            lines.append(
                f"`{s.getter_name}()` returns (`{s.value_type}`) — reads the storage slot "
                f"derived from {s.slot_derivation}. {s.rationale}"
            )
        for d in self.decompositions:
            for (name, contract) in d.helpers.items():
                lines.append(
                    f"`{name}` — {contract} (thin external wrapper over a step of `{d.target_function}`)"
                )
        return lines

def empty_main_harness_plan_error(plan: MainHarnessPlan | None) -> str | None:
    """The classifier must deliver ``null`` rather than an all-empty plan.
    Module-level (not inlined in the agent's result validator) so the rejection
    is unit-testable without running the agent."""
    if plan is not None and \
            not plan.unstructured_slots and \
            not plan.decompositions:
        return "main_contract_harness was proposed but contains no getters and no decompositions; deliver null instead"
    return None

def main_harness_path_error(path: str) -> str | None:
    """The delivered harness must live under ``certora/harnesses/``. The VFS
    write confinement only blocks *writes* outside that directory; without this
    check the agent could deliver a pre-existing project file (e.g. a protocol
    source) as the "generated" harness. Module-level for unit-testability."""
    if not path.startswith("certora/harnesses/"):
        return f"Delivered harness at {path}, but the harness must be a new file under `certora/harnesses/`"
    return None

class MainHarnessView(BaseModel):
    """Prompt-facing view of the main-contract augmentation harness, rendered by
    `harnessed_application_context.j2` so downstream agents see the harness API."""
    name: SolidityIdentifier
    path: str
    harness_of: SolidityIdentifier
    api: list[str]

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


class AgentSystemDescription(SystemDescriptionBase[ClosureContract]):
    """
    The result of your analysis
    """
    # Optional-with-default so cached pre-change analyses still validate.
    main_contract_harness: MainHarnessPlan | None = Field(default=None, description=(
        "The main-contract augmentation harness plan, if the main contract has "
        "unstructured storage without getters and/or monolithic external functions "
        "worth decomposing (null otherwise)"
    ))

    def needs_harnessing(self) -> bool:
        # Only gates the *external* N-instance harnessing; the main-contract
        # augmentation harness is generated separately (see generate_main_harness).
        return any([
            c.num_instances for c in self.transitive_closure
        ])

class LocatedClosureContract(ClosureContract):
    path: str

class LocatedSystemDescription(SystemDescriptionBase[LocatedClosureContract]):
    pass

class SystemDescriptionHarnessed(SystemDescriptionBase[HarnessedContract]):
    # Both optional-with-default so cached pre-change descriptions still validate.
    # ``main_harness`` is the generated augmentation harness (the verify target when
    # present); ``main_harness_plan`` is the analysis plan it was generated from,
    # kept so downstream prompts can describe the harness API.
    main_harness: HarnessedContract | None = None
    main_harness_plan: MainHarnessPlan | None = None

    def verify_contract_name(self, default_name: str) -> str:
        """The contract identifier the prover verifies: the main-harness identifier
        when an augmentation harness exists, ``default_name`` otherwise."""
        if self.main_harness is not None:
            return self.main_harness.solidity_identifier
        return default_name

    def verify_contract_path(self, default_path: str) -> str:
        """The source file the prover verifies, following ``verify_contract_name``."""
        if self.main_harness is not None:
            return self.main_harness.path
        return default_path

    def main_harness_api(self) -> list[str] | None:
        """Prompt-facing one-liners for the harness API (None when no harness)."""
        if self.main_harness is None or self.main_harness_plan is None:
            return None
        return self.main_harness_plan.api_lines()

    def main_harness_view(self) -> MainHarnessView | None:
        if self.main_harness is None or self.main_harness.harness_definition is None:
            return None
        return MainHarnessView(
            name=self.main_harness.solidity_identifier,
            path=self.main_harness.path,
            harness_of=self.main_harness.harness_definition.harness_of,
            api=self.main_harness_api() or [],
        )

class HarnessAnalysisParams(TypedDict):
    contract_name: str
    relative_path: str
    context: SourceApplication


class ContractSetup(BaseModel):
    system_description: SystemDescriptionHarnessed
    config: SetupSuccess

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
        if (plan_error := empty_main_harness_plan_error(res.main_contract_harness)) is not None:
            return plan_error
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
    solidity_compiler: str = Field(description=f"The solidity compiler to use for compiling these harnesses.")

class HarnessResult(BaseModel):
    identifier_to_source: dict[SolidityIdentifier, list[GeneratedHarnessSource]]

class HarnessInput(BaseModel):
    path: str
    n_harnesses: int
    solidity_identifier: SolidityIdentifier

class HarnessGenParams(TypedDict):
    to_harness: list[HarnessInput]

_HarnessGenerationPrompt = TypedTemplate[HarnessGenParams]("harness_generation_prompt.j2")

def harness_generation_key(
    instructions: AgentSystemDescription
) -> CacheKey[SystemDescriptionHarnessed, HarnessResult]:
    return CacheKey[SystemDescriptionHarnessed, HarnessResult](string_hash(instructions.model_dump_json()))

async def generate_harnesses(
    context: WorkflowContext[SystemDescriptionHarnessed],
    env: ServiceHost,
    source: SourceCode,
    application: SourceApplication,
    instructions: AgentSystemDescription
) -> HarnessResult:
    child = await context.child(harness_generation_key(instructions), instructions.model_dump())
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


    def result_validator(
        s: GenerationState,
        res: HarnessAgentResult,
        tid: str
    ) -> str | None:
        check_copy = expected.copy()
        all_files = [
            
        ]
        for (nm, r) in res.identifier_to_source.items():
            if nm not in check_copy:
                return f"Delivered result for contract {nm}, but no instructions were given to harness it"
            if len(r) != check_copy[nm]:
                return f"Delivered {len(r)} harnesses for {nm}, but {check_copy[nm]} were required"
            for res_c in r:
                if mat.get(s, res_c.path) is None:
                    return f"Delivered harness {res_c.harness_name} at {res_c.path} for {nm}, but it doesn't exist on the VFS"
                all_files.append(res_c.path)
            del check_copy[nm]
        if len(check_copy) != 0:
            error = ", ".join(
                [ f"contract {k} ({n} copies)" for (k,n) in check_copy.items() ]
            )
            return f"Missing harnesses in results: {error}"
        if False: # this doesn't work
            with mat.materialize(s) as temp_dir:
                compile_result = subprocess.run(
                    [res.solidity_compiler] + all_files,
                    cwd=temp_dir,
                    capture_output=True,
                    text=True
                )
                if compile_result.returncode != 0 and False:
                    return f"Harness compilation failed:\nstdout:\n{compile_result.stdout}\nstderr:\n{compile_result.stderr}"
        return None

    result_tool = result_tool_generator(
        "result",
        HarnessAgentResult,
        "Signal the completion of your workflow",
        validator=(GenerationState, result_validator)
    )

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
        v_tools + [result_tool]
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

class MainHarnessAgentResult(BaseModel):
    """
    The result of your main-contract harness generation
    """
    path: str = Field(description="The relative path to the generated harness file")
    harness_name: SolidityIdentifier = Field(description="The Solidity identifier of the harness contract defined in the file")

class MainHarnessResult(MainHarnessAgentResult):
    source: str

class MainHarnessGenParams(TypedDict):
    contract_name: str
    relative_path: str
    harness_name: str
    plan: MainHarnessPlan

_MainHarnessGenerationPrompt = TypedTemplate[MainHarnessGenParams]("main_harness_generation_prompt.j2")

def main_harness_generation_key(
    plan: MainHarnessPlan,
    contract_name: str,
) -> CacheKey[SystemDescriptionHarnessed, MainHarnessResult]:
    return CacheKey[SystemDescriptionHarnessed, MainHarnessResult](
        "main-harness-" + string_hash(plan.model_dump_json() + "\x00" + contract_name)
    )

async def generate_main_harness(
    context: WorkflowContext[SystemDescriptionHarnessed],
    env: ServiceHost,
    source: SourceCode,
    application: SourceApplication,
    plan: MainHarnessPlan
) -> MainHarnessResult:
    """Generate the main-contract augmentation harness `<Main>Harness is <Main>`:
    external view getters for unstructured storage plus thin external wrappers
    decomposing monolithic functions. Same VFS confinement as
    ``generate_harnesses`` (writes only under ``certora/harnesses``)."""
    child = await context.child(
        main_harness_generation_key(plan, source.contract_name), plan.model_dump()
    )
    if (cached := await child.cache_get(MainHarnessResult)) is not None:
        return cached

    tool_conf = VFSToolConfig(
        fs_layer=source.project_root,
        immutable=False,
        put_doc_extra="You may only write into the `certora/harnesses` directory",
        forbidden_write="^(?!certora/harnesses)",
        forbidden_read=source.forbidden_read
    )

    class GenerationState(MessagesState, VFSState):
        result: NotRequired[MainHarnessAgentResult]

    class GenerationInput(FlowInput, VFSState):
        pass

    v_tools, mat = vfs_tools(tool_conf, GenerationState)

    expected_name = f"{source.contract_name}Harness"

    bound_template = _MainHarnessGenerationPrompt.bind({
        "contract_name": source.contract_name,
        "relative_path": source.relative_path,
        "harness_name": expected_name,
        "plan": plan
    })

    def result_validator(
        s: GenerationState,
        res: MainHarnessAgentResult,
        tid: str
    ) -> str | None:
        if res.harness_name != expected_name:
            return f"Harness contract must be named {expected_name}, got {res.harness_name}"
        if (path_error := main_harness_path_error(res.path)) is not None:
            return path_error
        if mat.get(s, res.path) is None:
            return f"Delivered harness {res.harness_name} at {res.path}, but it doesn't exist on the VFS"
        return None

    result_tool = result_tool_generator(
        "result",
        MainHarnessAgentResult,
        "Signal the completion of your workflow",
        validator=(GenerationState, result_validator)
    )

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
        v_tools + [result_tool]
    ).with_default_summarizer().compile_async()

    res_state = await run_to_completion(
        graph=g,
        input=GenerationInput(input=[], vfs={}),
        context=None,
        description="Main Harness Generation",
        recursion_limit=child.recursion_limit,
        thread_id=child.thread_id
    )

    assert "result" in res_state
    gen = res_state["result"]
    source_code = mat.get(res_state, gen.path)
    assert source_code is not None, gen.path
    to_ret = MainHarnessResult(
        path=gen.path,
        harness_name=gen.harness_name,
        source=source_code.decode("utf-8")
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
    setup_ctx = await context.child(system_setup_key(application_desc), application_desc.model_dump())
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

    if analysis_results.main_contract_harness is not None:
        main_harness = await generate_main_harness(
            context=setup_ctx,
            env=env,
            source=source,
            application=application_desc,
            plan=analysis_results.main_contract_harness
        )
        # The harness extends the main contract, so it carries the main contract's
        # (already harness-patched) link fields.
        main_links = next(
            (c.link_fields for c in harnessed_system.transitive_closure
             if c.solidity_identifier == source.contract_name),
            []
        )
        harnessed_system.main_harness = HarnessedContract(
            solidity_identifier=main_harness.harness_name,
            link_fields=main_links,
            harness_definition=HarnessDef(
                harness_of=source.contract_name,
                harness_source=main_harness.source,
            ),
            path=main_harness.path
        )
        harnessed_system.main_harness_plan = analysis_results.main_contract_harness

    await setup_ctx.cache_put(harnessed_system)
    return harnessed_system

async def run_and_apply_part1(
    context: WorkflowContext[ContractSetup],
    source: SourceCode,
    env: ServiceHost,
    application_desc: SourceApplication
) -> SystemDescriptionHarnessed:
    res = await run_setup_part1(context, source, env, application_desc)
    to_write = list(res.transitive_closure)
    if res.main_harness is not None:
        to_write.append(res.main_harness)
    for c in to_write:
        if c.harness_definition is not None:
            tgt = Path(source.project_root) / c.path
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(c.harness_definition.harness_source)
    return res

config_key = CacheKey[None, ContractSetup]("config")

from logging import getLogger
_logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Split phases.
#
# Harness creation and AutoSetup are exposed as two separate, independently
# cached steps so the pipeline can run AutoSetup in parallel with invariant/bug
# analysis. They share the ``config_key`` parent context, so existing
# harness-creation caches (keyed by ``system_setup_key``) still hit; the
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


def autosetup_key(
    app: SourceApplication,
    prover_opts: ProverOptions,
    verify_contract: str | None = None,
) -> CacheKey[ContractSetup, SetupSuccess]:
    """Cache key for the AutoSetup phase. Includes ``prover_opts`` so cloud and
    local configurations never collide (the old composite ``config_key`` omitted
    them, which could reuse a stale config across modes)."""
    payload = app.model_dump_json() + "\x00" + "\x00".join(prover_opts.extra_args)
    # The verified contract participates in the key only when it deviates from
    # the default (a main-contract augmentation harness), so cache entries from
    # before harness support stay valid for harness-free runs.
    if verify_contract is not None:
        payload += "\x00verify=" + verify_contract
    return CacheKey[ContractSetup, SetupSuccess](
        "autosetup-" + string_hash(payload)
    )


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
    # When a main-contract augmentation harness exists, the harness *is* the
    # verified contract: it becomes AutoSetup's target (the original main
    # contract enters the scene through inheritance, not as its own instance).
    verify_harness = (
        sys_desc.main_harness.solidity_identifier if sys_desc.main_harness is not None else None
    )
    config_ctxt = context.child(config_key)
    cache = await config_ctxt.child(
        autosetup_key(application_desc, prover_opts, verify_harness),
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
        sys_desc.verify_contract_path(source.relative_path),
        sys_desc.verify_contract_name(source.contract_name),
        prover_opts,
        *extra_files,
    )

    if isinstance(setup_result, SetupFailure):
        raise RuntimeError(f"Auto setup failed: {setup_result.error}\nProc stderr:\n{setup_result.stderr}")

    # AutoSetup runs as a subprocess; its LLM token usage never reaches composer's
    # UsageCallback. Fold the counts it wrote to disk into the run summary so they
    # land in token_usage.json, the run tag, and the end-of-run table. No task_id:
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

