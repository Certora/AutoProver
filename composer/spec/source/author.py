from typing import NotRequired, override, Literal, Annotated
from typing_extensions import TypedDict
import json

from langchain_core.tools import BaseTool
from pydantic import Field, BaseModel, Discriminator

from graphcore.tools.schemas import (
    WithAsyncDependencies, WithAsyncImplementation, WithImplementation, WithInjectedId,
    WithInjectedState,
)
from graphcore.graph import tool_state_update
from graphcore.summary import SummaryConfig

from composer.spec.cvl_generation import (
    static_tools, CVLGenerationExtra, FeedbackToolContext, FEEDBACK_VALIDATION_KEY,
    check_completion, validate_property_rules, CVL_JUDGE_KEY, run_cvl_generator,
    GeneratedCVL, PropertyRuleMapping
)
from composer.spec.context import WorkflowContext, CVLGeneration, SourceCode
from composer.spec.prop import PropertyFormulation
from composer.spec.system_model import ContractComponentInstance, SolidityIdentifier
from composer.spec.source.prover import ProverStateExtra, DELETE_SKIP, VALIDATION_KEY as PROVER_VALIDATION_KEY
from langgraph.graph import MessagesState
from langgraph.runtime import get_runtime
from pathlib import Path, PurePosixPath
from composer.spec.gen_types import CVLResource, TypedTemplate, import_statement_for
from composer.spec.service_host import ServiceHost
from composer.workflow.services import CacheLevel

from langgraph.types import Command
from composer.spec.feedback import property_feedback_judge, FeedbackTemplate
from composer.ui.tool_display import tool_display

from graphcore.graph import FlowInput

class SourceAuthorExtra(TypedDict):
    failed: bool | None

class SourceCVLGenerationExtra(CVLGenerationExtra, ProverStateExtra, SourceAuthorExtra):
    pass

class SourceCVLGenerationInput(SourceCVLGenerationExtra, FlowInput):
    pass

class SourceCVLGenerationState(SourceCVLGenerationExtra, MessagesState):
    result: NotRequired[str]

class GaveUp(BaseModel):
    reason: str

type BatchGeneratedCVLResult = GeneratedCVL | GaveUp

@tool_display(lambda p: f"Expecting rule `{p['rule_name']}` to fail", None)
class ExpectRuleFailure(WithAsyncImplementation[Command], WithInjectedId):
    """
    Mark a rule name as expected to fail.
    """
    rule_name: str = Field(description="The name of the rule")
    reason: str = Field(description="The reason the rule is expected to fail")

    @override
    async def run(self) -> Command:
        return tool_state_update(
            tool_call_id=self.tool_call_id,
            content="Success",
            rule_skips={
                self.rule_name: self.reason
            }
        )
@tool_display(
    lambda p: f"Expecting rule `{p["rule_name"]}` to pass", None
)
class ExpectRulePassage(WithAsyncImplementation[Command], WithInjectedId):
    """
    Unmark a rule as expected to fail. By default all rules/invariants are expected to pass,
    so this should only be called to revert a prior call to `expect_rule_failure`.
    """
    rule_name : str = Field(description="The name of the rule that was previously marked as expected to fail that is now expected to pass")

    @override
    async def run(self) -> Command:
        return tool_state_update(
            tool_call_id=self.tool_call_id,
            content="Success",
            rule_skips={
                self.rule_name: DELETE_SKIP
            }
        )

@tool_display(
    label=lambda p: "Publishing CVL result",
    result=None,
)
class PublishResultTool(
    WithImplementation[Command | str],
    WithInjectedState[SourceCVLGenerationState],
    WithInjectedId,
):
    """
    Call to signal your completed cvl generation.
    """
    commentary: str = Field(description="Commentary on your generated spec")
    property_rules: list[PropertyRuleMapping] = Field(
        description="The property->rules mapping. For every property you did NOT skip "
        "(referenced by its unique snake_case title from the batch listing), list the "
        "name(s) of the rule(s)/invariant(s) in your spec that verify it. Every non-skipped "
        "property must appear with at least one rule."
    )

    @override
    def run(self) -> Command | str:
        if (err := check_completion(self.state)) is not None:
            return err
        titles = get_runtime(FeedbackToolContext).context.titles
        if (err := validate_property_rules(self.property_rules, self.state["skipped"], titles)) is not None:
            return err
        return tool_state_update(
            self.tool_call_id,
            "Accepted",
            result=self.commentary,
            property_rules=self.property_rules,
            failed=False,
        )


