"""Unit tests for the design-document finder (composer/spec/source/design_doc_finder.py).

These run WITHOUT postgres or a live LLM:

- The discovery cache round-trips a ``DesignDocChoice`` through a ``WorkflowContext``
  backed by an in-memory store (the doc-independent cache the user asked for).
- ``resolve_design_doc``'s discovered-path and fail-fast no-doc branches
  (``_discover`` stubbed). Supplying a doc explicitly is handled upstream in
  ``cli_pipeline`` (which only calls the finder when no doc was given), so it is
  not exercised here.
- The finder graph itself selects the right file when driven by a fake LLM
  scripting ``list_files -> get_file -> result`` (proves the templated prompt
  builds and the structured BaseModel result reconstructs — no string parsing).

The agent's real file-reading over a live model is covered by the (nightly) Counter
integration tape.
"""

from typing import Any, cast, Literal

from dataclasses import dataclass

import pytest

from collections.abc import Callable, Sequence

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore

from graphcore.graph import Builder, FlowInput

from langgraph.checkpoint.memory import InMemorySaver

from composer.input.files import InMemoryTextFile
from composer.spec.context import WorkflowContext, SourceFields
from composer.spec.service_host import ModelProvider, CoreModelProvider
from composer.spec.util import FS_FORBIDDEN_READ
from composer.templates.loader import load_jinja_template
from composer.ui.autoprove_app import AutoProvePhase
from composer.ui.autoprove_console import AutoProveConsoleHandler
from composer.spec.source.source_env import build_basic_source_tools
from composer.spec.source.design_doc_finder import (
    DESIGN_DOC_DISCOVERY_KEY,
    DesignDocChoice,
    _discover,
    build_finder_graph,
    discovery_cache_key,
    read_document_tool,
    resolve_design_doc,
)
from composer.io.multi_job import run_task, TaskInfo
from composer.spec.source.task_ids import DESIGN_DOC_DISCOVERY_TASK_ID

pytestmark = pytest.mark.asyncio


