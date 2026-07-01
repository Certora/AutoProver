from typing import Optional, cast, Any, Callable
import logging
import uuid
from dataclasses import dataclass
import pathlib
import psycopg

from langchain_core.runnables import RunnableConfig
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from graphcore.graph import Builder
from graphcore.tools.memory import async_memory_tool
from graphcore.tools.vfs import VFSState, VFSAccessor

from composer.input.types import WorkflowOptions, InputData, ResumeFSData, ResumeIdData, ResumeInput, NativeFS
from composer.input.files import Document, InMemoryTextFile
from composer.kb.knowledge_base import DefaultEmbedder, kb_tools as make_kb_tools
from composer.workflow.factories import get_vfs_tools, get_memory_ns
from composer.workflow.services import standard_connections, IndexedConnections
from composer.workflow.types import PromptParams, WorkflowSuccess, WorkflowFailure, WorkflowCrash, WorkflowResult
from composer.workflow.meta import create_resume_commentary
from composer.core.context import AIComposerContext, ProverOptions
from composer.core.state import AIComposerState
from composer.prover.core import make_prover_options, CexHandler
from composer.core.validation import ValidationType, prover, reqs as req_type
from composer.rag.db import rag_context, ComposerRAGDB
from composer.rag.models import get_model as get_rag_model
from composer.audit.db import AuditDB, AuditDBSink, ResumeArtifact, InputFileLike
from composer.natreq.extractor import get_requirements
from composer.natreq.judge import get_judge_tool
from composer.spec.cvl_research import CVL_RESEARCH_BASE_DOC, cvl_research_tool
from composer.tools.relaxation import requirements_relaxation
from composer.tools.search import cvl_manual_tools, cvl_manual_search
from composer.templates.loader import load_jinja_template
from composer.io.protocol import CodeGenIOHandler, WorkflowPurpose
from composer.io.context import with_handler, run_graph
from composer.ui.codegen_events import CodeGenEventHandler
from composer.core.state import AIComposerInput, AIComposerExtra
from composer.ui.tool_display import tool_context
from composer.prover.agentic_analyzer import AgenticCexHandler
from composer.prover.report_store import ReportStore
from composer.spec.proposal_store import ProposalStore
from composer.spec.cex_remediation import cex_remediation_tool, summary_critic_tool
from composer.spec.guidance import ERC20TokenGuidance
from composer.tools.working_spec import ApplyRemediationProposal, CommitWorkingSpec, ReadWorkingSpec, WriteWorkingSpec
from composer.tools.prover import CertoraProverTool, ProverDeps
from composer.tools.proposal import propose_spec_change
from composer.tools.question import human_in_the_loop
from composer.tools.result import code_result
from composer.workflow.codegen_store import CodegenStore
from composer.workflow.summarization import SummaryGeneration


_KB_NS = ("cvl",)


@dataclass
class _CodegenResearchContext:
    """Satisfies ResearchContext protocol for the CVL research sub-agent."""
    _store: BaseStore
    _kb_ns: tuple[str, ...]
    _checkpointer: Checkpointer
    _thread_prefix: str

    def kb_tools(self, read_only: bool) -> list[BaseTool]:
        return make_kb_tools(self._store, self._kb_ns, read_only)

    @property
    def checkpointer(self) -> Checkpointer:
        return self._checkpointer

    def uniq_thread_id(self) -> str:
        return f"{self._thread_prefix}-{uuid.uuid4().hex[:16]}"


def get_reference_input(input_data: InputData, debug_prompt: Optional[str]) -> str:
    return load_jinja_template(
        "workflow_info.j2",
        spec_filename=input_data.spec.basename,
        interface_filename=input_data.intf.basename,
        system_doc_filename=input_data.system_doc.basename,
        debug_prompt=debug_prompt)

def _get_empty_extra() -> AIComposerExtra:
    return AIComposerExtra(
        validation={}, skipped_reqs=set(), working_spec=None
    )


def get_fresh_input(input: InputData, workflow_options: WorkflowOptions) -> AIComposerInput:
    return AIComposerInput(input=[
                input.intf.to_dict(),
                input.spec.to_dict(),
                input.system_doc.to_dict(),
                {
                    "type": "text",
                    "text": get_reference_input(input_data=input, debug_prompt=workflow_options.debug_prompt_override)
                }
            ], vfs={"rules.spec": input.spec.string_contents}, **_get_empty_extra())