@tool_display(
    label=lambda p: f"Giving up on CVL generation: {p['reason']}",
    result=None,
)
class GiveUpTool(WithImplementation[Command], WithInjectedId):
    """
    Call this tool to give up on the CVL generation for this task.

    This should only ever be called as a LAST RESORT when you have exhausted all other
    mechanisms to complete your task.
    """
    reason: str = Field(description="The reason for giving up on your task")

    @override
    def run(self) -> Command:
        return tool_state_update(
            self.tool_call_id,
            "Accepted",
            failed=True,
            result=self.reason,
        )

class ResourceView(TypedDict):
    """A CVLResource prepared for the prompt: ``import_path`` is the CVL import
    string relative to the generated spec's directory (``certora/specs/``)."""
    description: str
    required: bool
    import_path: str

class PropertyGenParams(TypedDict):
    context: ContractComponentInstance | None
    resources: list[ResourceView]
    properties: list[PropertyFormulation]
    contract_name: str

class PropertyGenerationConfig(SummaryConfig[SourceCVLGenerationState]):
    def __init__(self):
        super().__init__()

    @override
    def get_summarization_prompt(self, state: SourceCVLGenerationState) -> str:
            return """
You are approaching the context limit for your task. After this point, your context will be cleared
and the task restarted from the initial prompt.

To enable you to continue to work effectively after this compaction, summarize the current state of your task. In particular, summarize:
1. Any key findings about CVL you received from the CVL researcher or your own research
2. The current state of your task, including:
   a. What properties have been formalized
   b. What properties you have skipped, and why
   c. What properties have been accepted by the feedback tool.
   d. What rules you have chosen to mark as failing, and why
3. If you have any outstanding, unaddressed feedback from your last iteration with the feedback tool, include that unaddressed feedback in your summary
4. If you have any outstanding, unaddressed tasks from the most recent iteration with the prover, include those unaddressed tasks in your summary
5. Any techniques/attempts that you or the feedback rejected or didn't work
6. Any techniques/attempts that you attempted but were rejected by the prover

In other words, your summary should include all information necessary to prevent the next iteration on this task from repeating work
or repeating mistakes.

If your current task itself began with a summary, include the salient parts of that summary in your new summary.
"""

    @override
    def get_resume_prompt(self, state: SourceCVLGenerationState, summary: str) -> str:
        return f"""
You are resuming this task already in progress. The current version of your spec (if any) is available via the `get_cvl` tool.

A summary of your work up until this point is as follows:

BEGIN SUMMARY:
{summary}

END SUMMARY

**IMPORTANT**: Absolutely *nothing* has changed since the summary was produced and now. You do *NOT* need to reverify
any information about CVL present in your summary unless you discovery something *new* with necessitates revisiting those conclusions.
If you have outstanding feedback to address, you do *NOT* need to re-invoke the feedback tool; proceed immediately with addressing
that feedback.
"""

class AddFile(BaseModel):
    """
    Add a new file to the input of the prover. If the Solidity identifier of the contract within the file does *NOT* match the file stem,
    specify it explicitly, otherwise leave it null.
    """
    type: Literal["add_file"]
    file_path: str = Field(description="The relative path to the file to include in the prover inputs")
    contract_name: SolidityIdentifier | None = Field(description="The Solidity identifier of the contract within `file_path` to ingest into the prover, if it does not match the file stem")

class RemoveFile(BaseModel):
    """
    Remove a file from the prover inputs. If the file is specified in the form `path/to/Contract.sol:Something`
    provide *only* the file path portion, i.e., `path/to/Contract.sol`
    """
    type: Literal["remove_file"]
    path_to_remove: str = Field(description="The path to the file to remove from prover inputs")