class _ToolBindingFakeLLM(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` that tolerates ``bind_tools`` (the base raises
    ``NotImplementedError``). It ignores the tools and keeps replaying the scripted
    responses — same trick as the integration tape's ``HarnessFakeLLM``."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self


def _ctx(store: InMemoryStore, cache_ns: tuple[str, ...] | None) -> WorkflowContext[None]:
    return WorkflowContext.create(
        services=lambda _ns: cast(BaseTool, object()),
        thread_id="t",
        store=store,
        recursion_limit=10,
        cache_namespace=cache_ns,
    )


class _StubUploader:
    """Reads text files inline like ``FileUploader.get_document`` does, without the
    Anthropic client (which would need an API key just to construct)."""

    async def get_document(self, path: Any) -> InMemoryTextFile | None:
        import pathlib
        p = pathlib.Path(path)
        if not p.is_file():
            return None
        return InMemoryTextFile(basename=p.name, string_contents=p.read_text(), provider="anthropic")


def _source(
    project_root: str,
    contract_name: str = "C",
    relative_path: str = "src/C.sol",
) -> SourceFields:
    return SourceFields(
        project_root=project_root,
        contract_name=cast(Any, contract_name),
        relative_path=relative_path,
        forbidden_read=FS_FORBIDDEN_READ,
    )


# ---------------------------------------------------------------------------
# discovery cache
# ---------------------------------------------------------------------------


async def test_discovery_cache_key_is_doc_independent_and_stable():
    k1 = discovery_cache_key("/proj", "src/C.sol", "C")
    k2 = discovery_cache_key("/proj", "src/C.sol", "C")
    k3 = discovery_cache_key("/proj", "src/D.sol", "C")
    assert k1 == k2          # deterministic
    assert k1 != k3          # sensitive to the contract path
    assert len(k1) == 16


async def test_discovery_cache_round_trip():
    store = InMemoryStore()
    child = _ctx(store, ("u", "discovery", "abc123")).child(DESIGN_DOC_DISCOVERY_KEY)

    assert await child.cache_get(DesignDocChoice) is None  # cold

    await child.cache_put(DesignDocChoice(selected_path="docs/design.md", reason="clear spec"))

    got = await child.cache_get(DesignDocChoice)
    assert got is not None
    assert got.selected_path == "docs/design.md"
    assert got.reason == "clear spec"


async def test_discovery_cache_disabled_when_no_namespace():
    # caching disabled (cache_ns None) => put is a no-op, get is always None.
    store = InMemoryStore()
    child = _ctx(store, None).child(DESIGN_DOC_DISCOVERY_KEY)
    await child.cache_put(DesignDocChoice(selected_path="x", reason="y"))
    assert await child.cache_get(DesignDocChoice) is None


# ---------------------------------------------------------------------------
# resolve_design_doc
# ---------------------------------------------------------------------------


async def test_resolve_discovered_doc(tmp_path, monkeypatch):
    doc = tmp_path / "docs" / "design.md"
    doc.parent.mkdir()
    doc.write_text("spec")

    async def fake_discover(**_kwargs) -> DesignDocChoice:
        return DesignDocChoice(selected_path="docs/design.md", reason="found it")

    monkeypatch.setattr("composer.spec.source.design_doc_finder._discover", fake_discover)

    path = await resolve_design_doc(
        source=_source(str(tmp_path)),
        uploader=cast(Any, _StubUploader()),
        models=cast(Any, None),
        disc_ctx=cast(Any, None),
    )
    assert path == doc


async def test_resolve_no_doc_fails_fast_with_reason(tmp_path, monkeypatch):
    async def fake_discover(**_kwargs) -> DesignDocChoice:
        return DesignDocChoice(selected_path=None, reason="only a build README here")

    monkeypatch.setattr("composer.spec.source.design_doc_finder._discover", fake_discover)

    with pytest.raises(ValueError, match="only a build README here"):
        await resolve_design_doc(
            source=_source(str(tmp_path)),
            uploader=cast(Any, _StubUploader()),
            models=cast(Any, None),
            disc_ctx=cast(Any, None),
        )


# ---------------------------------------------------------------------------
# finder graph (real graph, fake LLM)
# ---------------------------------------------------------------------------


async def test_finder_graph_selects_the_design_doc(tmp_path):
    (tmp_path / "design.md").write_text("# Design\nThe counter must never decrease.\n")
    (tmp_path / "README.md").write_text("# Build\nRun `forge build`.\n")

    tools = build_basic_source_tools(str(tmp_path), FS_FORBIDDEN_READ).base_source_tools

    # Script: inventory -> read the design doc -> submit the result.
    responses: list[BaseMessage] = [
        AIMessage(content="", tool_calls=[
            {"name": "list_files", "args": {}, "id": "1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "get_file", "args": {"path": "design.md"}, "id": "2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "result",
             "args": {"selected_path": "design.md", "reason": "clear behavioral spec"},
             "id": "3", "type": "tool_call"}]),
    ]
    builder = (
        Builder[None, None, None]()
        .with_llm(_ToolBindingFakeLLM(responses=responses))
        .with_loader(load_jinja_template)
    )
    graph = build_finder_graph(builder, tools, "Counter", "src/Counter.sol")
    state = await graph.ainvoke(
        FlowInput(input=[]),
        config={"configurable": {"thread_id": "u"}, "recursion_limit": 25},
    )
    result = state["result"]
    assert isinstance(result, DesignDocChoice)
    assert result.selected_path == "design.md"
    assert "spec" in result.reason


async def test_finder_graph_can_read_a_pdf_via_read_document(tmp_path):
    """The finder routes a PDF candidate through read_document (which attaches it as a
    user message) and the graph completes with that file selected."""
    (tmp_path / "spec.pdf").write_bytes(b"%PDF-1.4 the protocol specification")

    tools = list(build_basic_source_tools(str(tmp_path), FS_FORBIDDEN_READ).base_source_tools)
    tools.append(read_document_tool(cast(Any, _StubUploader()), str(tmp_path)))

    responses: list[BaseMessage] = [
        AIMessage(content="", tool_calls=[
            {"name": "list_files", "args": {}, "id": "1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "read_document", "args": {"path": "spec.pdf"}, "id": "2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "result",
             "args": {"selected_path": "spec.pdf", "reason": "the protocol specification"},
             "id": "3", "type": "tool_call"}]),
    ]
    builder = (
        Builder[None, None, None]()
        .with_llm(_ToolBindingFakeLLM(responses=responses))
        .with_loader(load_jinja_template)
    )
    graph = build_finder_graph(builder, tools, "Counter", "src/Counter.sol")
    state = await graph.ainvoke(
        FlowInput(input=[]),
        config={"configurable": {"thread_id": "p"}, "recursion_limit": 25},
    )
    assert state["result"].selected_path == "spec.pdf"


async def test_read_document_keeps_tool_results_adjacent_under_parallel_calls(tmp_path):
    """Regression: read_document must put the document INSIDE its tool result, not in a
    separate user message — otherwise, when it's one of several parallel tool calls in
    a turn, the injected message splits the tool results and Anthropic 400s with
    'tool_use ids were found without tool_result blocks immediately after'."""
    (tmp_path / "spec.pdf").write_bytes(b"%PDF-1.4 the spec")
    (tmp_path / "README.md").write_text("# Build\nrun make\n")

    tools = list(build_basic_source_tools(str(tmp_path), FS_FORBIDDEN_READ).base_source_tools)
    tools.append(read_document_tool(cast(Any, _StubUploader()), str(tmp_path)))

    responses: list[BaseMessage] = [
        # One turn, two parallel tool calls — exactly the shape that 400'd in the wild.
        AIMessage(content="", tool_calls=[
            {"name": "read_document", "args": {"path": "spec.pdf"}, "id": "A", "type": "tool_call"},
            {"name": "get_file", "args": {"path": "README.md"}, "id": "B", "type": "tool_call"},
        ]),
        AIMessage(content="", tool_calls=[
            {"name": "result",
             "args": {"selected_path": "spec.pdf", "reason": "the spec"},
             "id": "C", "type": "tool_call"}]),
    ]
    builder = (
        Builder[None, None, None]()
        .with_llm(_ToolBindingFakeLLM(responses=responses))
        .with_loader(load_jinja_template)
    )
    graph = build_finder_graph(builder, tools, "Counter", "src/Counter.sol")
    state = await graph.ainvoke(
        FlowInput(input=[]),
        config={"configurable": {"thread_id": "par"}, "recursion_limit": 25},
    )
    assert state["result"].selected_path == "spec.pdf"

    # Between an assistant tool-call message and the next assistant message there must
    # be ONLY tool results — one per tool_use, and no stray non-tool message.
    msgs = state["messages"]
    for i, m in enumerate(msgs):
        if isinstance(m, AIMessage) and m.tool_calls:
            seen: set[str] = set()
            j = i + 1
            while j < len(msgs) and not isinstance(msgs[j], AIMessage):
                assert isinstance(msgs[j], ToolMessage), (
                    f"non-tool message between tool_use and its results: {type(msgs[j]).__name__}"
                )
                seen.add(msgs[j].tool_call_id)
                j += 1
            assert {tc["id"] for tc in m.tool_calls} <= seen


def _finder_responses() -> list[BaseMessage]:
    return [
        AIMessage(content="", tool_calls=[
            {"name": "list_files", "args": {}, "id": "1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "get_file", "args": {"path": "design.md"}, "id": "2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[
            {"name": "result",
             "args": {"selected_path": "design.md", "reason": "clear behavioral spec"},
             "id": "3", "type": "tool_call"}]),
    ]


@dataclass
class FakeModelFactory:
    fake: BaseChatModel

    @property
    def provider(self):
        return "anthropic"

    def builder_for(self, *args, **kwargs):
        return self.fake

def _fake_models() -> ModelProvider:
    fake = _ToolBindingFakeLLM(responses=_finder_responses())
    return ModelProvider(
        heavy_model=FakeModelFactory(fake),
        lite_model=FakeModelFactory(fake),
        checkpointer=InMemorySaver(),
    )


async def test_discover_surfaces_choice_and_caches(tmp_path, capsys):
    """_discover runs the finder, emits the chosen doc as a progress event, and caches it;
    a second run on the same project + namespace hits the cache and re-surfaces the choice
    without invoking the agent. The caller wraps _discover in a run_task scope (as
    cli_pipeline does) so ``emit_custom_event`` has a handler to render into and the phase
    label is surfaced."""
    (tmp_path / "design.md").write_text("# Design\nThe counter must never decrease.\n")
    store = InMemoryStore()
    disc_ctx = _ctx(store, ("u", "discovery", "k"))
    handler = AutoProveConsoleHandler()

    info = TaskInfo(
        task_id=DESIGN_DOC_DISCOVERY_TASK_ID,
        label="Design Doc Discovery",
        phase=AutoProvePhase.DISCOVER_DESIGN_DOC,
    )
    common = dict(
        source=_source(str(tmp_path), contract_name="Counter", relative_path="src/Counter.sol"),
        uploader=cast(Any, _StubUploader()),
        disc_ctx=disc_ctx,
    )

    # First run: agent discovers and the choice is surfaced at completion.
    choice = await run_task(
        factory=handler.make_handler,
        info=info,
        fn=lambda: _discover(models=_fake_models(), **common),
    )
    assert choice.selected_path == "design.md"
    out = capsys.readouterr().out
    assert "Design Doc Discovery" in out and "discovered design doc: design.md" in out

    # Second run: same project + namespace -> cache hit, no agent. Surfaced as cached.
    # A fresh fake LLM with NO scripted responses would raise if the agent ran.
    no_llm = ModelProvider(
        FakeModelFactory(_ToolBindingFakeLLM(responses=[])),
        FakeModelFactory(_ToolBindingFakeLLM(responses=[])),
        checkpointer=InMemorySaver(),
    )
    choice2 = await run_task(
        factory=handler.make_handler,
        info=info,
        fn=lambda: _discover(models=no_llm, **common),
    )
    assert choice2.selected_path == "design.md"
    out2 = capsys.readouterr().out
    assert "reusing cached design doc: design.md" in out2
