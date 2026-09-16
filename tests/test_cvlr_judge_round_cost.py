"""What one CVLR judge round costs, measured against a real harness.

The rough-draft gate used to be a *completion* validator: the judge composed a verdict, was told it
had never read its draft back, read it, and re-composed the verdict. Measured on the run that
motivated the change, twelve verdicts across two units were produced and discarded that way — the
re-composed text was 0.95–1.00 similar to what had just been thrown out, one pair byte-identical.
So the self-review the gate existed to force was not happening either: the answer was already
written before the draft was ever read.

``write_rough_draft`` now echoes the draft back as its own tool result and stamps the flag, so the
review is the write and there is nothing to reject.
:func:`tests.test_rough_draft_tools.test_writing_a_draft_satisfies_the_completion_gate` pins that
composition for free. This is the other half — the claim that a **real judge, on a real artifact,
therefore emits one verdict instead of two** — and it costs one heavy-tier call rather than a
pipeline run, which is why it is worth having separately from the gate in
``tests/test_cvlr_gate.py``.

**The baseline is free.** It is already in the recording that motivated the change: ``formalize-0``
carried four discarded verdicts across six review rounds and ``formalize-2`` eight across ten. So
only the *after* number needs a model, and only for one round.

The fixture is the harness that run actually delivered — 506 lines, judge-accepted — rather than a
toy, because the thing being measured is how the judge behaves on work substantial enough to reason
about. Run with::

    env -u CERTORA uv run --no-sync pytest tests/test_cvlr_judge_round_cost.py -m expensive -q -s

**Why this is marked ``measurement`` as well as ``expensive``.** A number is worth paying for once;
it is not worth paying for nightly. Its two CVLR siblings skip in CI for a reason of their own —
neither the cargo toolchain nor the Solana platform tools are installed there — but this one needs
no toolchain at all, so nothing stopped it billing a heavy-tier call on every scheduled sweep. The
``measurement`` mark is what stops it: sweeping selections exclude it (see
``.github/workflows/integration-tests.yml``), and naming the file, as above, still runs it. The
composition half of the claim stays free and unconditional in
:func:`tests.test_rough_draft_tools.test_writing_a_draft_satisfies_the_completion_gate`.
"""

import difflib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.io.multi_job import TaskInfo, run_task
import composer.llm.registry as llm_registry
from composer.rag.models import DefaultEmbedder
from composer.spec.context import WorkflowContext
from composer.spec.cvlr.author import build_feedback_thunk
from composer.spec.cvlr.pipeline import CvlrPhase
from composer.spec.cvlr.state import CVLR_JUDGE_KEY, HarnessAssumptions
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.spec.types import PropertyFormulation, PropertyTitle
from composer.testing.record_tape import install_recorder
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import standard_connections

from tests.conftest import MockSentenceTransformer, needs_postgres

pytestmark = [
    pytest.mark.expensive, pytest.mark.measurement, needs_postgres, pytest.mark.asyncio
]

_HARNESS = Path(__file__).parent / "data" / "cvlr_judge" / "vault_lifecycle_initialization.rs"

#: What the recording showed for this same unit: six review rounds, four of which produced a verdict
#: that was rejected and re-composed. Quoted so a reader of a failure has the comparison without
#: going back to the tape.
_RECORDED_DISCARDED_VERDICTS = 4
_RECORDED_ROUNDS = 6


def _properties() -> list[PropertyFormulation]:
    """Two of the properties the recorded run was reviewing, enough for the prompt to be coherent.

    The judge's verdict is not asserted on, so this need not be the full batch — what it must not be
    is empty, since a review of a harness against no stated properties is a different task."""
    return [
        PropertyFormulation(
            title=PropertyTitle("initialize_sets_authority"),
            description=(
                "After initialize, the vault's authority field equals the signer that initialized "
                "it."
            ),
            sort="safety_property",
        ),
        PropertyFormulation(
            title=PropertyTitle("initialize_starts_empty"),
            description="A freshly initialized vault records a balance of zero.",
            sort="safety_property",
        ),
    ]


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-5",
        lite_model="claude-sonnet-5",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    )


def _verdicts(messages: list[AIMessage]) -> list[str]:
    """The feedback text of every ``result`` the judge emitted, in order.

    More than one means a verdict was composed and thrown away — which is the whole measurement.
    """
    return [
        tc["args"]["feedback"]
        for message in messages
        for tc in (message.tool_calls or [])
        if tc["name"] == "result" and "feedback" in tc["args"]
    ]