class AddLink(BaseModel):
    """
    Add a link from one contract to another via a storage field.

    For example, if contract A has a *top-level* storage field
    `rewardToken` that points to the instance of `B` you should register the link
    (A, rewardToken, B).

    NB that the link field *must* be at the top-level of the contract's storage. Link flags cannot be used
    to link fields in structs.
    """
    type: Literal["add_link"]
    source_contract_name: SolidityIdentifier = Field(description="The Solidity identifier of the contract that is the source of the link")
    link_field_name: str = Field(description="The storage field holding the link within `source_contract_name`")
    target_contract_name : SolidityIdentifier = Field(description="The Solidity identifier of the contract held in `link_field_name` of `source_contract_name`")

class RemoveLink(BaseModel):
    """
    Remove a link from one contract to another.
    """
    type: Literal["remove_link"]
    source_contract_name : SolidityIdentifier = Field(description="The Solidity identifier of the contract whose link should be removed")
    link_field_name : str = Field(description="The storage field holding the link within `source_contract_name` that should be removed")

class SetProverFlag(BaseModel):
    """
    Set a whitelisted top-level prover flag in the configuration:

    * `optimistic_fallback` (bool): assume unresolved low-level calls (`.call{value: ...}`, `send`,
      `transfer`) succeed instead of HAVOCing and always being able to fail. Step 3 of the vacuity
      repair ladder.
    * `loop_iter` (int, 1-8): the number of loop unrollings the prover performs.
    * `global_timeout` (int, 1-7200): the overall run timeout in seconds.
    * `contract_recursion_limit` (int, 0-10): the allowed depth of recursive calls between contracts.
    """
    # `rule_sanity` and `optimistic_loop` are DELIBERATELY not in this whitelist: vacuity
    # detection (composer/prover/vacuity.py) depends on `rule_sanity: basic`, and
    # `prover_config_overlay` sets both AFTER spreading the base config so they would win
    # anyway — the whitelist keeps the agent from even trying (and from being confused by a
    # silently-overridden edit).
    type: Literal["set_flag"]
    flag: Literal["optimistic_fallback", "loop_iter", "global_timeout", "contract_recursion_limit"]
    value: bool | int = Field(description="The value to set: a bool for `optimistic_fallback`, an int for the other flags")

def _validate_prover_flag(flag: str, value: bool | int) -> str | None:
    """Per-flag validation for SetProverFlag; returns an error message or None if valid.

    NB: ``bool`` is a subclass of ``int``, so the bool checks must come before any int check.
    """
    if flag == "optimistic_fallback":
        if not isinstance(value, bool):
            return f"optimistic_fallback expects a boolean value, got {value!r}"
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{flag} expects an integer value, got {value!r}"
    match flag:
        case "loop_iter":
            if not (1 <= value <= 8):
                return f"loop_iter must be between 1 and 8, got {value}"
        case "global_timeout":
            if not (1 <= value <= 7200):
                return f"global_timeout must be between 1 and 7200 (seconds), got {value}"
        case "contract_recursion_limit":
            if not (0 <= value <= 10):
                return f"contract_recursion_limit must be between 0 and 10, got {value}"
    return None

type ConfigEdit = Annotated[RemoveLink | AddLink | AddFile | RemoveFile | SetProverFlag, Discriminator("type")]

