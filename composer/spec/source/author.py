from typing import NotRequired, Sequence, override, Literal, Annotated
from typing_extensions import TypedDict
import json

from dataclasses import dataclass

from langchain_core.tools import BaseTool
from pydantic import Field, BaseModel, Discriminator

from graphcore.tools.schemas import (
    WithAsyncImplementation, WithImplementation, WithInjectedId, WithInjectedState,
    WithAsyncDependencies
)
from graphcore.tools.vfs import VFSAccessor, VFSState
from graphcore.graph import tool_state_update
from graphcore.summary import SummaryConfig

from composer.spec.cvl_generation import (
    static_tools, property_tools, skip_tools, CVLGenerationExtra, FEEDBACK_VALIDATION_KEY,
    check_completion, validate_property_rules, CVL_JUDGE_KEY, run_cvl_generator,
    GeneratedCVL, PropertyRuleMapping, AppliedEdit, FeedbackToolBase, SkippedProperty,
    PropertyFeedbackProtocol,
)
from composer.spec.source.live_explorer import VersionedHistory, LiveEditTools, WIPE_HISTORY
from composer.spec.context import WorkflowContext, CVLGeneration, CacheKey, SourceCode
from composer.spec.types import PropertyFormulation
from composer.pipeline.core import GaveUp
from composer.spec.system_model import ContractComponentInstance, SolidityIdentifier, component_context
from composer.spec.source.prover import (
    OVERLAY_OWNED_KEYS, ProverStateExtra, DELETE_SKIP, VALIDATION_KEY as PROVER_VALIDATION_KEY,
)
from langgraph.graph import MessagesState
from pathlib import Path
from composer.spec.gen_types import CVLResource, TypedTemplate, import_statement_for
from composer.spec.service_host import ServiceHost, Sort
from composer.llm.provider import CacheLevel
from composer.pipeline.ptypes import GaveUp

from langgraph.types import Command
from graphcore.graph import Builder
from composer.spec.feedback import (
    property_feedback_judge, source_feedback_judge, FeedbackTemplate, Properties,
    SourceSnapshot, ContextualFeedbackToolImpl,
)
from composer.ui.tool_display import tool_display

from composer.spec.source.conf_maps import (
    CompilerSettings, MapViolations, extend_compiler_maps, map_violation_message,
)
from composer.spec.source.munge.edit_store import EditStore
from composer.spec.source.munge.munge_agent import editor_tool
from composer.spec.source.munge.tool_names import (
    COMMIT_EDIT, CONFIG_EDIT, EDIT_HISTORY_LOG, REVERT_TO_EDIT,
)
from composer.spec.source.munge.vfs_diff import summarize_changes

from graphcore.graph import FlowInput

class SourceAuthorExtra(TypedDict):
    failed: bool | None

# ``vfs`` comes from ProverStateExtra (NotRequired, no merge op — replaced
# wholesale by commit_edit / revert_to_edit); the generation input always
# seeds it explicitly.
class SourceCVLGenerationExtra(CVLGenerationExtra, ProverStateExtra, SourceAuthorExtra, VersionedHistory):
    pass

class SourceCVLGenerationInput(SourceCVLGenerationExtra, FlowInput):
    pass

class SourceCVLGenerationState(SourceCVLGenerationExtra, MessagesState):
    result: NotRequired[str]

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
    WithAsyncDependencies[Command | str, list[str]],
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
    async def run(self) -> Command | str:
        if (err := check_completion(self.state, self.state["version_history"])) is not None:
            return err
        with self.tool_deps() as titles:
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

@component_context
class PropertyGenParams(TypedDict):
    sort: Literal["existing"]
    context: ContractComponentInstance | None
    resources: list[ResourceView]
    properties: list[PropertyFormulation]
    contract_name: str

