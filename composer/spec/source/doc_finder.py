"""Design-document finder sub-agent + the doc-resolution funnel.

When the autoprover / foundry CLI is run WITHOUT a design document, this module's
:func:`resolve_design_doc` discovers one: a lite-tier sub-agent walks the project
tree with the bare file-system tools (``list_files`` / ``get_file`` / ``grep_files``)
and returns the single best existing design/specification file — or ``None`` with a
reason. The result is cached under a DOC-INDEPENDENT key (see
:func:`discovery_cache_key`) so a repeat run on the same project skips the agent.

The chosen path flows through the same ``uploader.get_document`` call the manual path
uses, so PDF/text handling, the source artifact, and the byte-hash root cache key are
all unchanged.

Shared by both entry points (``composer/spec/source/autoprove_common.py`` and
``composer/foundry/entry.py``). :func:`resolve_design_doc` is generic over the phase
marker so each pipeline routes the discovery task to its own ``DISCOVER_DESIGN_DOC``
phase / handler.
"""

import hashlib
import logging
import pathlib
from typing import Any, Literal, NotRequired, Sequence, TypedDict, override

from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from graphcore.graph import Builder, FlowInput, MessagesState, tool_return
from graphcore.tools.schemas import WithAsyncImplementation, WithInjectedId

from composer.input.files import Document, FileUploader
from composer.io.context import emit_custom_event
from composer.io.multi_job import HandlerFactory, HasName, TaskInfo, run_task
from composer.spec.context import CacheKey, WorkflowContext
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.service_host import ModelProvider
from composer.spec.source.source_env import build_basic_source_tools
from composer.spec.source.task_ids import DESIGN_DOC_DISCOVERY_TASK_ID
from composer.spec.util import uniq_thread_id

_logger = logging.getLogger(__name__)


class DesignDocChosenEvent(TypedDict):
    """Progress event emitted at design-doc-discovery completion. The console handler
    (``AutoProveConsoleHandler``) and the TUI app (``AutoProveApp``) render it from
    ``handle_progress_event``; other handlers ignore unknown types (``NullEventHandler``).
    ``source`` is the display verb ("discovered" / "reusing cached")."""
    type: Literal["design_doc_chosen"]
    source: str
    path: str
    reason: str


def _emit_choice(source: str, choice: "DesignDocChoice") -> None:
    """Surface the chosen design doc to the user as the discovery phase completes.

    Emits a progress event the console + TUI handlers render (the autoprove logger is
    files-only, so logging alone would be invisible, and stderr is hidden under the
    TUI). Must run inside the discovery task's handler scope — ``emit_custom_event``
    requires it."""
    if choice.selected_path is None:
        return  # no doc found — resolve_design_doc raises with the reason instead
    event: DesignDocChosenEvent = {
        "type": "design_doc_chosen",
        "source": source,
        "path": choice.selected_path,
        "reason": choice.reason,
    }
    emit_custom_event(event)
    _logger.info("%s design doc: %s — %s", source, choice.selected_path, choice.reason)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class DesignDocChoice(BaseModel):
    """The finder's verdict: one existing design document, or nothing.

    ``selected_path is None`` *is* the "no document found" signal — there is no
    separate boolean to keep consistent."""

    selected_path: str | None = Field(
        default=None,
        description=(
            "Project-root-relative path to the single best EXISTING design/specification "
            "document for the contract under analysis (e.g. 'docs/design.md', 'SPEC.md', "
            "'whitepaper.pdf'). null if NO document describes the system's intended "
            "behavior. No leading './'."
        ),
    )
    reason: str = Field(
        description="One or two sentences: why this file is the design doc, or why nothing qualifies.",
    )


class _FinderState(MessagesState):
    result: NotRequired[DesignDocChoice]


# ---------------------------------------------------------------------------
# Finder sub-agent
# ---------------------------------------------------------------------------


class _DocFinderParams(TypedDict):
    contract_name: str
    relative_path: str


_FINDER_PROMPT = TypedTemplate[_DocFinderParams]("design_doc_finder_prompt.j2")