@dataclass
class InputChangeDesc:
    orig_text: str
    updated_text: str

    single_form: str
    plural: str

    vfs_note: Optional[str]

def get_resume_prompt_common(
        art: ResumeArtifact,
        res: ResumeInput,
        updated_spec: str,
        other_changes: list[InputChangeDesc] | None = None
        ) -> list[str | dict]:
    changes = []
    if other_changes is not None:
        changes.extend(other_changes)

    if res.new_system is not None:
        changes.append(InputChangeDesc(
            orig_text=art.system_doc,
            updated_text=res.new_system.string_contents,
            plural="system documents",
            single_form="system document",
            vfs_note=None
        ))

    return [load_jinja_template(
        "resume_prompt.j2",
        commentary=art.commentary,
        spec_change_commentary=res.comments,
        orig_spec=art.spec_file,
        new_spec=updated_spec,
        other_changes=changes
    )]

def get_resume_id_input(input: ResumeIdData, resume_art: ResumeArtifact, workflow_options: WorkflowOptions) -> AIComposerInput:

    input_messages : list[str | dict] = get_resume_prompt_common(
        art=resume_art,
        res=input,
        updated_spec=input.new_spec.string_contents
    )
    if workflow_options.debug_prompt_override is not None:
        input_messages.append(workflow_options.debug_prompt_override)

    vfs_materialize = resume_art.vfs.to_dict()
    new_vfs = { k: v.decode("utf-8") for (k, v) in vfs_materialize.items() }
    new_vfs["rules.spec"] = input.new_spec.string_contents
    return AIComposerInput(
        input=input_messages,
        vfs=new_vfs,
        **_get_empty_extra()
    )

def get_resume_fs_input(input: ResumeFSData, resume_art: ResumeArtifact, workflow_options: WorkflowOptions) -> tuple[AIComposerInput, InputFileLike, InputFileLike]:
    path = pathlib.Path(input.file_path)

    spec_p = path / "rules.spec"
    if not spec_p.is_file():
        raise RuntimeError("Specification file is apparently missing")
    new_spec = spec_p.read_text()

    intf_p = path / resume_art.interface_path
    if not intf_p.is_file():
        raise RuntimeError("Interface file was moved or deleted")
    changes = []
    if (intf_text := intf_p.read_text()) != resume_art.interface_file:
        changes.append(InputChangeDesc(
            orig_text=resume_art.interface_file,
            updated_text=intf_text,
            single_form="interface",
            plural="interfaces",
            vfs_note=resume_art.interface_path
        ))
    input_messages = get_resume_prompt_common(
        art=resume_art,
        res=input,
        other_changes=changes,
        updated_spec=new_spec
    )
    input_messages.append("In addition to the explicit changes mentioned above, the contents of the VFS may have been arbitrarily changed since your last work. " \
    "Some of these changes may cause the current implementation to no longer compile. Thus, analyze the current implementation and consider what changes are necessary to " \
    "fix any compilation errors.")

    if workflow_options.debug_prompt_override is not None:
        input_messages.append(workflow_options.debug_prompt_override)

    return (AIComposerInput(input=input_messages, vfs={}, **_get_empty_extra()), NativeFS(intf_p), NativeFS(spec_p))


def _system_doc_as_document(fl: InputFileLike) -> Document:
    """Bridge a resume-path system-doc handle to a ``Document`` for the CEX
    agents. Audit/resume handles carry text (``InputFileLike.string_contents``
    is typed ``str``), so an inline text document is the faithful
    representation; binary system docs on resume await the input-layer rework.
    """
    return InMemoryTextFile(basename=fl.basename, string_contents=fl.string_contents)


