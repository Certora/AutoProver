"""CVLR research sub-agent: answers questions about CVLR out of the two corpora.

The peer of :mod:`composer.spec.cvl_research`, and deliberately close to it — divergence between
two agents doing the same job costs review attention that neither earns. What differs is the two
things CVLR has that CVL does not.

**Two corpora, and an ordering between them.** ``cvlr_api_kb`` is generated from the CVLR crates
and compile-gated; ``cvlr_kb`` is the Solana Prover manual, hand-written and allowed to
lag. A retrieval hit carries no provenance, so the ordering cannot live in the rows — it lives
in this agent, which holds both tool sets and is told which wins. That is what the source mount
used to do by being the code (``docs/cvlr-api-docs-plan.md`` §5).

**Nothing run-specific reaches it.** The version pin (§2) means the corpus describes exactly the
releases the build resolves, and ``--withhold-crate`` is a statement about the *target* rather than
about what CVLR this run may know (§2.3). So the researcher takes no per-run input beyond the
question — which is the precondition for the cross-run answer cache, and the cache is not optional:
the live run's tool census recorded 50 crate-source calls in a single run, and each one of those
becomes a sub-agent invocation unless it is answered from the index.
"""

from typing import Any, NotRequired, TypedDict, override

from dataclasses import dataclass

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field

from graphcore.graph import Builder, FlowInput
from graphcore.tools.schemas import WithInjectedId

from composer.kb.kb_context import with_cvlr_facts
from composer.spec.agent_index import (
    AgentIndex,
    AgentIndexConfig,
    IndexedTool,
    RetrieveDocumentTool,
)
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.service_host import ModelProvider
from composer.spec.util import uniq_thread_id
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools
from composer.ui.tool_display import CommonTools, tool_display_of

#: Where cached answers live. Shared across runs on purpose: the pin and §2.3 together mean an
#: answer about CVLR is a fact about the pinned release, not about the run that asked.
DEFAULT_CVLR_AGENT_INDEX_NS: tuple[str, ...] = ("cvlr_research", "cached")

CVLR_RESEARCH_BASE_DOC = (
    "Delegate a question about CVLR — the Rust specification language the Certora Solana Prover "
    "checks — to a research sub-agent. It searches the generated CVLR API reference and the CVLR "
    "manual, then delivers one synthesized answer.\n\n"
    "Use it for what exists and how to write it: whether a helper exists, what its exact "
    "signature is, which crate defines it, what a macro takes, how to express something in a "
    "rule. It reads the CVLR crates' own documentation, so it is a better authority on the "
    "language than your recollection of it.\n\n"
    "It knows nothing about the program under verification and nothing about your other tools — "
    "ask it about CVLR only. An answer of 'I could not establish that' is a real answer: it means "
    "the corpus does not support the claim, not that the question should be retried."
)


class CvlrResearchSysParams(TypedDict):
    """What the system prompt renders.

    ``context_instructions`` is the index's own preamble, present only when the researcher is
    indexed — which it always is here, but the template fuzzer constructs both, so the parameter
    is declared rather than assumed.
    """

    context_instructions: str | None


_ResearchSys = TypedTemplate[CvlrResearchSysParams]("cvlr_research_system_prompt.j2")


class _CvlrResearchInput(FlowInput, RoughDraftState):
    pass


class _CvlrResearchST(MessagesState, RoughDraftState):
    result: NotRequired[str]


_CompiledResearchGraph = CompiledStateGraph[_CvlrResearchST, None, _CvlrResearchInput, Any]


def _wrote_draft(s: _CvlrResearchST, _: object) -> str | None:
    """A draft before delivering, which is what keeps the answer grounded.

    The failure this prevents is the one the corpus exists to prevent: an answer assembled from
    recall that happens to read like a retrieval result.
    """
    if not s.get("drafted"):
        return "You must write a rough draft before delivering your answer"
    return None


def _build_research_graph(
    builder: Builder[None, None, None],
    corpus_tools: tuple[BaseTool, ...],
    with_index: bool,
) -> _CompiledResearchGraph:
    sys_templ = _ResearchSys.bind(
        CvlrResearchSysParams(
            context_instructions=AgentIndex.WITH_INDEX_SYS_COMMON if with_index else None
        )
    )
    return (
        bind_standard(builder, _CvlrResearchST, "Your research findings", validator=_wrote_draft)
        .with_input(_CvlrResearchInput)
        .with_tools(corpus_tools)
        .with_tools(get_rough_draft_tools(_CvlrResearchST))
        .inject(lambda g: sys_templ.render_to(g.with_sys_prompt_template))
        .with_initial_prompt(with_cvlr_facts("Answer the following question"))
        .compile_async()
    )


class CvlrResearchSchemaBase(BaseModel):
    question: str = Field(
        description="A specific question about CVLR. "
        "Good: 'What is the signature of cvlr_deserialize_nondet_accounts?' "
        "Good: 'How do I give an account field a nondeterministic value?' "
        "Good: 'Does cvlr-mathint have an unsigned max helper?' "
        "Bad: 'How does the withdraw handler work?' (that is about the program, not about CVLR)"
    )


@dataclass(frozen=True)
class _Researcher:
    """The compiled graph plus how a call into it is run."""

    graph: _CompiledResearchGraph
    recursion_limit: int

    async def answer(self, question: str, context: list[str], within_tool: str | None) -> str:
        state = await run_to_completion(
            graph=self.graph,
            context=None,
            description="CVLR research",
            thread_id=uniq_thread_id("cvlr-research"),
            recursion_limit=self.recursion_limit,
            input=_CvlrResearchInput(input=[question, *context], drafted=False, memory=None),
            within_tool=within_tool,
        )
        assert "result" in state
        return state["result"]


def cvlr_research_tools(
    models: ModelProvider,
    corpus_tools: tuple[BaseTool, ...],
    store: BaseStore,
    *,
    recursion_limit: int,
    index_config: AgentIndexConfig,
    doc: str = CVLR_RESEARCH_BASE_DOC,
) -> tuple[BaseTool, ...]:
    """The researcher and its document-ref companion, ready to bind onto an agent.

    Indexed from the start, where CVL's equivalent treats that as optional. The author asks the
    same API questions across units, across rounds and from the judge, and every one of them is
    otherwise a fresh sub-agent invocation — the traffic this replaces was a local grep.

    Runs on the lite tier, as CVL's does: this is a retrieval-and-synthesis job over a corpus that
    already holds the answer, not a reasoning one.
    """
    index = AgentIndex(store=store, config=index_config)
    researcher = _Researcher(
        graph=_build_research_graph(models.builder_lite(), corpus_tools, with_index=True),
        recursion_limit=recursion_limit,
    )

    @tool_display_of(CommonTools.cvlr_research)
    class CvlrResearcher(CvlrResearchSchemaBase, IndexedTool[AgentIndex], WithInjectedId):
        __doc__ = doc

        @override
        def get_question(self) -> str:
            return self.question

        @override
        async def answer_question(self, context: list[str]) -> str:
            return await researcher.answer(self.question, context, self.tool_call_id)

    return (
        CvlrResearcher.bind(index).as_tool("cvlr_research"),
        RetrieveDocumentTool.bind(index).as_tool("cvlr_document_ref"),
    )