class PropertyGenerationConfig(SummaryConfig[SourceCVLGenerationState]):
    def __init__(self, source_editing: bool = False):
        super().__init__()
        self._source_editing = source_editing

    @override
    def get_summarization_prompt(self, state: SourceCVLGenerationState) -> str:
            edit_item = (
                "\n7. The source edits you have applied (their edit ids and what each was for), "
                "any edit ids the editor produced that you chose NOT to apply, and any plans "
                "you had to request further edits"
                if self._source_editing else ""
            )
            return f"""
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
6. Any techniques/attempts that you attempted but were rejected by the prover{edit_item}

In other words, your summary should include all information necessary to prevent the next iteration on this task from repeating work
or repeating mistakes.

If your current task itself began with a summary, include the salient parts of that summary in your new summary.
"""

    @override
    def get_resume_prompt(self, state: SourceCVLGenerationState, summary: str) -> str:
        edit_note = (
            "\nAny source edits you applied remain in effect on your working copy; "
            f"the `{EDIT_HISTORY_LOG}` tool shows each applied edit and its diff.\n"
            if self._source_editing else ""
        )
        return f"""
You are resuming this task already in progress. The current version of your spec (if any) is available via the `get_cvl` tool.
{edit_note}
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
    contract_name: SolidityIdentifier | None = Field(
        description="The Solidity identifier of the contract within `file_path`" \
        "to ingest into the prover, if it does not match the file stem")
    compiler_settings: CompilerSettings | None = Field(
        default=None,
        description="Explicit per-file compiler-map settings for the file. Required exactly when "
        "the prover configuration carries the corresponding per-file compiler map (and no "
        "existing entry matches the file); rejected when it does not. Mirror the editor's "
        "declared settings when registering an editor-created file.",
    )

class RemoveFile(BaseModel):
    """
    Remove a file from the prover inputs. If the file is specified in the form `path/to/Contract.sol:Something`
    provide *only* the file path portion, i.e., `path/to/Contract.sol`
    """
    type: Literal["remove_file"]
    path_to_remove: str = Field(description="The path to the file to remove from prover inputs")

class SetStorageExtensionAnnotation(BaseModel):
    """
    Enable or disable the `storage_extension_annotation` prover flag, which makes the
    prover honor `@custom:storage-location` annotations. Setting it to false removes
    the flag from the configuration.
    """
    type: Literal["storage_extension_annotation"]
    value: bool = Field(description="True to enable the flag, false to remove it")

class SetStorageExtensionHarnesses(BaseModel):
    """
    Replace the `storage_extension_harnesses` prover flag: (contract, harness) pairs
    declaring that `harness` describes storage extensions of `contract`. The provided
    pairs replace any previously configured ones; an empty list removes the flag from
    the configuration.
    """
    type: Literal["storage_extension_harnesses"]
    harnesses: list[tuple[SolidityIdentifier, SolidityIdentifier]] = Field(
        description="The (extended contract, harness contract) pairs, as Solidity identifiers"
    )

class ExtensionSpec(BaseModel):
    """One extension of a contract: the extending contract and the functions to exclude."""
    extension: SolidityIdentifier = Field(description="The Solidity identifier of the extension contract")
    exclude: list[str] = Field(
        default_factory=list,
        description="Function names of the extension to exclude; empty to include all",
    )

class SetContractExtensions(BaseModel):
    """
    Replace the `contract_extensions` prover flag: for each extended contract, the
    extension specs to apply. The provided mapping replaces any previously configured
    one; an empty mapping removes the flag from the configuration.
    """
    type: Literal["contract_extensions"]
    extensions: dict[SolidityIdentifier, list[ExtensionSpec]] = Field(
        description="Extended contract -> its extension specs"
    )

type ConfigEdit = Annotated[
    AddFile | RemoveFile | SetStorageExtensionAnnotation | SetStorageExtensionHarnesses
    | SetContractExtensions,
    Discriminator("type"),
]

# Conf keys written by the flag edits above. The run overlay silently forces its own
# keys onto the base config (see OVERLAY_OWNED_KEYS), so an "accepted" edit to one of
# them would never reach the prover — the two sets must stay disjoint.
_FLAG_KEYS = frozenset({
    "storage_extension_annotation", "storage_extension_harnesses", "contract_extensions",
})
assert not (_shadowed := _FLAG_KEYS & OVERLAY_OWNED_KEYS), \
    f"flag edits shadowed by the run overlay: {', '.join(sorted(_shadowed))}"

class ConfigEditTool(WithAsyncImplementation[Command | str], WithInjectedId, WithInjectedState[ProverStateExtra]):
    """
    Call this tool to make edits to the prover configuration.

    Each individual edit is applied in some sequence; if the edits conflict with one another the result is undefined.
    The configuration change is atomic: if any of the edits fail to apply the configuration will remain unchanged,
    and the issue will be returned. Otherwise, the updated configuration is returned as the result of this call.
    """
    edits: list[ConfigEdit] = Field(
        description="A list of the atomic edits to make to the file."
    )

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
                case AddFile(file_path=to_add, contract_name=explicit_name, compiler_settings=settings):
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
                    ext = extend_compiler_maps(curr_config, [(to_add, settings)])
                    if isinstance(ext, MapViolations):
                        return map_violation_message(ext)
                    curr_config = ext.config
                case SetStorageExtensionAnnotation(value=value):
                    if value:
                        curr_config["storage_extension_annotation"] = True
                    else:
                        curr_config.pop("storage_extension_annotation", None)
                case SetStorageExtensionHarnesses(harnesses=harnesses):
                    if any(".sol" in c or ".sol" in h for (c, h) in harnesses):
                        return ".sol extension found in a harness pair; provide contract identifiers, not filenames"
                    if harnesses:
                        curr_config["storage_extension_harnesses"] = [f"{c}={h}" for (c, h) in harnesses]
                    else:
                        curr_config.pop("storage_extension_harnesses", None)
                case SetContractExtensions(extensions=extensions):
                    if any(".sol" in c or any(".sol" in e.extension for e in specs)
                           for (c, specs) in extensions.items()):
                        return ".sol extension found in a contract extension; provide contract identifiers, not filenames"
                    if extensions:
                        curr_config["contract_extensions"] = {
                            c: [e.model_dump() for e in specs] for (c, specs) in extensions.items()
                        }
                    else:
                        curr_config.pop("contract_extensions", None)

        return tool_state_update(
            self.tool_call_id,
            f"Accepted, new config is:\n```json\n{json.dumps(curr_config, indent=2)}\n```",
            config=curr_config
        )

class ApplyEditTool(WithAsyncDependencies[str | Command, EditStore], WithInjectedState[SourceCVLGenerationExtra], WithInjectedId):
    """
     Apply the edit staged by the edit agent to your working tree.
    """
    edit_id: str = Field(description="The unique edit ID produced by the editor you want to apply")

    @override
    async def run(self) -> str | Command:
        with self.tool_deps() as dep:
            if self.edit_id in self.state["version_history"]:
                return f"{self.edit_id} has already been applied; if you want to revert to that state, use the {REVERT_TO_EDIT} tool"
            new_state = await dep.read(self.edit_id)
            if new_state is None:
                return f"{self.edit_id} does not denote any known edit"
            return tool_state_update(
                tool_call_id=self.tool_call_id,
                content="Edit applied",
                vfs=new_state.vfs,
                version_history=[self.edit_id]
            )

@dataclass
class HistoryDeps:
    mat: VFSAccessor[VFSState]
    edit_store: EditStore

class EditHistoryLog(WithAsyncDependencies[str, HistoryDeps], WithInjectedState[SourceCVLGenerationExtra]):
    """
    Use this to view a list of the applied edit ids, and the changes to the source code made on
    each edit
    """

    @override
    async def run(self) -> str:
        hist = self.state["version_history"]
        if len(hist) == 0:
            return "No edits applied, working against clean project directory"
        fetched_states: list[dict[str, str]] = []
        history : list[tuple[str, str, str]] = []
        with self.tool_deps() as dep:
            for (i, edit_id) in enumerate(hist):
                edit_state = await dep.edit_store.read(edit_id)
                if edit_state is None:
                    return "Something has gone very wrong; your edit history has an orphan ID. This is an unrecoverable error; terminate your task immediately"
                if i == 0:
                    prev = {}
                else:
                    prev = fetched_states[i - 1]
                diff = summarize_changes(
                    {"vfs": edit_state.vfs}, dep.mat, prev
                )
                history.append((edit_id, edit_state.executive_summary, diff))
                fetched_states.append(edit_state.vfs)
        to_format = [
            f"""