def build_finder_graph(
    builder: Builder[None, None, None],
    source_tools: Sequence[BaseTool],
    contract_name: str,
    relative_path: str,
) -> CompiledStateGraph[_FinderState, None, FlowInput, Any]:
    """Compile the finder agent graph.

    Split out from :func:`find_design_doc` so it can be driven directly in a unit
    test, without the ``run_task`` handler scope that :func:`run_to_completion`
    requires. Mirrors ``harness.classifier_agent``'s build: bare fs tools, a Jinja
    system prompt, and a bound initial-prompt template carrying the contract identity.
    """
    bound = _FINDER_PROMPT.bind({
        "contract_name": contract_name,
        "relative_path": relative_path,
    })
    return bind_standard(
        builder, _FinderState,
    ).with_input(
        FlowInput,
    ).with_tools(
        list(source_tools),
    ).inject(
        lambda g: bound.render_to(g.with_initial_prompt_template),
    ).with_sys_prompt_template(
        "design_doc_finder_system_prompt.j2",
    ).compile_async(checkpointer=InMemorySaver())


async def find_design_doc(
    *,
    builder: Builder[None, None, None],
    source_tools: Sequence[BaseTool],
    contract_name: str,
    relative_path: str,
    recursion_limit: int,
) -> DesignDocChoice:
    """Run the finder agent to completion and return its verdict.

    Must be called within an active handler scope (i.e. inside ``run_task``); see
    :func:`_discover`."""
    graph = build_finder_graph(builder, source_tools, contract_name, relative_path)
    st = await run_to_completion(
        graph=graph,
        context=None,
        input=FlowInput(input=[]),
        recursion_limit=recursion_limit,
        thread_id=uniq_thread_id("doc_finder"),
        description="Design Doc Discovery",
    )
    assert "result" in st, "finder graph completed without a result"
    return st["result"]


def read_document_tool(uploader: FileUploader, project_root: str) -> BaseTool:
    """A ``read_document`` tool that lets the finder read a document properly —
    including PDFs, which ``get_file`` can only return as raw bytes.

    It loads the file through the same ``uploader.get_document`` the pipeline uses
    (text inline, binary via the Files API) and returns a ``Command`` that attaches
    the document as a **user-message content block** — the API-correct placement for
    documents (a ``tool_result`` cannot carry a document block). The model then reads
    the attachment on its next turn."""
    root = pathlib.Path(project_root)

    class ReadDocument(WithAsyncImplementation[Command], WithInjectedId):
        """Read a document — including a PDF — so you can judge its actual contents.

        Unlike `get_file` (which returns raw text and shows only gibberish for a PDF),
        this attaches the document to the conversation so you can read it. Use it for
        any PDF candidate, and for any file `get_file` returned as unreadable bytes,
        before deciding whether it is the design document. Pass one project-root-
        relative path."""

        path: str = Field(
            description="Project-root-relative path to the document to read "
            "(e.g. 'docs/spec.pdf', 'whitepaper.pdf'). No leading './'.",
        )

        @override
        async def run(self) -> Command:
            target = (root / self.path).resolve()
            if not target.is_relative_to(root.resolve()):
                return tool_return(
                    self.tool_call_id,
                    f"{self.path!r} is outside the project root; refusing to read it.",
                )
            doc = await uploader.get_document(target)
            if doc is None:
                return tool_return(
                    self.tool_call_id, f"cannot read {self.path!r}: not a regular file."
                )
            # Tool-result ack + a user message carrying the document block (documents
            # belong in user content, not in a tool_result).
            return Command(update={"messages": [
                ToolMessage(
                    tool_call_id=self.tool_call_id,
                    content=f"Loaded {self.path!r}; its contents are attached below.",
                ),
                HumanMessage(content=[doc.to_dict()]),
            ]})

    return ReadDocument.as_tool("read_document")


# ---------------------------------------------------------------------------
# Discovery cache
# ---------------------------------------------------------------------------

DESIGN_DOC_DISCOVERY_KEY = CacheKey[None, DesignDocChoice]("design-doc-discovery")