async def execute_ai_composer_workflow(
    handler: CodeGenIOHandler,
    llm: BaseChatModel,
    input: InputData | ResumeFSData | ResumeIdData,
    workflow_options: WorkflowOptions,
    memory_namespace: str | None = None,
    resume_work_key: str | None = None,
) -> WorkflowResult:
    """Execute the AI Composer workflow with interrupt handling.

    Opens the async langgraph connection bundle (store / checkpointer /
    indexed store / memory / uploader) and the (still-sync) audit DB, then
    delegates to ``_run_codegen``. The audit DB stays on psycopg for now;
    only the langgraph side moves async.
    """
    # One embedding model, shared by the indexed-store embedder and the RAG
    # connection — get_model() loads a fresh SentenceTransformer per call, so
    # instantiating once avoids double-loading the same nomic-embed model.
    model = get_rag_model()
    async with (
        standard_connections(embedder=DefaultEmbedder(model)) as conn,
        rag_context(workflow_options.rag_db, model) as rag_db,
    ):
        audit_conn = psycopg.connect(workflow_options.audit_db)
        try:
            return await _run_codegen(
                handler, llm, input, workflow_options, conn, rag_db, AuditDB(audit_conn),
                memory_namespace=memory_namespace, resume_work_key=resume_work_key,
            )
        finally:
            audit_conn.close()