async def test_one_review_round_emits_one_verdict(langgraph_db, tmp_path, capsys):
    """A judge round composes its verdict once.

    The assertion is deliberately about *count*, not content: what the judge says is the model's
    business and changes run to run, while how many times it says it is a property of the gate. A
    second verdict here means the completion validator rejected the first — the round-trip this
    measures the absence of.

    **What this round is not.** The judge here has no source tools, so it reads the harness and goes
    straight to drafting; a production round first spends a dozen calls exploring the program and
    the CVLR crates (95 ``get_file`` in the recorded unit alone). That head is unchanged by the gate
    and deliberately left out — including it would make the measurement slower, dearer and dominated
    by the part that did not change. What is measured is the tail the gate governs: draft, then
    verdict.

    Measured when this was written: 3 calls — ``get_harness``, ``write_rough_draft`` (1,604 chars),
    ``result`` (5,530 chars) — against the 4-call tail the same unit took before the change (write,
    verdict, *rejected*, read, verdict again).
    """
    draft = _HARNESS.read_text()

    # The recorder is the capture mechanism rather than a bespoke callback: it exists to collect
    # every LLM response the process makes, and reading its lanes back is exactly the question.
    recorder = install_recorder("judge_round_cost", str(tmp_path / "captured.py"))

    summary = RunSummary()
    install_run_summary(summary)
    # Through the module, never `from … import get_provider_for`: the recorder installs itself
    # by replacing that attribute, and a name bound at import time here would keep the original
    # — the model would be real, the capture empty, and the assertion below would read it as
    # "no verdict at all". This is the same binding that made every tape unrecordable.
    tiered = llm_registry.get_provider_for(tiered=cast(Any, _model_args()))
    async with (
        standard_connections(
            provider=tiered.provider_service, embedder=DefaultEmbedder(MockSentenceTransformer())
        ) as conns,
        async_tool_context(),
    ):
        models = ModelProvider(
            heavy_model=tiered.heavy, lite_model=tiered.lite, checkpointer=conns.checkpointer
        )
        env = PureServiceHost(models=models, rag_tools=(), sort="existing").bind_source_tools(())
        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="cvlr_judge_round_cost",
            store=conns.store,
            recursion_limit=100,
            cache_namespace=None,
            memory_namespace=None,
        )
        thunk = build_feedback_thunk(
            ctx.child(CVLR_JUDGE_KEY),
            env,
            _properties(),
            None,
            "vault",
            "cvlr 0.6.1, cvlr-solana 0.5.0",
            (),
        )
        # Inside a task scope, as the authoring loop calls it: that is what installs the IO handler
        # the graph runner requires, and it gives the recorder a named lane instead of parking the
        # calls where ``HarnessFakeLLM`` could never route them.
        async def review():
            return await thunk(
                HarnessAssumptions(summaries=(), munges=()), draft, (), (), "measure"
            )

        feedback = await run_task(
            GenericRustConsoleHandler(set()).make_handler,
            TaskInfo("judge-round-cost", "Judge round", CvlrPhase.FORMALIZATION),
            review,
        )

    captured = [m for lane in recorder.lanes.values() for m in lane]
    verdicts = _verdicts(cast(list[AIMessage], captured))

    with capsys.disabled():
        print(f"\njudge round: {len(captured)} model call(s), {len(verdicts)} verdict(s) composed")
        for n, text in enumerate(verdicts, 1):
            print(f"  verdict {n}: {len(text)} chars")
        if len(verdicts) == 2:
            print(f"  similarity: {difflib.SequenceMatcher(None, *verdicts).ratio():.2f}")
        print(f"  accepted: {feedback.good}")
        print(
            f"\nrecorded baseline for this unit (pre-change): {_RECORDED_DISCARDED_VERDICTS} "
            f"discarded verdicts over {_RECORDED_ROUNDS} rounds"
        )

    assert verdicts, "the judge produced no verdict at all"
    assert len(verdicts) == 1, (
        f"the judge composed {len(verdicts)} verdicts for one review round. More than one means the "
        f"completion validator rejected the first — the round-trip write_rough_draft's echo exists "
        f"to remove. Similarity between them: "
        f"{difflib.SequenceMatcher(None, verdicts[0], verdicts[1]).ratio():.2f}"
    )