@tool_display(
    lambda p: f"Editing prover configuration ({len(p['edits'])} edit(s))", None
)
class ConfigEditTool(WithAsyncImplementation[Command | str], WithInjectedId, WithInjectedState[ProverStateExtra]):
    """
    Call this tool to make a edits to the prover configuration.

    Each individual edit is applied in some sequence; if the edits conflict with one another the result is undefined.
    The configuration change is atomic: if any of the edits fail to apply the configuration will remain unchanged,
    and the issue will be returned. Otherwise, the updated configuration is returned as the result of this call.
    """
    edits: list[ConfigEdit] = Field(
        description="A list of the atomic edits to make to the file."
    )

    def _parse_link(self, l) -> tuple[str, str, str]:
        base = l.split("=", 1)
        assert len(base) == 2, l
        contract_and_field = base[0].split(":", 1)
        assert len(contract_and_field), base[0]
        return (contract_and_field[0], contract_and_field[1], base[1])

    @override
    async def run(self) -> Command | str:
        curr_config = self.state["config"].copy()
        for ed in self.edits:
            match ed:
                case RemoveFile(path_to_remove=to_remove):
                    assert "files" in curr_config
                    new_files = []
                    found = False
                    for (ind, f) in enumerate(curr_config["files"]):
                        if f.startswith(to_remove):
                            new_files.extend(curr_config["files"][ind+1:])
                            found = True
                            break
                        new_files.append(f)
                    if not found:
                        return f"Path {to_remove} doesn't seem to appear in {"\n".join(curr_config["files"])}"
                    curr_config["files"] = new_files
                case AddFile(file_path=to_add, contract_name=explicit_name):
                    assert "files" in curr_config
                    if any([ x.startswith(to_add) for x in curr_config["files"] ]):
                        return f"Path {to_add} already appears in prover inputs"
                    new_files = curr_config["files"].copy()
                    if explicit_name is not None:
                        to_add += f":{explicit_name}"
                    new_files.append(
                        to_add
                    )
                    curr_config["files"] = new_files
                case AddLink(source_contract_name=src, link_field_name=fld, target_contract_name=tgt):
                    if ".sol" in src or ".sol" in tgt:
                        return ".sol extension found in source/dest of AddLink; did you accidentally provide a filename?"
                    if "link" in curr_config:
                        curr_link : list[str] = curr_config["link"]
                        for l in curr_link:
                            (curr_src, curr_fld, curr_dst) = self._parse_link(l)
                            if curr_src == src and curr_fld == fld:
                                return f"Link for field {fld} in contract {src} already exists -> {curr_dst}"
                    new_links = list(curr_config.get("link", []))
                    new_links.append(f"{src}:{fld}={tgt}")
                    curr_config["link"] = new_links
                case RemoveLink(source_contract_name=src, link_field_name=fld):
                    if "link" not in curr_config:
                        return "No links configured, nothing to remove"
                    new_links = []
                    found = False
                    curr_links = curr_config["link"]
                    for (i, l) in enumerate(curr_links):
                        (curr_src, curr_fld, _) = self._parse_link(l)
                        if curr_src == src and curr_fld == fld:
                            new_links.extend(curr_links[i+1:])
                            found = True
                            break
                    if not found:
                        return f"No existing link found that matches {src}:{fld}"
                    curr_config["link"] = new_links
                case SetProverFlag(flag=flag, value=value):
                    if (err := _validate_prover_flag(flag, value)) is not None:
                        return err
                    curr_config[flag] = value

        return tool_state_update(
            self.tool_call_id,
            f"Accepted, new config is:\n```json\n{json.dumps(curr_config, indent=2)}\n```",
            config=curr_config
        )


# The only directory write_mock may write into, mirroring the harness generator's VFS
# confinement (`forbidden_write="^(?!certora/harnesses)"` in harness.py) — but enforced
# directly here because the authoring agent's source tools are read-only over the real
# project tree and the prover compiles from the real tree, so the mock must land on disk.
MOCKS_DIR = PurePosixPath("certora/mocks")


@tool_display(lambda p: f"Writing mock `{p['file_path']}`", None)
class WriteMockTool(WithAsyncDependencies[str, str]):
    """
    Write a minimal Solidity mock contract under `certora/mocks` implementing an interface, so
    the prover can resolve calls to an otherwise-unresolved callee (step 2 of the vacuity repair
    ladder). After writing the mock, register it in the prover configuration with `edit_config`
    (an `add_file` edit, plus an `add_link` edit if the callee is reached through a storage field).

    Keep the mock as small as possible: implement only the interface the caller needs, but
    preserve the semantics that matter to the calling contract — in particular a `payable`
    callee must remain `payable`. Writing to an existing path overwrites it, so you can iterate
    on a mock in place.
    """
    file_path: str = Field(description=f"Project-root-relative path for the mock, under `{MOCKS_DIR}` (e.g. `{MOCKS_DIR}/MockOracle.sol`)")
    content: str = Field(description="The full Solidity source of the mock contract")

    @override
    async def run(self) -> str:
        rel = PurePosixPath(self.file_path)
        if rel.is_absolute() or ".." in rel.parts:
            return f"Invalid path `{self.file_path}`: must be a project-root-relative path without `..`"
        if rel.parts[:len(MOCKS_DIR.parts)] != MOCKS_DIR.parts or len(rel.parts) <= len(MOCKS_DIR.parts):
            return f"You may only write into the `{MOCKS_DIR}` directory (got `{self.file_path}`)"
        if rel.suffix != ".sol":
            return f"Mock must be a Solidity source file (`.sol`), got `{self.file_path}`"
        with self.tool_deps() as project_root:
            target = Path(project_root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.content)
        return (
            f"Wrote mock to `{self.file_path}`. Remember to register it in the prover configuration "
            "via `edit_config` (`add_file`, plus `add_link` if the callee is reached through a storage field)."
        )