async def _run_codegen(
    handler: CodeGenIOHandler,
    llm: BaseChatModel,
    input: InputData | ResumeFSData | ResumeIdData,
    workflow_options: WorkflowOptions,
    conn: IndexedConnections,
    rag_db: ComposerRAGDB,
    audit_db: AuditDB,
    *,
    memory_namespace: str | None,
    resume_work_key: str | None,
) -> WorkflowResult:
    logger = logging.getLogger(__name__)

    checkpointer = conn.checkpointer

    thread_id = workflow_options.thread_id

    if thread_id is None:
        thread_id = "crypto_session_" + str(uuid.uuid1())
        await handler.log_workflow_thread(WorkflowPurpose.CODEGEN, thread_id)
        logger.info(f"Selected thread id: {thread_id}")

    mem_root = memory_namespace or thread_id

    prompt_params: PromptParams
    fs_layer: str | None = None
    flow_input: AIComposerInput

    system_doc: InputFileLike
    system_doc_doc: Document
    interface_file: InputFileLike
    spec_file: InputFileLike
    resume_art : None | ResumeArtifact = None

    match input:
        case InputData():
            prompt_params = PromptParams(is_resume=False)
            flow_input = get_fresh_input(input, workflow_options)
            system_doc = input.system_doc.to_file_like()
            system_doc_doc = input.system_doc
            interface_file = input.intf
            spec_file = input.spec

        case ResumeIdData() | ResumeFSData():
            prompt_params = PromptParams(is_resume=True)

            resume_art = audit_db.get_resume_artifact(thread_id=input.thread_id)
            if input.new_system is None:
                system_doc = resume_art.system_vfs_handle
            else:
                system_doc = input.new_system
            system_doc_doc = _system_doc_as_document(system_doc)
            match input:
                case ResumeFSData():
                    (flow_input, interface_file, spec_file) = get_resume_fs_input(input, resume_art, workflow_options)
                    fs_layer = input.file_path
                case ResumeIdData():
                    interface_file = resume_art.intf_vfs_handle
                    flow_input = get_resume_id_input(input, resume_art, workflow_options)
                    spec_file = input.new_spec

    store = conn.store
    report_store = ReportStore(store=store)
    codegen_store = CodegenStore(store)

    req_mem_tool = async_memory_tool(conn.memory(get_memory_ns(mem_root, "natreq")))

    reqs_list = await codegen_store.requirements(thread_id)
    if reqs_list is None and not workflow_options.skip_reqs:
        print("Analyzing requirements...")
        extraction = await get_requirements(
            handler,
            workflow_options,
            llm,
            system_doc,
            spec_file,
            req_mem_tool,
            resume_art,
        )
        reqs_list = extraction.reqs
        await handler.log_workflow_thread(WorkflowPurpose.NATREQ, extraction.thread_id)
        await codegen_store.record_requirements(thread_id, reqs_list)
    extra_tools = []

    if reqs_list is not None:
        judge_tool = get_judge_tool(
            reqs=reqs_list,
            mem_tool=req_mem_tool,
            unbound=llm,
            vfs_tools=get_vfs_tools(
                fs_layer=fs_layer, immutable=True
            )[0]
        )
        extra_tools.append(judge_tool)
        extra_tools.append(requirements_relaxation)

    memory = async_memory_tool(conn.memory(get_memory_ns(mem_root, "composer")))
    extra_tools.append(memory)

    research_tool, cvl_builder = _cvl_knowledge_setup(llm, rag_db, conn, workflow_options.recursion_limit)
    extra_tools.append(research_tool)

    # VFS tooling: the mutable layer (its materializer is shared into the
    # AIComposerContext) plus an immutable view for the read-only sub-agents.
    vfs_tooling, materializer = get_vfs_tools(fs_layer=fs_layer, immutable=False)
    immut_vfs_tools, _ = get_vfs_tools(fs_layer=fs_layer, immutable=True)

    # Prover options, built here so the prover tool's ProverDeps can bind them.
    resolved = make_prover_options(cloud=not workflow_options.local_prover)
    prover_opts = ProverOptions(
        capture_output=workflow_options.prover_capture_output,
        keep_folder=workflow_options.prover_keep_folders,
        extra_args=resolved.extra_args,
    )

    cex_remediation_tools = _remediation_tools(
        cvl_builder,
        conn,
        system_doc_doc,
        report_store,
        immut_vfs_tools,
        materializer,
        async_memory_tool(conn.memory(get_memory_ns(mem_root, "cex-remediation"))),
        workflow_options.recursion_limit,
    )
    extra_tools.extend(cex_remediation_tools)

    cex_handler = AgenticCexHandler(
        builder=cvl_builder,
        report_store=report_store,
        recursion_limit=workflow_options.recursion_limit
    )

    crypto_tools = _codegen_author_tools(cex_handler, prover_opts, rag_db, vfs_tooling)
    workflow_builder = _codegen_builder(llm, crypto_tools)

    workflow_graph = workflow_builder.with_tools(extra_tools).with_sys_prompt_template(
        "system_prompt.j2"
    ).with_initial_prompt_template(
        "synthesis_prompt.j2", **prompt_params
    ).build_async()[0]

    audit_db.register_run(
        thread_id=thread_id,
        system_doc=system_doc,
        interface_file=interface_file,
        spec_file=spec_file,
        vfs_init=materializer.iterate(flow_input),
        reqs=reqs_list
    )

    workflow_exec = workflow_graph.compile(checkpointer=checkpointer, store=store)
    if reqs_list is not None:
        flow_input["input"].append(f"""
    Additionally, the implementation MUST satisfy the following requirements:
    {"\n".join(f"{i}. {r}" for (i, r) in enumerate(reqs_list, start = 1))}
    """)

    if resume_work_key is not None:
        snapshot = await codegen_store.recovery(resume_work_key)
        if snapshot is not None:
            vfs_files = list(snapshot.items())
            recovery_msg = load_jinja_template("crash_recovery_context.j2", vfs_files=vfs_files)
            flow_input["input"].insert(0, recovery_msg)

    try:
        import grandalf
        layout = workflow_exec.get_graph().draw_ascii()
        logger.debug(f"\n{layout}")
    except ModuleNotFoundError:
        pass

    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    config["recursion_limit"] = workflow_options.recursion_limit

    if workflow_options.checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = workflow_options.checkpoint_id

    required_validations: list[ValidationType] = [prover]
    if reqs_list is not None:
        required_validations.append(req_type)

    work_context = AIComposerContext(
        vfs_materializer=materializer, required_validations=required_validations
    )

    audit_sink = AuditDBSink(audit_db, thread_id)

    try:
        async with with_handler(handler, CodeGenEventHandler(handler, audit_sink)):
            final_state = await run_graph(workflow_exec, work_context, flow_input, config, description="Code generation")

        result = final_state.get("generated_code", None)
        if result is None:
            return WorkflowFailure()

        res_commentary = await create_resume_commentary(final_state, llm=llm)
        audit_db.register_complete(
            thread_id, materializer.iterate(final_state), res_commentary.interface_path, res_commentary.commentary
        )

        await handler.output(result, materializer, final_state)
        return WorkflowSuccess()
    except Exception as exc:
        await handler.show_error(exc)
        # Attempt to capture VFS from last checkpoint
        resume_key: str | None = None
        try:
            ct = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
            if ct is not None:
                channel_values = cast(VFSState, ct.checkpoint["channel_values"])
                vfs_snapshot = {path: content.decode("utf-8") for path, content in materializer.iterate(channel_values)}
                resume_key = f"crash_{thread_id}_{uuid.uuid4().hex[:8]}"
                await codegen_store.save_recovery(resume_key, vfs_snapshot)
                logger.info(f"Saved crash recovery snapshot: {resume_key}")
        except Exception as snapshot_exc:
            logger.warning(f"Failed to capture crash snapshot: {snapshot_exc}")
        return WorkflowCrash(resume_work_key=resume_key, error=exc)