--- Edit #{i} (ID: {t})

Summary: {summary}

Diff from {"prior edit" if i > 0 else "project directory"}:

{diff}
"""
            for (i,(t, summary, diff)) in enumerate(history)
        ]
        return "\n\n".join(to_format)
    
class RevertToEdit(WithAsyncDependencies[Command | str, EditStore], WithInjectedId, WithInjectedState[SourceCVLGenerationExtra]):
    """
    Call this tool to revert to a prior edit in your history, or (with a null
    edit id) to discard every applied edit and return to the unedited project
    directory.
    """
    edit_id: str | None = Field(description="An edit ID to revert to (it must appear in your history), or null to revert to the unedited project directory")

    async def run(self) -> str | Command:
        if self.edit_id is None:
            if not self.state["version_history"]:
                return "No edits are applied; the working copy is already the unedited project directory"
            return tool_state_update(
                self.tool_call_id,
                "Reverted to the unedited project directory",
                vfs={},
                version_history=[WIPE_HISTORY],
            )
        if self.edit_id not in self.state["version_history"]:
            return f"{self.edit_id} does not appear in your edit history, nothing to revert to"
        if self.state["version_history"][-1] == self.edit_id:
            return f"Already at edit id {self.edit_id}, nothing to do"
        with self.tool_deps() as dep:
            i = self.state["version_history"].index(self.edit_id)
            target = await dep.read(self.edit_id)
            if target is None:
                return f"{self.edit_id} is in your history but absent from the edit store; this is an unrecoverable error"
            return tool_state_update(
                self.tool_call_id,
                f"Reverted to id {self.edit_id}",
                vfs=target.vfs,
                version_history=[WIPE_HISTORY, *self.state["version_history"][:i+1]]
            )


def generate_edit_management_tools(
    ctx: WorkflowContext[CVLGeneration],
    source_env: ServiceHost,
    edit: EditStore,
    live_tools: LiveEditTools,
) -> list[BaseTool]:
    editor = editor_tool(
        ctx=ctx,
        env=source_env,
        edit_tools=live_tools,
        edit_store=edit
    )
    return [
        editor,
        ApplyEditTool.bind(edit).as_tool(COMMIT_EDIT),
        EditHistoryLog.bind(HistoryDeps(live_tools.mat, edit)).as_tool(EDIT_HISTORY_LOG),
        RevertToEdit.bind(edit).as_tool(REVERT_TO_EDIT),
        ConfigEditTool.as_tool(CONFIG_EDIT),
    ]


@dataclass(frozen=True)
class SourceEditing:
    """The editing-enabled generation phase's kit: the live tool suite
    (vfs-aware reads + versioned explorer + live doc ref, plus the write tools
    the editor sub-agent uses) and the edit snapshot store. Phases whose output
    must hold against the unedited source — structural invariants — run
    without one."""
    live: LiveEditTools
    store: EditStore


class _LastAttemptEdits(BaseModel):
    """Sibling of cvl_generation's last-attempt draft cache: the applied-edit
    history at snapshot time. The working copy itself is deliberately not
    stored — the author's vfs only ever changes wholesale to an edit-store
    snapshot, so it is always recoverable as ``store[version_history[-1]].vfs``.

    Written on the same exit path as the draft cache but *after* it (the inner
    ``finally`` runs first), so a crash between the two puts can leave a fresh
    draft paired with stale history. Accepted risk: that resume degrades to
    the pre-snapshot behavior (a draft referencing edits that were not
    restored), and the window is two consecutive store puts.

    ``config`` snapshots the prover config for the same reason the history is
    snapshotted: ``config_edit`` changes (e.g. an added file registering an
    editor-minted harness) must survive the restart together with the working
    copy they describe. None on records written before the field existed, in
    which case resume falls back to the initial config.
    """
    version_history: list[str]
    config: dict | None = None


LAST_ATTEMPT_EDITS_KEY = CacheKey[CVLGeneration, _LastAttemptEdits]("last_attempt_edits")


@dataclass(frozen=True)
class _LiveJudgeHost:
    """Judge construction surface for the editing pipeline: RAG tools plus the
    vfs-aware read suite, so the judge reads the author's working copy (seeded
    into its state per invocation via the SourceSnapshot lift) rather than the
    on-disk baseline."""
    env: ServiceHost
    editing: SourceEditing

    def builder_heavy(self) -> Builder[None, None, None]:
        return self.env.builder_heavy()

    @property
    def sort(self) -> Sort:
        return self.env.sort

    @property
    def judge_tools(self) -> tuple[BaseTool, ...]:
        return (
            self.env.rag_tools
            + tuple(self.editing.live.read_tools)
            + (self.editing.live.explorer, self.editing.live.doc_tool)
        )


@tool_display("Getting feedback", "Feedback")
class EditorAwareFeedbackTool(
    FeedbackToolBase[SourceCVLGenerationState],
    WithAsyncDependencies[Command, ContextualFeedbackToolImpl[SourceSnapshot]],
):
    __doc__ = FeedbackToolBase.__doc__

    @override
    async def _get_feedback(
        self, spec: str, skipped: list[SkippedProperty]
    ) -> PropertyFeedbackProtocol:
        with self.tool_deps() as judge:
            assert "vfs" in self.state
            snap = SourceSnapshot(
                vfs=self.state["vfs"],
                version_history=self.state["version_history"],
            )
            return await judge(snap, spec, skipped, self.rebuttals, self.tool_call_id)

    @override
    def _version_history(self) -> Sequence[str]:
        return self.state["version_history"]


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
    spec_stem: str,
    editing: SourceEditing | None,
) -> BatchGeneratedCVLResult:
    # *spec_dir* (project-root-relative) is where the caller will persist the spec
    # authored here. The prover resolves the spec's CVL imports relative to its own
    # directory, so resource imports are expressed relative to *spec_dir*.
    # *spec_stem* is the basename it is persisted under; the prover materializes its
    # transient spec/conf under the same stem so on-disk names match the dump.
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
        "contract_name": source.contract_name,
        "sort": "existing"
    })

    titles = [p.title for p in props]
    judge_ctx = ctx.child(CVL_JUDGE_KEY)
    judge_prompt = FeedbackTemplate.bind({
        "sort": "existing",
        "context": component,
        "source_editing": editing is not None,
    })
    if editing is None:
        feedback_suite = property_tools(
            property_feedback_judge(judge_ctx, env, judge_prompt, props)
        )
    else:
        judge_impl = source_feedback_judge(
            judge_ctx, _LiveJudgeHost(env, editing), judge_prompt, props
        )
        feedback_suite = [
            EditorAwareFeedbackTool.bind(judge_impl).as_tool("feedback_tool"),
            *skip_tools(titles),
        ]

    # use "cache=long" to account for very long prover runs.
    # on anthropic (the only backend we support) a long cache is 1hr
    # NB that on longer prover runs we'll still get a cache miss;
    # this is a trade off we may have to revisit later.
    b = env.builder_heavy(cache_level=CacheLevel.LONG).with_tools(
        env.rag_tools
    )
    if editing is not None:
        b = b.with_tools(
            editing.live.read_tools
        ).with_tools(
            [editing.live.explorer, editing.live.doc_tool]
        ).with_tools(
            generate_edit_management_tools(ctx, env, editing.store, editing.live)
        )
    else:
        b = b.with_tools(env.source_tools)
    task_graph = b.with_tools(
        static_tools()
    ).with_tools(
        feedback_suite
    ).with_tools(
        [prover_tool,
         ExpectRulePassage.as_tool("expect_rule_passage"),
         ExpectRuleFailure.as_tool("expect_rule_failure"),
         GiveUpTool.as_tool("give_up"),
         PublishResultTool.bind(titles).as_tool("result"),
         ctx.get_memory_tool()]
    ).with_state(
        SourceCVLGenerationState
    ).with_output_key(
        "result"
    ).with_input(
        SourceCVLGenerationInput
    ).with_sys_prompt_template(
        "property_generation_system_prompt.j2", source_editing=editing is not None
    ).inject(
        lambda d: bound_template.render_to(d.with_initial_prompt_template)
    ).with_summary_config(
        PropertyGenerationConfig(source_editing=editing is not None)
    ).compile_async()

    # Crash recovery for the working copy, sibling to cvl_generation's draft
    # recovery: restore the applied-edit history and rehydrate the vfs from the
    # durable edit store. Independent of whether a draft snapshot exists — a
    # crash after edits but before any draft still restores the fork.
    restored_history: list[str] = []
    restored_vfs: dict[str, str] = {}
    restored_config: dict = init_config
    resume_note: list[str | dict] = []
    if editing is not None:
        prior = await ctx.child(LAST_ATTEMPT_EDITS_KEY).cache_get(_LastAttemptEdits)
        if prior is not None:
            if prior.config is not None:
                restored_config = prior.config
            if prior.version_history:
                tail = await editing.store.read(prior.version_history[-1])
                assert tail is not None, (
                    f"recovered edit {prior.version_history[-1]} absent from the edit store"
                )
                restored_history = prior.version_history
                restored_vfs = tail.vfs
                resume_note = [
                    "Source edits applied during your previous attempt at this task have "
                    f"been restored to your working copy; use `{EDIT_HISTORY_LOG}` to review them."
                ]

    try:
        res_state = await run_cvl_generator(
            ctx = ctx,
            d = task_graph,
            description=description,
            in_state=SourceCVLGenerationInput(
                curr_spec=None,
                config=restored_config,
                input=resume_note,
                required_validations=[FEEDBACK_VALIDATION_KEY, PROVER_VALIDATION_KEY],
                rule_skips={},
                skipped=[],
                property_rules=[],
                validations={},
                failed=None,
                vfs=restored_vfs,
                version_history=restored_history
            )
        )
    finally:
        if editing is not None:
            last_state = (
                await task_graph.aget_state({"configurable": {"thread_id": ctx.thread_id}})
            ).values
            hist = last_state.get("version_history")
            if hist is not None:
                await ctx.child(LAST_ATTEMPT_EDITS_KEY).cache_put(
                    _LastAttemptEdits(
                        version_history=list(hist), config=last_state.get("config"),
                    )
                )

    assert "result" in res_state
    assert res_state["failed"] is not None
    if res_state["failed"]:
        return GaveUp(reason=res_state["result"])
    d = res_state["curr_spec"]
    assert d is not None
    applied_edits: list[AppliedEdit] = []
    if editing is not None:
        for edit_id in res_state["version_history"]:
            rec = await editing.store.read(edit_id)
            assert rec is not None, f"edit {edit_id} in history but absent from the edit store"
            applied_edits.append(AppliedEdit(
                edit_id=edit_id,
                executive_summary=rec.executive_summary,
                why_sound=rec.why_sound,
            ))
    # Persist the base prover config and last run link from the final state so a later cache
    # hit (which skips the prover) can still reconstruct certora/confs and retain the link.
    assert "vfs" in res_state
    return GeneratedCVL(
        commentary=res_state["result"],
        cvl=d,
        skipped=res_state["skipped"],
        property_rules=res_state["property_rules"],
        config=res_state["config"],
        final_link=res_state.get("prover_link"),
        vfs=res_state["vfs"],
        applied_edits=applied_edits,
    )