def discovery_cache_key(project_root: str, relative_path: str, contract_name: str) -> str:
    """A DOC-INDEPENDENT cache key for the discovery step.

    Unlike the root cache key (which hashes the chosen doc's bytes), discovery is
    keyed only on inputs known *before* a doc exists, so a repeat run on the same
    project reuses the previously chosen path instead of re-running the agent.
    Staleness is intentional and consistent with the root key, which already ignores
    the wider source tree: a newly-added doc isn't picked up until the cache namespace
    rotates.
    """
    combined = "|".join([project_root, relative_path, contract_name])
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


async def _discover[P: HasName](
    *,
    project_root: str,
    contract_name: str,
    relative_path: str,
    forbidden_read: str,
    uploader: FileUploader,
    models: ModelProvider,
    handler: HandlerFactory[P, None],
    discover_phase: P,
    disc_ctx: WorkflowContext[None],
) -> DesignDocChoice:
    """Discover a design doc, cached, as a visible ``DISCOVER_DESIGN_DOC`` task.

    The phase runs every time so the chosen doc is surfaced (console + TUI) on both
    fresh and cached runs — and the handler scope it installs is what lets the
    completion event reach the UI. On a cache hit it returns instantly without the
    agent. Mirrors ``classifier_agent``'s cache pattern: ``cache_get`` first, the agent
    only on a miss, ``cache_put`` after."""
    child = disc_ctx.child(DESIGN_DOC_DISCOVERY_KEY)

    async def _run() -> DesignDocChoice:
        cached = await child.cache_get(DesignDocChoice)
        if cached is not None:
            _emit_choice("reusing cached", cached)  # cache hit: report, skip the agent
            return cached
        source_tools = build_basic_source_tools(project_root, forbidden_read).base_source_tools
        # Add read_document so the finder can read PDFs (and other binary docs) properly,
        # not just judge them by filename.
        tools = [*source_tools, read_document_tool(uploader, project_root)]
        choice = await find_design_doc(
            builder=models.builder_lite(),
            source_tools=tools,
            contract_name=contract_name,
            relative_path=relative_path,
            recursion_limit=child.recursion_limit,
        )
        await child.cache_put(choice)
        _emit_choice("discovered", choice)
        return choice

    return await run_task(
        handler,
        TaskInfo(DESIGN_DOC_DISCOVERY_TASK_ID, "Design Doc Discovery", discover_phase),
        _run,
    )


# ---------------------------------------------------------------------------
# Resolution funnel
# ---------------------------------------------------------------------------


async def resolve_design_doc[P: HasName](
    *,
    system_doc_arg: str | None,
    project_root: str,
    contract_name: str,
    relative_path: str,
    forbidden_read: str,
    uploader: FileUploader,
    models: ModelProvider,
    handler: HandlerFactory[P, None],
    discover_phase: P,
    disc_ctx: WorkflowContext[None],
) -> tuple[pathlib.Path, Document]:
    """Resolve the design document to a ``(path, Document)`` pair.

    When ``system_doc_arg`` is given, read it directly (unchanged behavior, no phase).
    Otherwise run the finder under a ``run_task`` discovery phase, fail fast if it
    finds nothing, then load the chosen path through the same uploader. The returned
    path feeds the unchanged byte-hash root cache key, so a discovered doc and a
    supplied doc produce an identical key.
    """
    if system_doc_arg is not None:
        path = pathlib.Path(system_doc_arg)
        content = await uploader.get_document(path)
        if content is None:
            raise ValueError(f"cannot read {system_doc_arg}")
        return path, content

    choice = await _discover(
        project_root=project_root,
        contract_name=contract_name,
        relative_path=relative_path,
        forbidden_read=forbidden_read,
        uploader=uploader,
        models=models,
        handler=handler,
        discover_phase=discover_phase,
        disc_ctx=disc_ctx,
    )
    if choice.selected_path is None:  # None *is* "no doc found"
        raise ValueError(
            f"No design document found under {project_root}.\n"
            f"  {choice.reason}\n"
            "  Pass one explicitly as the design-doc argument — e.g. a file under test/ "
            "or a .json file, which the finder does not search."
        )
    path = pathlib.Path(project_root) / choice.selected_path
    content = await uploader.get_document(path)
    if content is None:
        raise ValueError(
            f"the finder selected {choice.selected_path!r}, but it could not be read "
            f"(resolved to {path})."
        )
    return path, content