def _cvl_knowledge_setup(
    llm: BaseChatModel,
    rag_db: ComposerRAGDB,
    conn: IndexedConnections,
    recursion_limit: int,
) -> tuple[BaseTool, Builder]:
    """CVL knowledge tooling built on the standard connections. Returns the
    research sub-agent (which uses cvl_research's own default run_to_completion
    runner) and the rag-equipped builder the CEX sub-agents extend — with the
    research tool already bound, since every consumer wants it. ``base_rag_tools``
    is the CVL manual + KB search over the standard indexed store."""
    base_rag_tools = tuple(cvl_manual_tools(rag_db)) + tuple(
        make_kb_tools(conn.indexed_store, _KB_NS, read_only=True)
    )
    builder = (
        Builder()
        .with_llm(llm)
        .with_loader(load_jinja_template)
        .with_checkpointer(conn.checkpointer)
    )

    @dataclass(frozen=True)
    class _CVLResearchEnv:
        builder: Builder[None, None, None]
        base_rag_tools: tuple[BaseTool, ...]

    research_doc = CVL_RESEARCH_BASE_DOC + " Do NOT use this for source code questions — use the VFS tools for that."
    research_tool = cvl_research_tool(
        _CVLResearchEnv(builder, base_rag_tools), research_doc, recursion_limit
    )
    return research_tool, builder.with_tools([*base_rag_tools, research_tool])


def _remediation_tools(
    cvl_builder: Builder[None, None, None],
    conn: IndexedConnections,
    system_doc: Document,
    report_store: ReportStore,
    immut_vfs_tools: list[BaseTool],
    materializer: VFSAccessor[VFSState],
    mem_tool: BaseTool,
    recursion_limit: int,
) -> list[BaseTool]:
    """The CEX remediation sub-agent (+ its summary critic) and the agentic CEX
    handler. ``cvl_builder`` already carries the CVL manual / KB / research
    tools. Returns the author-facing tools (the remediator + apply-proposal) to
    add to the codegen toolset, plus the handler to inject into the prover via
    ProverDeps. ``recursion_limit`` is threaded into every sub-agent."""
    remediation_builder = cvl_builder.with_tools(
        immut_vfs_tools
    )
    proposal_store = ProposalStore(conn.store)

    cex_remediator = cex_remediation_tool(
        remediation_builder,
        materializer,
        system_doc,
        mem_tool,
        proposal_store,
        report_store,
        recursion_limit=recursion_limit,
    )
    author_tools: list[BaseTool] = [
        cex_remediator,
        ApplyRemediationProposal.bind(proposal_store).as_tool("apply_remediation_proposal"),
    ]
    return author_tools


def _codegen_author_tools(
    cex_handler: CexHandler,
    prover_opts: ProverOptions,
    rag_db: ComposerRAGDB,
    vfs_tooling: list[BaseTool],
) -> list[BaseTool]:
    """The codegen author's tool set (formerly ``get_cryptostate_builder``'s
    crypto_tools). The prover tool is bound with its per-run deps here;
    cvl_manual_search takes the rag_db instance directly."""
    return [
        CertoraProverTool.bind(
            ProverDeps(cex_handler=cex_handler, prover_opts=prover_opts)
        ).as_tool("certora_prover"),
        propose_spec_change,
        human_in_the_loop,
        code_result,
        cvl_manual_search(rag_db),
        *vfs_tooling,
        ReadWorkingSpec.as_tool("read_working_spec"),
        WriteWorkingSpec.as_tool("write_working_spec"),
        CommitWorkingSpec.as_tool("commit_working_spec"),
    ]


def _codegen_builder(llm: BaseChatModel, crypto_tools: list[BaseTool]) -> Builder:
    """The codegen workflow Builder with the author tool set bound."""
    return Builder().with_context(
        AIComposerContext
    ).with_loader(
        load_jinja_template
    ).with_input(
        AIComposerInput
    ).with_tools(
        crypto_tools
    ).with_state(
        AIComposerState
    ).with_llm(
        llm
    ).with_output_key(
        "generated_code"
    ).with_summary_config(
        SummaryGeneration()
    )