_PropertyGenTemplate = TypedTemplate[PropertyGenParams]("property_generation_prompt.j2")

async def batch_cvl_generation(
    ctx: WorkflowContext[CVLGeneration],
    init_config: dict,
    props: list[PropertyFormulation],
    component: ContractComponentInstance | None,
    resources: list[CVLResource],
    prover_tool: BaseTool,
    env: ServiceHost,
    description: str,
    source: SourceCode,
    spec_dir: Path,
) -> BatchGeneratedCVLResult:
    # *spec_dir* (project-root-relative) is where the caller will persist the spec
    # authored here. The prover resolves the spec's CVL imports relative to its own
    # directory, so resource imports are expressed relative to *spec_dir*.
    resource_views: list[ResourceView] = [
        {
            "description": r.description,
            "required": r.required,
            "import_path": import_statement_for(r.path, spec_dir),
        }
        for r in resources
    ]
    bound_template = _PropertyGenTemplate.bind({
        "resources": resource_views,
        "context": component,
        "properties": props,
        "contract_name": source.contract_name
    })

    # use "cache=long" to account for very long prover runs.
    # on anthropic (the only backend we support) a long cache is 1hr
    # NB that on longer prover runs we'll still get a cache miss;
    # this is a trade off we may have to revisit later.
    task_graph = env.builder_heavy(cache_level=CacheLevel.LONG).with_tools(
        env.all_tools
    ).with_tools(
        static_tools()
    ).with_tools(
        [
            prover_tool, ExpectRulePassage.as_tool("expect_rule_passage"), ExpectRuleFailure.as_tool("expect_rule_failure"),
            GiveUpTool.as_tool("give_up"), PublishResultTool.as_tool("result"),
            # Setup-repair tools for the vacuity repair ladder: edit_config covers ladder
            # steps 2 (register a mock via add_file/add_link) and 3 (optimistic_fallback via
            # set_flag); write_mock produces the step-2 mock itself.
            ConfigEditTool.as_tool("edit_config"),
            WriteMockTool.bind(source.project_root).as_tool("write_mock"),
            ctx.get_memory_tool(),
        ]
    ).with_state(
        SourceCVLGenerationState
    ).with_output_key(
        "result"
    ).with_input(
        SourceCVLGenerationInput
    ).with_context(
        FeedbackToolContext
    ).with_sys_prompt_template(
        "property_generation_system_prompt.j2"
    ).inject(
        lambda d: bound_template.render_to(d.with_initial_prompt_template)
    ).with_summary_config(PropertyGenerationConfig()).compile_async()

    feedback_env = property_feedback_judge(
        ctx.child(CVL_JUDGE_KEY), env, FeedbackTemplate.bind({
            "sort": "existing",
            "context": component
        }), props
    )

    res_state = await run_cvl_generator(
        ctx = ctx,
        d = task_graph,
        description=description,
        ctxt=feedback_env,
        in_state=SourceCVLGenerationInput(
            curr_spec=None,
            config=init_config,
            input=[],
            required_validations=[FEEDBACK_VALIDATION_KEY, PROVER_VALIDATION_KEY],
            rule_skips={},
            vacuous_methods={},
            skipped=[],
            property_rules=[],
            validations={},
            failed=None,
        )
    )

    assert "result" in res_state
    assert res_state["failed"] is not None
    if res_state["failed"]:
        return GaveUp(reason=res_state["result"])
    d = res_state["curr_spec"]
    assert d is not None
    # Persist the base prover config and last run link from the final state so a later cache
    # hit (which skips the prover) can still reconstruct certora/confs and retain the link.
    return GeneratedCVL(
        commentary=res_state["result"],
        cvl=d,
        skipped=res_state["skipped"],
        property_rules=res_state["property_rules"],
        config=res_state["config"],
        final_link=res_state.get("prover_link"),
    )

