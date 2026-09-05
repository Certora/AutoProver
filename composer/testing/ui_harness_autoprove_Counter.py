"""
Fake-LLM end-to-end UI harness for ``tui_autoprove.py`` (auto-prove
multi-agent pipeline).

Substitutes the real ``ChatAnthropic`` built via
``composer.llm.registry.get_provider_for(...).builder_for(...)`` with a
``FakeMessagesListChatModel`` preloaded with a hand-authored tape of
responses. Every other part of the pipeline runs normally — ``AutoProveApp``
TUI, real tool execution (solc, Typechecker.jar, certoraTypeCheck.py,
the real Certora prover, PreAudit subprocess), workflow graphs,
checkpointing, Postgres-backed store/checkpointer, RAG.

Scenario inputs and wiring instructions live under
``composer/testing/scenarios/autoprove_counter/``.

The scenario is deliberately constrained to one contract with one component
so that the per-component ``asyncio.gather`` fan-outs in the extraction and
CVL phases collapse to a single lane each. Multiple invariants and multiple
properties are still authored per-phase — a single authoring agent services
them sequentially, so each lane stays linear.

``AutoProveTaskHandler.format_hitl_prompt`` raises ``NotImplementedError``
— there is no Textual-side HITL prompt in this pipeline. The interactive
post-bug-analysis *refinement conversation* is a different mechanism: it
runs through a ``RichConsoleConversationClient`` outside the Textual
screen and consumes plain-text human input from stdin. **Run the pipeline
with ``--interactive``** to exercise it — the refinement conversation's four
tape entries live in the ``extract-0`` lane, after the property-extraction
entries. Routing is per-lane now, so skipping ``--interactive`` just leaves
those entries unconsumed rather than corrupting another phase. Every
expected human reply is embedded as a ``[TAPE EXPECTATION: respond ...]``
marker inside the preceding AI message so the operator running the harness
knows what to type.

Lanes and call order
--------------------
The pipeline runs several phases concurrently (``asyncio.gather``), so there
is no single global call order any more. ``HarnessFakeLLM`` routes each call
to a per-phase *lane* keyed by the ``run_task`` task_id (read from the
``get_current_task_id`` ContextVar that ``run_task`` sets). Within a lane the
calls happen in the order authored below; sub-agents (invariant_feedback, CEX
analyzer, cvl_research, code_explorer) inherit their parent phase's task_id,
so their responses live in the parent's lane.

    system-analysis : run_component_analysis (+ code_explorer sub-agent)
    harness         : run_harness_creation / classifier_agent
    autosetup       : run_autosetup_phase — a subprocess, makes NO LLM calls,
                      so it has no lane
    ── after harness creation, these lanes run concurrently ──
    invariants       : get_invariant_formulation (+ invariant_feedback ×3)
    extract-0        : run_property_inference (+ refinement when --interactive)
    ── staged CVL join, after the concurrent branch completes ──
    invariant-cvl    : batch_cvl_generation, component=None
                        (+ cvl_research, code_explorer, feedback ×2, CEX ×1)
    formalize-0      : batch_cvl_generation, component=<one>
                        (+ feedback ×1, CEX ×1 — surfaces the real
                        ``incrementOther`` implementation bug)
    ── final, best-effort report phase ──
    report           : build_report → call_grouping_llm (one structured-output
                        call partitioning the formalized properties into groups)

    Per-component lanes are ``extract-{component index}`` / ``formalize-{component
    index}`` (from ``pipeline.core``); the Counter has a single component, index 0.
"""

from typing import Any
import uuid

from composer.testing.harness_tape import HarnessFakeLLM, install_fake_llm
from composer.spec.source.prover import STUCK_RULE_NAG_THRESHOLD
from composer.spec.source.task_ids import (
    DESIGN_DOC_DISCOVERY_TASK_ID,
    SYSTEM_ANALYSIS_TASK_ID, HARNESS_TASK_ID, INVARIANTS_TASK_ID,
    INVARIANT_CVL_TASK_ID, REPORT_TASK_ID,
)
from composer.pipeline.core import extract_task_id, formalize_task_id

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.tool import ToolCall


def _tc(name: str, **args: Any) -> ToolCall:
    """Tool-call dict with a unique ``id`` (LangGraph binds tool responses back
    to calls by id, so every entry needs its own)."""
    return {
        "id": f"toolu_{uuid.uuid4().hex[:20]}",
        "name": name,
        "args": args,
        "type": "tool_call",
    }


def _ai(text: str = "", *tool_calls: ToolCall) -> AIMessage:
    """Build a tape entry: optional text + zero or more tool_calls. LangGraph's
    agent loop transitions to the tools node when ``tool_calls`` is non-empty,
    and to END (returning to output_key extraction) otherwise."""
    content: list[str | dict] = []
    if text:
        content.append(text)
    content.extend(
        {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]}
        for t in tool_calls
    )
    return AIMessage(content=content, tool_calls=list(tool_calls))


# ---------------------------------------------------------------------------
# Scenario artifacts (Solidity + CVL)
# ---------------------------------------------------------------------------
#
# The Solidity source is staged on disk in
# ``composer/testing/scenarios/autoprove_counter/src/Counter.sol``. These CVL
# strings are emitted as ``put_cvl_raw`` arguments during the invariant-CVL
# and component-CVL phases. Real tools validate them:
#
#   - Typechecker.jar  — gatekeeps ``put_cvl_raw`` (rejects parse errors).
#   - Certora prover   — gatekeeps ``verify_spec`` (proves or CEXes).


# Intentionally malformed surface-syntax CVL. Triggers the Typechecker.jar
# rejection path on the first ``put_cvl_raw`` of the invariant-CVL phase;
# the tape's next turn resubmits valid CVL.
BROKEN_PARSE_CVL = """\
invariant not_valid_cvl()
    this is definitely not valid CVL syntax;
"""

# Typechecks but the invariant is obviously false: after ``increment()`` runs,
# ``count`` is 1, so ``count == 0`` no longer holds. Used as the first
# (easy-to-catch) semantic-error candidate — the feedback judge rejects this
# on first pass without involving the prover at all.
BAD_INV_CVL = """\
invariant increments_sum_is_count() currentContract.count == 0;
"""

# Typechecks and declares the two ostensibly-correct invariant names, but the
# ``increments_sum_is_count`` is subtly wrong; without an init state axiom, the
# prover can choose an initial value of incrementsSum that violates the base case.
# The feedback judge approves by name-coverage; the prover catches it on the
# base case (initial state has ``count == 0``, violating ``count > 0``).
# This is the artifact that drives the verify_spec → analyze_cex_raw round-trip
# in the tape — exactly one failing rule (``count_nonneg``), so exactly one
# CEX LLM call is consumed.
SUBTLE_INV_CVL = """\
ghost uint256 incrementsSum;

hook Sstore currentContract.increments[KEY address who] uint256 newValue (uint256 oldValue) {
	incrementsSum = require_uint256(incrementsSum + (newValue - oldValue));
}

invariant zero_address_is_zero() currentContract.increments[0] == 0;

invariant increments_sum_is_count() currentContract.count == incrementsSum;
"""

# Two trivially-true invariants over the Counter state. Both should verify
# against Counter.sol on first try, so verify_spec stamps the prover digest
# and the author can call `result` to terminate the invariant-CVL author graph.
GOOD_INV_CVL = """\
ghost uint256 incrementsSum {
	init_state axiom incrementsSum == 0; 
}

hook Sstore currentContract.increments[KEY address who] uint256 newValue (uint256 oldValue) {
	incrementsSum = require_uint256(incrementsSum + (newValue - oldValue));
}

invariant zero_address_is_zero() currentContract.increments[0] == 0;

invariant increments_sum_is_count() currentContract.count == incrementsSum;
"""

# Component-CVL spec: three rules covering all three extracted properties.
# The first two rules verify on the first prover run. The third rule —
# ``incrementOther_credits_target_when_distinct`` — CEXes against
# ``Counter.incrementOther`` (which has a real off-target bug: it credits
# ``msg.sender`` instead of ``other``). The tape responds to that CEX by
# calling ``expect_rule_failure`` to mark the rule as surfacing a real
# implementation bug, then re-runs ``verify_spec`` with the rule excluded.
COMPONENT_CVL = """\
methods {
    function count() external returns (uint256) envfree;
    function increments(address) external returns (uint256) envfree;
    function increment() external;
    function incrementOther(address) external;
}

rule increment_increases_count {
    env e;
    mathint before = count();
    increment(e);
    assert to_mathint(count()) == before + 1,
        "increment() must increase count by exactly 1";
}

rule increment_increases_sender_tally {
    env e;
    address s = e.msg.sender;
    mathint before = increments(s);
    increment(e);
    assert to_mathint(increments(s)) == before + 1,
        "increment() must increase increments[msg.sender] by exactly 1";
}

rule incrementOther_credits_target_when_distinct {
    env e;
    address other;
    require other != 0;
    require other != e.msg.sender;
    mathint before_other = increments(other);
    incrementOther(e, other);
    assert to_mathint(increments(other)) == before_other + 1,
        "incrementOther(other) must increase increments[other] by exactly 1 when other != msg.sender";
}
"""


# ---------------------------------------------------------------------------
# SourceApplication payload — emitted by the component-analysis result tool
# ---------------------------------------------------------------------------
#
# Shape must satisfy pydantic validation of
# ``composer.spec.system_model.SourceApplication`` AND the
# ``validate_solidity_connectivity`` validator: unique names, all referenced
# components / external actors exist, and every declared ``path`` names a real
# file under the scenario's project root. One SourceExplicitContract ("Counter")
# with one ContractComponent ("Increment"), no interactions, no external
# actors — minimal valid shape.

_APP_RESULT = {
    "application_type": "Counter",
    "description": (
        "A minimal singleton Counter application that maintains a global "
        "count and a per-caller tally of invocations via two external "
        "entry points (``increment`` and ``incrementOther``)."
    ),
    "components": [
        {
            "sort": "singleton",
            "name": "Counter",
            "path": "src/Counter.sol",
            "description": (
                "The only contract in the system; owns the count and per-"
                "caller tally state and the two increment entry points."
            ),
            "solidity_identifier": "Counter",
            "components": [
                {
                    "name": "Increment",
                    "description": (
                        "Handles all count updates through the "
                        "``increment()`` and ``incrementOther(address)`` "
                        "external entry points."
                    ),
                    "external_entry_points": [
                        "increment()", "incrementOther(address)"
                    ],
                    "state_variables": [
                        "uint256 count",
                        "mapping(address => uint256) increments",
                    ],
                    "interactions": [],
                    "requirements": [
                        "Each call to increment() increases count by exactly 1.",
                        "Each call to increment() increases increments[msg.sender] by exactly 1.",
                        "Each call to incrementOther(other) increases increments[other] by exactly 1.",
                        "increment() must not revert under normal operation.",
                    ],
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# AgentSystemDescription payload — emitted by the classifier-agent result tool
# ---------------------------------------------------------------------------
#
# Shape must satisfy pydantic validation of
# ``composer.spec.source.harness.AgentSystemDescription`` AND the
# ``classifier_agent`` validator: every ``transitive_closure[*].solidity_identifier``
# must map to a known SourceExplicitContract, every ``external_interfaces[*].name``
# must map to a known SourceExternalActor (with a path).
#
# We use ``harness_determination=None`` (→ ``num_instances`` None) so
# ``needs_harnessing()`` returns False and the harness-generation sub-agent is
# skipped. ``erc20_contracts=[]`` and ``external_interfaces=[]`` so the
# summaries sub-agent is skipped.

_CLASSIFIER_RESULT = {
    "non_trivial_state": (
        "A non-trivial state has been reached once at least one call to "
        "increment() has executed: count > 0 and increments[msg.sender] > 0 "
        "for that sender."
    ),
    "transitive_closure": [
        {
            "link_fields": [],
            "harness_determination": None,
            "solidity_identifier": "Counter"
        }
    ],
    "erc20_contracts": [],
    "external_interfaces": [],
}


# ---------------------------------------------------------------------------
# PropertyFormulation payloads — emitted by the bug-analysis result tool
# ---------------------------------------------------------------------------
#
# The bug-analysis agent's result schema is ``list[PropertyFormulation]``
# wrapped via the ``(type, doc)`` overload of ``result_tool_generator``, so
# the tool args are ``{"value": [...]}``.

_BUG_ANALYSIS_PROPS = [
    {
        "title": "count_increments_by_one",
        "methods": ["increment()"],
        "sort": "safety_property",
        "description": (
            "After calling increment(), the global count must be exactly "
            "one greater than before the call."
        ),
    },
    {
        "title": "sender_increments_by_one",
        "methods": ["increment()"],
        "sort": "safety_property",
        "description": (
            "After calling increment(), increments[msg.sender] must be "
            "exactly one greater than before the call."
        ),
    },
    {
        "title": "other_increments_by_one",
        "methods": ["incrementOther(address)"],
        "sort": "safety_property",
        "description": (
            "After calling incrementOther(other), increments[other] must "
            "be exactly one greater than before the call."
        ),
    },
]


# After the user works through the refinement conversation, the AI is asked
# to refine property 3 (incrementOther) so that it is explicit about which
# storage slot is supposed to move and which is supposed to stay put. The
# updated list is what eventually feeds the component-CVL phase.

_REFINED_BUG_ANALYSIS_PROPS = [
    {
        "title": "count_increments_by_one",
        "methods": ["increment()"],
        "sort": "safety_property",
        "description": (
            "After calling increment(), the global count must be exactly "
            "one greater than before the call."
        ),
    },
    {
        "title": "sender_increments_by_one",
        "methods": ["increment()"],
        "sort": "safety_property",
        "description": (
            "After calling increment(), increments[msg.sender] must be "
            "exactly one greater than before the call."
        ),
    },
    {
        "title": "other_increments_by_one",
        "methods": ["incrementOther(address)"],
        "sort": "safety_property",
        "description": (
            "After calling incrementOther(other) with other != msg.sender, "
            "increments[other] must increase by exactly 1 and "
            "increments[msg.sender] must be unchanged."
        ),
    },
]


# ---------------------------------------------------------------------------
# The tape
# ---------------------------------------------------------------------------
#
# Authored as one list per phase ("lane"), assembled into the per-lane
# ``_AUTOPROVE_TAPE`` dict at the bottom. HarnessFakeLLM serves each LLM call
# from its lane's cursor (keyed by run_task task_id), so the scripted responses
# stay correct even though the pipeline runs phases concurrently. Within a lane,
# entries are popped in order; if the pipeline issues a call the lane doesn't
# have, the fake raises. Editing the tape is the cheap loop.

_SYSTEM_ANALYSIS_TAPE: list[BaseMessage] = [

    # ───────────────────────────────────────────────────────────────────
    # P1. Component analysis (run_component_analysis → SourceApplication)
    # ───────────────────────────────────────────────────────────────────
    # Tools available: memory, write_rough_draft, read_rough_draft,
    #   source_tools = list_files, get_file, grep_files, code_explorer,
    #                  code_document_ref.
    # Validator: validate_solidity_connectivity (graph wellformedness plus the
    #   existence of every declared source path; no did_read requirement — we can
    #   hit `result` at any time once the application shape is correct).

    # P1.1 — exercise memory + list_files + get_file. Memory paths must sit
    # under /memories; `view /memories` is the harmless exercise.
    _ai(
        "Cataloguing memory and surveying the project layout.",
        _tc("memory", command="view", path="/memories"),
        _tc("list_files"),
        _tc("get_file", path="src/Counter.sol"),
    ),

    # P1.2 — exercise grep_files. Returns matches for `increment` in the
    # source; the agent uses the result to narrow understanding.
    _ai(
        "Grepping for the entry point symbol.",
        _tc(
            "grep_files",
            search_string="increment",
            matching_lines=False,
        ),
    ),

    # P1.3 — exercise code_explorer. This spawns the code-explorer sub-agent
    # (CE.1..CE.2 below). The indexed variant caches by normalized question
    # hash; subsequent code_explorer calls with the same question return
    # without an LLM call, so we only pay for it here. Tool is registered
    # as ``code_explorer`` by ``indexed_code_explorer_tool`` — note the
    # ``source_displays()`` mapping uses the stale key ``explore_code``,
    # but the tool itself is dispatched under ``code_explorer``.
    _ai(
        "Delegating a state-shape question to the code-explorer sub-agent.",
        _tc(
            "code_explorer",
            question=(
                "What storage state does the Counter contract maintain, and "
                "which function modifies it?"
            ),
        ),
    ),

    # CE.1 — code-explorer sub-agent turn 1. Tools: base_source_tools
    # (list_files, get_file, grep_files) + result. The sub-agent has no
    # memory/rough_draft tools (see composer/spec/code_explorer.py).
    _ai(
        "Explorer: inspecting Counter.sol.",
        _tc("get_file", path="src/Counter.sol"),
    ),

    # CE.2 — code-explorer result. Schema is (str, "Your findings about
    # the source code"), so args are {"value": "..."}.
    _ai(
        "Explorer: findings ready.",
        _tc(
            "result",
            value=(
                "Counter stores `uint256 public count` and "
                "`mapping(address => uint256) public increments`. Both are "
                "mutated by the single external entry point `increment()`, "
                "which adds 1 to `count` and 1 to `increments[msg.sender]`."
            ),
        ),
    ),

    # P1.4 — exercise rough_draft tools before result. No did_read validator
    # in this phase, so the order is just for coverage.
    _ai(
        "Drafting a one-paragraph summary for self-reference.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Counter is a singleton with one component (Increment). "
                "State: count (uint256) + increments (address→uint256). "
                "One entry: increment(). No external interactions."
            ),
        ),
    ),
    _ai(
        "Reading back the draft before emitting the application model.",
        _tc("read_rough_draft"),
    ),

    # P1.5 — emit the SourceApplication. Satisfies validate_solidity_connectivity
    # (unique names, no dangling interaction references since there are no
    # interactions, and a declared path that exists in the scenario tree).
    _ai(
        "Application model ready.",
        _tc("result", **_APP_RESULT),
    ),

    # ───────────────────────────────────────────────────────────────────
    # P2. Classifier agent (run_harness_creation → classifier_agent →
    #     AgentSystemDescription)
    # ───────────────────────────────────────────────────────────────────
    # Tools available: memory, source_tools, result.
    # Validator: every transitive_closure[*].name must be a known
    #   SourceExplicitContract and every external_interfaces[*].name must
    #   be a known SourceExternalActor with a non-None path. We return zero
    #   external interfaces and only "Counter" in the closure.
    #
    # After this result, `needs_harnessing()` returns False
    # (num_instances=None) so generate_harnesses is skipped. Empty erc20 +
    # empty external_interfaces means setup_summaries is skipped by
    # `run_autoprove_pipeline`.
    #
    # The preaudit subprocess runs between this phase and the invariant
    # phase — it's a real `python -m orchestrator` call and does not
    # consume LLM calls.

]

_HARNESS_TAPE: list[BaseMessage] = [

    # P2.1 — exercise list_files in this agent's thread (different from
    # the P1 thread, so the listing call re-runs against the real fs).
    _ai(
        "Classifier: surveying project contents before classifying.",
        _tc("list_files"),
    ),

    # P2.2 — emit the AgentSystemDescription. Empty external_interfaces +
    # empty erc20_contracts + num_instances=None short-circuits the next
    # two pipeline phases (harnessing + summaries).
    _ai(
        "Counter is standalone — no harnessing, no external summaries.",
        _tc("result", **_CLASSIFIER_RESULT),
    ),

    # ───────────────────────────────────────────────────────────────────
    # P3. Structural invariant formulation (get_invariant_formulation)
    # ───────────────────────────────────────────────────────────────────
    # Main-agent tools: memory, source_tools, invariant_feedback, result.
    # Feedback sub-agent tools: memory, rough_draft, source_tools, result
    #   (schema: InvariantFeedback{sort, explanation}).
    # Validator `_validate_invariants`: every inv in the final result must
    #   appear in state["invariant_data"] with (description, "GOOD") matching
    #   exactly. The state dict merges on name, so resubmitting the same
    #   name with a different description overwrites the prior entry.
    #
    # The tape uses 3 invariant_feedback rounds (1 bad + 2 good) to exercise
    # the NOT_INDUCTIVE → resubmit recovery path, and delivers 2 invariants
    # in the final result.

]

_INVARIANTS_TAPE: list[BaseMessage] = [

    # P3.1 — exercise source_tools in the main invariant agent.
    _ai(
        "Reading Counter.sol to understand the state shape.",
        _tc("get_file", path="src/Counter.sol"),
    ),

    # P3.2 — first invariant_feedback call: candidate "count_zero" (count is
    # always 0) — intentionally bad. This spawns F1.{1-3}.
    _ai(
        "Proposing count_zero as a structural candidate.",
        _tc(
            "invariant_feedback",
            inv={
                "name": "count_zero",
                "description": "The global count is always zero.",
            },
        ),
    ),

    # F1.1 — invariant feedback judge, first invocation, turn 1. Judge tools:
    # memory, rough_draft, source_tools, result. Validator on this sub-agent
    # is the standard `bind_standard` without custom checks — the only
    # implicit requirement is providing `result` to set output_key.
    _ai(
        "Judge: inspecting the source + drafting a verdict.",
        _tc("get_file", path="src/Counter.sol"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "count_zero claims count is always 0, but increment() "
                "mutates count upward. The post-state of any increment() "
                "call already violates this claim. Verdict: NOT_INDUCTIVE."
            ),
        ),
    ),

    # F1.2 — judge: read the draft before emitting result.
    _ai(
        "Judge: re-reading the draft.",
        _tc("read_rough_draft"),
    ),

    # F1.3 — judge: NOT_INDUCTIVE verdict. This stores
    # state["invariant_data"]["count_zero"] = ("The global count is always
    # zero.", "NOT_INDUCTIVE"). The main agent sees the ToolMessage and can
    # try a different candidate.
    _ai(
        "Judge: delivering NOT_INDUCTIVE verdict.",
        _tc(
            "result",
            sort="NOT_INDUCTIVE",
            explanation=(
                "The claim fails immediately after any call to increment(): "
                "count transitions from k to k+1 and the invariant does not "
                "hold in the post-state. Consider a non-negativity "
                "invariant (count >= 0) or a correlation between count and "
                "the increments mapping instead."
            ),
        ),
    ),

    # P3.3 — main agent resubmits with a stronger invariant name:
    # "count_nonneg" (trivially true on uint256). Spawns F2.{1-3}.
    _ai(
        "Addressing the feedback — proposing count_nonneg instead.",
        _tc(
            "invariant_feedback",
            inv={
                "name": "increments_sum_is_count",
                "description": (
                    "`count` is the sum of all values in the `increments` map"
                ),
            },
        ),
    ),

    # F2.1 — judge, second invocation, turn 1.
    _ai(
        "Judge: evaluating count_nonneg.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Sums can be reasoned about in CVL. Formal and inductive. Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    # F2.3 — GOOD verdict. Stamps state["invariant_data"]["count_nonneg"].
    _ai(
        "Judge: GOOD verdict on increments_sum_is_count.",
        _tc(
            "result",
            sort="GOOD",
            explanation=(
                "The invariant is inductive and formalizable."
            ),
        ),
    ),

    # P3.4 — main agent proposes second invariant. Spawns F3.{1-3}.
    _ai(
        "Proposing the second invariant.",
        _tc(
            "invariant_feedback",
            inv={
                "name": "zero_address_is_zero",
                "description": (
                    "The zero address' `increments` value is always 0."
                ),
            },
        ),
    ),

    # F3.1 — judge, third invocation.
    _ai(
        "Judge: evaluating zero_address_is_zero.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Trivially implied by the implementation, "
                "but formal and inductive. Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    _ai(
        "Judge: GOOD verdict on zero_address_is_zero.",
        _tc(
            "result",
            sort="GOOD",
            explanation=(
                "The invariant is trivially true"
            ),
        ),
    ),

    # P3.5 — main agent delivers both invariants. Descriptions must match
    # the ones in state["invariant_data"] verbatim (merged on name).
    _ai(
        "Delivering the validated invariants.",
        _tc(
            "result",
            inv=[
                {
                    "name": "increments_sum_is_count",
                    "description": (
                        "`count` is the sum of all values in the `increments` map"
                    ),
                },
                {
                    "name": "zero_address_is_zero",
                    "description": (
                        "The zero address' `increments` value is always 0."
                    ),
                },
            ],
        ),
    ),

    # ───────────────────────────────────────────────────────────────────
    # P4. Invariant CVL generation (batch_cvl_generation, component=None)
    # ───────────────────────────────────────────────────────────────────
    # Author-agent tools:
    #   - cvl_authorship_tools (source_tools + rag_tools): list_files,
    #     get_file, grep_files, code_explorer, code_document_ref,
    #     cvl_manual_search, cvl_keyword_search, get_cvl_manual_section,
    #     get_cvl_recipe, cvl_research, cvl_document_ref.
    #   - static_tools: put_cvl, put_cvl_raw, feedback_tool, record_skip,
    #     unskip_property, get_cvl, erc20_guidance, unresolved_call_guidance.
    #   - prover_tool: verify_spec.
    #   - ExpectRuleFailure.as_tool("expect_rule_failure"),
    #     ExpectRulePassage.as_tool("expect_rule_passage").
    #   - result (str commentary), memory.
    #
    # Result digest: validations[feedback] AND validations[prover] must
    # both equal digest(curr_spec, skipped) before `result` is accepted.
    # feedback_tool (good=True) stamps feedback; verify_spec (rules=None,
    # all_verified) stamps prover. Any put_cvl_raw / record_skip /
    # unskip_property invalidates both stamps.
    #
    # 2 invariants — record_skip / unskip_property accept the property titles
    # `increments_sum_is_count` and `zero_address_is_zero`.

]

_INVARIANT_CVL_TAPE: list[BaseMessage] = [

    # Q1 — exercise the similarity + keyword search paths.
    _ai(
        "Surveying the CVL manual for invariant patterns.",
        _tc(
            "cvl_manual_search",
            question=(
                "What is the syntax for declaring a parametric invariant "
                "in CVL?"
            ),
            similarity_cutoff=0.5,
            max_results=5,
            manual_section=[],
        ),
        _tc("cvl_keyword_search", query="invariant", min_depth=0, limit=5),
    ),

    # Q2 — exercise section retrieval + recipe retrieval (hit path).
    _ai(
        "Fetching the Invariants section and retrieving a recipe.",
        _tc("get_cvl_manual_section", headers=["Invariants"]),
        _tc("get_cvl_recipe", id="R1"),
    ),

    # Q3 — exercise the recipe miss path + both guidance tools + memory view.
    # The recipe id is expected to miss — the harness only cares
    # about exercising the tool dispatch, not the result value.
    _ai(
        "Checking for a recipe and pulling guidance.",
        _tc("get_cvl_recipe", id="R99"),
        _tc("erc20_guidance"),
        _tc("unresolved_call_guidance"),
        _tc("memory", command="view", path="/memories"),
    ),

    # Q4 — delegate a CVL-syntax question to the research sub-agent.
    # Spawns CR.{1-3}.
    _ai(
        "Delegating an invariant-syntax question to the researcher.",
        _tc(
            "cvl_research",
            question=(
                "What is the correct syntax to write an invariant over a "
                "single top-level uint256 storage field using "
                "currentContract?"
            ),
        ),
    ),

    # CR.1 — research sub-agent, turn 1. Tools: write_rough_draft,
    # read_rough_draft, base_rag_tools (cvl_manual_*, kb_*), result.
    # Validator `_did_rough_draft_read` rejects result until did_read=True.
    _ai(
        "Researcher: sketching an answer + pulling the manual section.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Plan: quote the parametric-invariant syntax from the "
                "Invariants section of the manual. Give a worked example "
                "against a uint256 storage field called `count`."
            ),
        ),
        _tc(
            "cvl_manual_search",
            question="invariant syntax currentContract storage field",
            similarity_cutoff=0.5,
            max_results=5,
            manual_section=[],
        ),
    ),

    # CR.2 — research: read the draft so did_read flips true.
    _ai(
        "Researcher: reading the draft before answering.",
        _tc("read_rough_draft"),
    ),

    # CR.3 — research result. Schema is (str, "Your research findings"), so
    # args are {"value": "..."}.
    _ai(
        "Researcher: answer ready.",
        _tc(
            "result",
            value=(
                "An invariant over a single storage field uses the form:\n"
                "  invariant <name>()\n"
                "      currentContract.<field> <relational-op> <expr>;\n"
                "For a uint256 `count`, non-negativity is expressed as:\n"
                "  invariant count_nonneg()\n"
                "      currentContract.count >= 0;\n"
                "Parametric invariants quantify over free variables in the "
                "parameter list (e.g., `invariant f(address a) ...`)."
            ),
        ),
    ),

    # Q5 — intentionally malformed CVL on the first put_cvl_raw.
    # Typechecker.jar rejects the parse and the tool returns the error text
    # without mutating curr_spec.
    _ai(
        "Attempting an initial draft.",
        _tc("put_cvl_raw", cvl_file=BROKEN_PARSE_CVL),
    ),

    # Q6 — put the BAD_INV_CVL. Typechecks fine — the bug is semantic
    # (the invariant is false), not syntactic. Mutates state["curr_spec"]
    # and resets did_read.
    _ai(
        "Putting an initial count_zero-style invariant.",
        _tc("put_cvl_raw", cvl_file=BAD_INV_CVL),
    ),

    # Q7 — exercise get_cvl + record_skip. The two invariant titles are
    # `increments_sum_is_count` (1st) and `zero_address_is_zero` (2nd).
    _ai(
        "Reading back the draft + recording a tentative skip.",
        _tc("get_cvl"),
        _tc(
            "record_skip",
            property_title="increments_sum_is_count",
            reason=(
                "Tentative — will be undone on the next turn to exercise "
                "unskip_property."
            ),
        ),
    ),

    # Q8 — exercise unskip_property. Empty-reason sentinel in merge_skips
    # filters the entry out, so state["skipped"] returns to [].
    _ai(
        "Undoing the tentative skip.",
        _tc("unskip_property", property_title="increments_sum_is_count"),
    ),

    # Q9 — exercise expect_rule_failure + expect_rule_passage. The rule
    # name here needn't match any actual rule in curr_spec — both tools just
    # record a rule_skips entry. `expect_rule_passage` then removes it with
    # the DELETE_SKIP sentinel, so state["rule_skips"] returns to {}.
    _ai(
        "Marking a rule expected-to-fail...",
        _tc(
            "expect_rule_failure",
            rule_name="count_zero",
            reason=(
                "Tentative mark — about to unmark to exercise the paired "
                "expect_rule_passage tool."
            ),
        ),
    ),
    _ai(
        "Actually, just kidding",
        _tc("expect_rule_passage", rule_name="count_zero"),
    ),

    # Q10 — first feedback_tool invocation against BAD_INV_CVL. Spawns the
    # feedback judge sub-agent (J1.{1-3}). The judge returns good=False so
    # validations["feedback"] is NOT stamped.
    _ai(
        "Seeking judge feedback on the current (bad) draft.",
        _tc("feedback_tool"),
    ),

    # J1.1 — feedback judge, first invocation, turn 1. Tools: memory,
    # rough_draft, get_cvl, feedback_tools (= cvl_authorship_tools), result
    # (PropertyFeedback). Validator `did_rough_draft_read` rejects result
    # until did_read=True.
    _ai(
        "Judge: gathering the spec + drafting a verdict.",
        _tc("memory", command="view", path="/memories"),
        _tc("get_cvl"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "First-pass: the current spec encodes `count == 0` as an "
                "invariant, which directly contradicts the property that "
                "increment() increases count by 1. Verdict: BAD — spec does "
                "not faithfully express the two target invariants "
                "(increments_sum_is_count, zero_address_is_zero)."
            ),
        ),
    ),

    # J1.2 — judge: read the draft.
    _ai(
        "Judge: reading the draft before verdict.",
        _tc("read_rough_draft"),
    ),

    # J1.3 — judge: good=False verdict. Does NOT stamp the feedback digest.
    _ai(
        "Judge: delivering the first (rejecting) verdict.",
        _tc(
            "result",
            good=False,
            feedback=(
                "The submitted spec states `count == 0` as an invariant "
                "but the properties to formalize are `count_nonneg` and "
                "`zero_address_is_zero`. Please replace the spec with "
                "invariants that match the approved property list."
            ),
        ),
    ),

    # Q11 — author addresses the feedback by replacing the spec with
    # SUBTLE_INV_CVL (has the two expected invariant names but `count_nonneg`
    # is subtly wrong — body says ``count > 0`` instead of ``>= 0``).
    # Mutates curr_spec, resets did_read. The feedback digest stamped for
    # BAD_INV_CVL (if any — here J1 returned good=False so there was no
    # stamp) is now stale regardless.
    _ai(
        "Addressing the judge feedback with the two named invariants.",
        _tc("put_cvl_raw", cvl_file=SUBTLE_INV_CVL),
    ),

    # Q12 — second feedback_tool invocation against SUBTLE_INV_CVL. Spawns
    # J2.{1-3}. The judge approves by name-coverage (both expected names
    # present, both trivially typecheck) — missing the subtle `count > 0`
    # semantic bug in the first invariant. good=True stamps
    # validations["feedback"] = digest(SUBTLE_INV_CVL, skipped=[]).
    _ai(
        "Re-running the judge on the updated draft.",
        _tc("feedback_tool"),
    ),

    # J2.1 — feedback judge, second invocation, turn 1.
    _ai(
        "Judge: re-evaluating the updated spec.",
        _tc("get_cvl"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Second pass: the spec declares both increments_sum_is_count and "
                "zero_address_is_zero as separate invariants matching the "
                "approved property list. Coverage looks complete. "
                "Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    # J2.3 — good=True verdict. Stamps validations["feedback"] =
    # digest(SUBTLE_INV_CVL, []). Judge did not catch the `count > 0`
    # typo; the prover will.
    _ai(
        "Judge: approving the spec.",
        _tc(
            "result",
            good=True,
            feedback="",
        ),
    ),

    # Q13 — run verify_spec against SUBTLE_INV_CVL. The base-case check
    # for `count_nonneg` fires on the initial state (count == 0), where
    # the body `count > 0` is false. One rule violated → one
    # ``analyze_cex_raw`` LLM call fires INSIDE verify_spec (between this
    # tape entry and the next author turn). ``all_verified=False`` so
    # the tool returns the raw report string; validations[prover] is NOT
    # stamped.
    _ai(
        "Running the prover on the updated draft.",
        _tc("verify_spec", rules=None),
    ),

    # CEX.1 — inline counter-example analysis. ``analyze_cex_raw`` in
    # ``composer/prover/analysis.py`` calls ``llm.ainvoke(messages)`` (via
    # ``acached_invoke``) with a human-framed instruction template. It
    # expects a plain-text AIMessage back — NO tool_calls, because the
    # call bypasses the LangGraph agent loop entirely.
    #
    # Placement is critical: ``FakeMessagesListChatModel`` has a single
    # global cursor, so this entry must sit between the verify_spec turn
    # (Q13) and the next author turn (Q14). If the author reorders or
    # verify_spec is invoked twice without an intervening CEX, the tape
    # will drift.
    _ai(
        "Counter-example analysis for rule ``increments_sum_is_count``:\n\n"
        "The prover found a spurious starting state where incrementsSum is initialized to be"
        " non-zero in the invariant base case (constructor) which causes a trivial failure.\n\n"
        "Suggested fix: add an init_state axiom to constrain the value of the ghost in the base case."

    ),

    # Q14 — author responds to the CEX by replacing SUBTLE_INV_CVL with
    # GOOD_INV_CVL (uses ``>=`` instead of ``>``). Mutates curr_spec,
    # invalidates validations["feedback"] (digest changes).
    _ai(
        "Fixing the count_nonneg operator as the CEX suggests.",
        _tc("put_cvl_raw", cvl_file=GOOD_INV_CVL),
    ),

    # Q15 — third feedback_tool invocation. Spawns J3.{1-3}. Digest stale
    # since curr_spec changed; re-stamping is required before result.
    _ai(
        "Re-running the judge to re-stamp the feedback digest.",
        _tc("feedback_tool"),
    ),

    # J3.1 — feedback judge, third invocation, turn 1.
    _ai(
        "Judge: re-evaluating with the operator fix applied.",
        _tc("get_cvl"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "The init state axiom is well justified given that the sum of increments is 0 on creation."
                 " Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    # J3.3 — good=True. Stamps validations["feedback"] =
    # digest(GOOD_INV_CVL, []).
    _ai(
        "Judge: approving the fixed spec.",
        _tc("result", good=True, feedback=""),
    ),

    # Q16 — run verify_spec on GOOD_INV_CVL. Both invariants reduce to
    # uint256 non-negativity and hold trivially. all_verified=True with
    # rules=None → validations["prover"] stamped with
    # digest(GOOD_INV_CVL, []) — same digest as feedback.
    _ai(
        "Running the prover on the fixed invariants.",
        _tc("verify_spec", rules=None),
    ),

    # Q17 — final result. Both validations current, curr_spec unchanged
    # since Q14 / J3. PublishResultTool requires `commentary` plus a
    # `property_rules` mapping covering every (non-skipped) batch title —
    # here the two invariant titles, each verified by the invariant of the
    # same name in GOOD_INV_CVL.
    _ai(
        "Finalizing the invariant CVL.",
        _tc(
            "result",
            commentary=(
                "Formalized the two structural invariants (increments_sum_is_count, "
                "zero_address_is_zero)."
            ),
            property_rules=[
                {"property_title": "increments_sum_is_count", "rules": ["increments_sum_is_count"]},
                {"property_title": "zero_address_is_zero", "rules": ["zero_address_is_zero"]},
            ],
        ),
    ),

    # ───────────────────────────────────────────────────────────────────
    # P5. Bug analysis (run_bug_analysis, 1 component)
    # ───────────────────────────────────────────────────────────────────
    # Tools available: rough_draft (via get_rough_draft_tools),
    #   bug_analysis_tools (= source_tools), result.
    # Validator: standard bind_standard (output_key). Result schema is
    #   (list[PropertyFormulation], "The security properties ..."), so args
    #   are {"value": [...]}.
    #
    # `refinement` is None from the pipeline, so there is NO refinement-loop
    # conversation after this — once `result` fires, the phase ends.

]

_BUG_TAPE: list[BaseMessage] = [

    # P5.1 — exercise source_tools + rough_draft. No did_read requirement,
    # kept for coverage.
    _ai(
        "Bug analysis: inspecting the entry point source.",
        _tc("get_file", path="src/Counter.sol"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "increment() unconditionally adds 1 to count and 1 to "
                "increments[msg.sender]. incrementOther(other) is meant "
                "to credit increments[other] but the implementation looks "
                "off — flag a property over its intended behavior. Three "
                "safety properties total: (a) increment() bumps count "
                "by 1, (b) increment() bumps increments[msg.sender] by 1, "
                "(c) incrementOther(other) bumps increments[other] by 1."
            ),
        ),
    ),

    # P5.2 — read draft before emitting result.
    _ai(
        "Bug analysis: re-reading the draft.",
        _tc("read_rough_draft"),
    ),

    # P5.3 — emit all three properties in one result call. Schema is the
    # ``_AgentRoundResult`` BaseModel (composer/spec/bug.py): ``items`` is the
    # property list and ``reasoning`` is a required narrative field — there
    # is no ``value`` wrapper here, unlike the tuple-shaped result tools.
    _ai(
        "Delivering the three extracted properties.",
        _tc(
            "result",
            items=_BUG_ANALYSIS_PROPS,
            reasoning=(
                "increment() unconditionally mutates two storage slots: it "
                "adds 1 to `count` and 1 to `increments[msg.sender]`. "
                "incrementOther(other) is documented to credit "
                "`increments[other]` by 1; whether the implementation "
                "actually does that is a question for the prover. The "
                "three pre/post equalities on those slots are the obvious "
                "safety properties; nothing else in the contract surface "
                "is worth formalizing at this stage."
            ),
        ),
    ),


    # ───────────────────────────────────────────────────────────────────
    # P6. Component CVL generation (batch_cvl_generation, component=<one>)
    # ───────────────────────────────────────────────────────────────────
    # Same author-agent shape as P4 but streamlined — we do not re-exercise
    # every tool. Tool coverage is satisfied by P4; P6 covers the
    # surface-a-real-bug path.
    #
    # 3 refined properties from P5b — record_skip would accept their titles,
    # but the tape doesn't exercise record_skip in this phase.
    #
    # The spec contains three rules: two that hold against the
    # implementation and one (``incrementOther_credits_target_when_distinct``)
    # that CEXes because ``Counter.incrementOther`` has a real bug — it
    # credits ``msg.sender`` instead of ``other``. The author marks that
    # rule as expected-to-fail with a reason explaining the surfaced bug,
    # then re-runs the prover with the rule excluded so
    # ``validations[prover]`` can be stamped.

]

_CVL_TAPE: list[BaseMessage] = [

    # R1 — put the three-rule component spec. Typechecks; covers all three
    # refined props.
    _ai(
        "Writing the component spec covering all three properties.",
        _tc("put_cvl_raw", cvl_file=COMPONENT_CVL),
    ),

    # R2 — request feedback. Spawns J3.{1-3}. Judge returns good=True on
    # first pass (the spec faithfully encodes all three properties; whether
    # the rules pass against the implementation is the prover's question).
    _ai(
        "Requesting judge feedback on the component spec.",
        _tc("feedback_tool"),
    ),

    # J3.1 — feedback judge, single pass, turn 1.
    _ai(
        "Judge: inspecting the component spec.",
        _tc("get_cvl"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Three rules, each asserting the exact post-condition for "
                "its respective property. The incrementOther rule "
                "constrains other != msg.sender per the refined property. "
                "Coverage is complete. Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    # J3.3 — good=True verdict. Stamps validations["feedback"] with
    # digest(COMPONENT_CVL, skipped=[]). rule_skips is NOT part of the
    # digest, so the later expect_rule_failure won't invalidate this
    # stamp.
    _ai(
        "Judge: approving the component spec.",
        _tc("result", good=True, feedback=""),
    ),

    # R3 — first prover run. The two increment() rules verify; the
    # incrementOther rule CEXes (msg.sender credited instead of other).
    # all_verified=False → validations[prover] NOT stamped, tool returns
    # raw report string. Exactly ONE failing rule → exactly ONE
    # ``analyze_cex_raw`` LLM call fires inline (CEX.2 below).
    _ai(
        "Running the prover on the component spec.",
        _tc("verify_spec", rules=None),
    ),

    # CEX.2 — inline analysis of the incrementOther CEX. Plain AIMessage,
    # no tool_calls, mirrors the CEX.1 entry in the invariant-CVL phase.
    # Critical placement: between R3 and R4 in the global tape cursor.
    _ai(
        "Counter-example analysis for rule "
        "``incrementOther_credits_target_when_distinct``:\n\n"
        "The prover constructed a state where ``msg.sender`` and "
        "``other`` are distinct nonzero addresses and ``increments[other]"
        "`` starts at 0. After the call, ``increments[other]`` is still "
        "0 — the implementation incremented ``increments[msg.sender]`` "
        "instead. This is a real bug in ``Counter.incrementOther``: it "
        "credits the caller rather than the target address. The CVL "
        "rule is correctly written; the implementation is wrong.\n\n"
        "Suggested action: leave the rule in place as a regression "
        "witness, mark it expected-to-fail with a citation back to the "
        "implementation bug, and surface this in the final commentary "
        "so a human can fix the Solidity."
    ),

    # R4 — author responds to the surfaced bug by marking the rule as
    # expected-to-fail. ``expect_rule_failure`` writes into ``rule_skips``
    # via a Command. ``rule_skips`` is NOT part of the digest used by
    # validation stamps, so the prior feedback stamp remains valid.
    _ai(
        "The CEX flags a real bug in Counter.incrementOther. Marking the "
        "rule as expected-to-fail so we can re-run the prover with it "
        "excluded.",
        _tc(
            "expect_rule_failure",
            rule_name="incrementOther_credits_target_when_distinct",
            reason=(
                "Surfaces a real implementation bug in "
                "Counter.incrementOther: the function credits "
                "increments[msg.sender] instead of increments[other]. "
                "The rule (and the property it formalizes) are correct; "
                "the Solidity needs to be fixed. Tracking the rule as "
                "expected-to-fail so the spec still verifies for the two "
                "increment() properties while the bug is open."
            ),
        ),
    ),

    # R5 — re-run prover. With the buggy rule in rule_skips, the
    # all_verified loop in verify_spec ignores it; the two increment()
    # rules pass, so all_verified=True and rules=None → stamps
    # validations[prover] at digest(COMPONENT_CVL, skipped=[]), which
    # matches the feedback stamp from J3.3.
    _ai(
        "Re-running the prover with the buggy rule excluded.",
        _tc("verify_spec", rules=None),
    ),
    _ai(
        "Counter-example analysis for rule "
        "``incrementOther_credits_target_when_distinct``:\n\n"
        "The prover constructed a state where ``msg.sender`` and "
        "``other`` are distinct nonzero addresses and ``increments[other]"
        "`` starts at 0. After the call, ``increments[other]`` is still "
        "0 — the implementation incremented ``increments[msg.sender]`` "
        "instead. This is a real bug in ``Counter.incrementOther``: it "
        "credits the caller rather than the target address. The CVL "
        "rule is correctly written; the implementation is wrong.\n\n"
        "Suggested action: leave the rule in place as a regression "
        "witness, mark it expected-to-fail with a citation back to the "
        "implementation bug, and surface this in the final commentary "
        "so a human can fix the Solidity."
    ),

    # R6 — final result. Both stamps current, curr_spec unchanged since
    # R1. Commentary documents the surfaced bug so the downstream
    # ``natspec_report`` / file-on-disk autospec output flags it for the
    # human reviewer.
    _ai(
        "Finalizing the component CVL.",
        _tc(
            "result",
            commentary=(
                "Formalized all three extracted safety properties as "
                "pre/post equalities. The two increment() rules verify. "
                "The incrementOther rule is left in place and marked "
                "expected-to-fail because the prover surfaced a real "
                "bug: Counter.incrementOther credits increments[msg."
                "sender] instead of increments[other]. The spec is "
                "correct; the implementation needs to be fixed."
            ),
            property_rules=[
                {"property_title": "count_increments_by_one", "rules": ["increment_increases_count"]},
                {"property_title": "sender_increments_by_one", "rules": ["increment_increases_sender_tally"]},
                {"property_title": "other_increments_by_one", "rules": ["incrementOther_credits_target_when_distinct"]},
            ],
        ),
    ),
]


# ───────────────────────────────────────────────────────────────────────────
# P7. Report grouping (build_report → call_grouping_llm)
# ───────────────────────────────────────────────────────────────────────────
# The final, best-effort phase. ``call_grouping_llm`` makes ONE structured-output
# call (``llm.with_structured_output(GroupingResult)``), so this lane has exactly
# one entry: an AIMessage whose ``GroupingResult`` tool call (the tool name is the
# pydantic model's class name) partitions every formalized property into groups.
#
# The five formalized properties this run produces (component, title):
#   ("Increment", count_increments_by_one / sender_increments_by_one /
#    other_increments_by_one)  +  ("Structural Invariants",
#    increments_sum_is_count / zero_address_is_zero).
# ``coverage.validate`` requires each appear in exactly one group, so the two
# groups below must cover all five with no overlap or omission. Without this lane
# the call raised ``no tape lane``, which ``build_report`` swallows into its
# fallback single-bucket grouping — so the report path ran but the grouping step
# was never actually exercised.

_REPORT_TAPE: list[BaseMessage] = [
    _ai(
        "Partitioning the formalized properties into high-level claims.",
        _tc(
            "GroupingResult",
            groups=[
                {
                    "slug": "increment-accounting",
                    "title": "Increment entry points update counters by exactly one",
                    "description": (
                        "Each increment operation advances the global counter and the "
                        "relevant per-address tally by exactly one."
                    ),
                    "members": [
                        ["Increment", "count_increments_by_one"],
                        ["Increment", "sender_increments_by_one"],
                        ["Increment", "other_increments_by_one"],
                    ],
                },
                {
                    "slug": "counter-structural-invariants",
                    "title": "Counter state respects its structural invariants",
                    "description": (
                        "The global counter stays consistent with the sum of the per-address "
                        "tallies and the zero address is never credited."
                    ),
                    "members": [
                        ["Structural Invariants", "increments_sum_is_count"],
                        ["Structural Invariants", "zero_address_is_zero"],
                    ],
                },
            ],
        ),
    ),
]


# ───────────────────────────────────────────────────────────────────────────
# Budget-curtailment variant lanes
# ───────────────────────────────────────────────────────────────────────────
# Alternate invariant-CVL / formalize-0 lanes for the curtailment integration
# test, which runs the pipeline with the ``formalization_preparation`` and
# ``formalization`` caps at 0.0: the budget monitor's wrap-up alert fires on the
# first tool-result tick (0 >= 0.8 * 0), lifting the validation gates and
# stamping ``budget_curtailed`` — while the hard stop never fires (taped runs
# accrue no cost, and 0 > 0 is false). Each lane therefore: puts a typechecking
# draft, skips one property "for budget", and publishes WITHOUT ever consulting
# the feedback judge or the prover — the lifted gates accept it. No judge or
# CEX entries, and no live prover run, are consumed.

_CURTAILED_INVARIANT_CVL_TAPE: list[BaseMessage] = [
    # V1 — put a valid draft (real Typechecker.jar gatekeeps this put).
    _ai(
        "Drafting the structural invariants.",
        _tc("put_cvl_raw", cvl_file=GOOD_INV_CVL),
    ),
    # The wrap-up alert lands before this turn: skip what isn't finished.
    _ai(
        "Budget pressure — skipping the remaining invariant and wrapping up.",
        _tc(
            "record_skip",
            property_title="zero_address_is_zero",
            reason="Budget exhausted before this invariant could be validated.",
        ),
    ),
    # V3 — publish the partial under the lifted gates (no feedback/prover stamps).
    _ai(
        "Publishing the partial invariant spec.",
        _tc(
            "result",
            commentary=(
                "Budget-curtailed partial: increments_sum_is_count is drafted but "
                "unverified; zero_address_is_zero was skipped."
            ),
            property_rules=[
                {"property_title": "increments_sum_is_count", "rules": ["increments_sum_is_count"]},
            ],
        ),
    ),
]

_CURTAILED_CVL_TAPE: list[BaseMessage] = [
    # W1 — put the full three-rule draft (typechecks; never sent to the prover).
    _ai(
        "Writing the component spec.",
        _tc("put_cvl_raw", cvl_file=COMPONENT_CVL),
    ),
    # The wrap-up alert lands before this turn.
    _ai(
        "Budget pressure — skipping the incrementOther property and wrapping up.",
        _tc(
            "record_skip",
            property_title="other_increments_by_one",
            reason="Budget exhausted before this property could be validated.",
        ),
    ),
    # W3 — publish the partial under the lifted gates.
    _ai(
        "Publishing the partial component spec.",
        _tc(
            "result",
            commentary=(
                "Budget-curtailed partial: the two increment() rules are drafted but "
                "unverified; other_increments_by_one was skipped."
            ),
            property_rules=[
                {"property_title": "count_increments_by_one", "rules": ["increment_increases_count"]},
                {"property_title": "sender_increments_by_one", "rules": ["increment_increases_sender_tally"]},
            ],
        ),
    ),
]


# ───────────────────────────────────────────────────────────────────────────
# Stuck-rule nag variant lanes
# ───────────────────────────────────────────────────────────────────────────
# Alternate authoring lanes for the prover-nag integration test
# (``tests/test_prover_nag_integration.py``). That test mocks ``run_prover``
# and assigns statuses by rule name (every declared rule VERIFIED except
# NAG_STUCK_RULE → SANITY_FAILED), so no prover jobs run; the spec is kept
# live-prover-honest anyway: the third rule's contradictory ``require``s make
# its body unreachable, so it typechecks (put_cvl_raw's real Typechecker gate
# still runs) and a real run would report the same SANITY_FAILED via
# ``rule_sanity: basic`` (forced by ``prover_config_overlay``) — with no
# counterexample, so no CEX-analysis entries are consumed either way.
# The author runs the spec ``STUCK_RULE_NAG_THRESHOLD`` times, nudging it with
# a trailing comment between runs: the streak detector keys on (rule, status),
# not spec digest, so the nudge doesn't reset it — while keeping every run's
# digest distinct, clear of the identical-spec re-run gate in ``verify_spec``
# (today it only fires on TIMEOUT results, but it is expected to broaden to
# the other stuck statuses). The last run trips the stuck-rule detector, which appends a
# NagMarker to ``prover_history`` and queues the reminder the author monitor
# injects as a ``<system-reminder>`` HumanMessage. The author then reacts as
# the reminder suggests — marks the rule expected-to-fail — and re-verifies
# (the skipped rule no longer blocks ``all_verified``, and must NOT be
# re-nagged). The judge round comes AFTER the streak: every put invalidates
# the feedback stamp, so it is earned once, on the final spec text.

#: The rule the nag tape gets stuck on (and then marks expected-to-fail).
NAG_STUCK_RULE = "incrementOther_credits_target_when_distinct"

# Same two passing increment() rules as COMPONENT_CVL; the third rule's
# contradictory requires make it permanently vacuous → SANITY_FAILED.
NAG_COMPONENT_CVL = """\
methods {
    function count() external returns (uint256) envfree;
    function increments(address) external returns (uint256) envfree;
    function increment() external;
    function incrementOther(address) external;
}

rule increment_increases_count {
    env e;
    mathint before = count();
    increment(e);
    assert to_mathint(count()) == before + 1,
        "increment() must increase count by exactly 1";
}

rule increment_increases_sender_tally {
    env e;
    address s = e.msg.sender;
    mathint before = increments(s);
    increment(e);
    assert to_mathint(increments(s)) == before + 1,
        "increment() must increase increments[msg.sender] by exactly 1";
}

rule incrementOther_credits_target_when_distinct {
    env e;
    address other;
    require other != e.msg.sender;
    require other == e.msg.sender;
    mathint before_other = increments(other);
    incrementOther(e, other);
    assert to_mathint(increments(other)) == before_other + 1,
        "incrementOther(other) must increase increments[other] by exactly 1 when other != msg.sender";
}
"""


# Minimal happy-path invariant lane: the nag test doesn't re-pay the
# broken-parse / bad-draft / CEX detours the main tape covers — one judge
# round, one (passing) prover run, publish.
_NAG_INVARIANT_CVL_TAPE: list[BaseMessage] = [
    _ai(
        "Drafting the structural invariants.",
        _tc("put_cvl_raw", cvl_file=GOOD_INV_CVL),
    ),
    _ai(
        "Requesting judge feedback.",
        _tc("feedback_tool"),
    ),
    _ai(
        "Judge: inspecting the spec.",
        _tc("get_cvl"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Both approved invariants (increments_sum_is_count, "
                "zero_address_is_zero) are faithfully encoded, with the ghost "
                "seeded by an init_state axiom. Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    _ai(
        "Judge: approving the spec.",
        _tc("result", good=True, feedback=""),
    ),
    _ai(
        "Running the prover on the invariants.",
        _tc("verify_spec", rules=None),
    ),
    _ai(
        "Finalizing the invariant CVL.",
        _tc(
            "result",
            commentary=(
                "Formalized the two structural invariants "
                "(increments_sum_is_count, zero_address_is_zero)."
            ),
            property_rules=[
                {"property_title": "increments_sum_is_count", "rules": ["increments_sum_is_count"]},
                {"property_title": "zero_address_is_zero", "rules": ["zero_address_is_zero"]},
            ],
        ),
    ),
]


def _nag_attempt_spec(i: int) -> str:
    """The spec for streak attempt ``i`` (0-based). Attempts differ only by a
    trailing comment: the streak detector keys on (rule, status), so the nudge
    doesn't reset it, while every run's spec digest stays distinct — clear of
    the identical-spec re-run gate in ``verify_spec``."""
    if i == 0:
        return NAG_COMPONENT_CVL
    return (
        f"{NAG_COMPONENT_CVL}\n"
        f"// Attempt {i + 1}: nudging the spec to retry the sanity failure.\n"
    )


def _nag_streak_turns() -> list[BaseMessage]:
    """The stuck streak: threshold-many put/verify rounds. Each run: two rules
    VERIFIED, the vacuous rule SANITY_FAILED (never VIOLATED → no CEX
    entries). The final run reaches the threshold and fires the nag; the
    author monitor injects the <system-reminder> before the next turn."""
    turns: list[BaseMessage] = [
        _ai(
            "Writing the component spec covering all three properties.",
            _tc("put_cvl_raw", cvl_file=_nag_attempt_spec(0)),
        ),
        _ai(
            "Running the prover on the component spec.",
            _tc("verify_spec", rules=None),
        ),
    ]
    for i in range(1, STUCK_RULE_NAG_THRESHOLD):
        turns.append(_ai(
            f"The sanity failure could be transient — nudging the spec and "
            f"trying again (attempt {i + 1}).",
            _tc("put_cvl_raw", cvl_file=_nag_attempt_spec(i)),
        ))
        turns.append(_ai(
            f"Re-running the prover (attempt {i + 1}).",
            _tc("verify_spec", rules=None),
        ))
    return turns


_NAG_CVL_TAPE: list[BaseMessage] = [

    # NG1..NG{2×threshold} — put the component spec with the permanently-
    # vacuous third rule, then the nudge/verify streak up to the threshold.
    *_nag_streak_turns(),

    # NG-react — the turn after the nag reminder landed. React as it suggests:
    # take the stuck rule out of the verification obligation. rule_skips is
    # not part of the validation digest, so the feedback stamp survives.
    _ai(
        "The reminder is right — the rule has failed sanity identically on "
        "every run; its requires are unsatisfiable as written. Marking it "
        "expected-to-fail and moving on rather than burning more prover runs.",
        _tc(
            "expect_rule_failure",
            rule_name=NAG_STUCK_RULE,
            reason=(
                "Stuck in SANITY_FAILED across repeated identical runs (the "
                "rule body is vacuous — the requires are contradictory). "
                "Flagged by the stuck-rule reminder; excluded from the "
                "verification obligation instead of re-running."
            ),
        ),
    ),

    # NG-verify — re-verify. The skipped rule is excluded from all_verified
    # AND from the stuck-rule tally (no re-nag); the two increment() rules
    # pass, so rules=None + all_verified stamps validations["prover"] at the
    # digest of the final (attempt-nudged) spec.
    _ai(
        "Re-running the prover with the stuck rule excluded.",
        _tc("verify_spec", rules=None),
    ),

    # NG-judge — the feedback round runs AFTER the streak: every nudge put
    # invalidated any earlier feedback stamp, so it is earned once here, on
    # the final spec text (matching the prover stamp above). The judge
    # approves on property coverage — it doesn't run the prover, so the
    # vacuity goes unnoticed, deliberately.
    _ai(
        "Requesting judge feedback on the component spec.",
        _tc("feedback_tool"),
    ),
    _ai(
        "Judge: inspecting the component spec.",
        _tc("get_cvl"),
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Three rules, one per extracted property, each asserting the "
                "exact post-condition. The incrementOther rule is recorded as "
                "expected-to-fail with a documented reason. Coverage is "
                "complete. Verdict: GOOD."
            ),
        ),
    ),
    _ai(
        "Judge: reading the draft.",
        _tc("read_rough_draft"),
    ),
    _ai(
        "Judge: approving the component spec.",
        _tc("result", good=True, feedback=""),
    ),

    # NG-publish — coverage maps the third property onto the vacuous rule
    # (allowed for expected-to-fail rules, mirroring the main tape's R6).
    _ai(
        "Finalizing the component CVL.",
        _tc(
            "result",
            commentary=(
                "Formalized all three extracted safety properties. The two "
                "increment() rules verify. The incrementOther rule is stuck "
                "in a sanity failure (vacuous body) and was marked "
                "expected-to-fail after the stuck-rule reminder fired; it "
                "needs a human rewrite."
            ),
            property_rules=[
                {"property_title": "count_increments_by_one", "rules": ["increment_increases_count"]},
                {"property_title": "sender_increments_by_one", "rules": ["increment_increases_sender_tally"]},
                {"property_title": "other_increments_by_one", "rules": [NAG_STUCK_RULE]},
            ],
        ),
    ),
]


# Design-doc discovery lane. Only consumed when the run omits the design doc
# (system_doc=None); the finder lists the project, reads the design doc, and selects
# it. Counter's design doc is ``system.md`` at the scenario root. A single flat lane:
# bare fs tools, no nested code_explorer.
_DESIGN_DOC_TAPE: list[BaseMessage] = [
    _ai(
        "Inventorying the project to locate a design document.",
        _tc("list_files"),
    ),
    _ai(
        "Reading the most likely design document.",
        _tc("get_file", path="system.md"),
    ),
    _ai(
        "system.md specifies Counter's intended behavior. Selecting it.",
        _tc(
            "result",
            selected_path="system.md",
            reason="Describes the Counter's intended behavior and invariants.",
        ),
    ),
]


# The tape, as a per-phase lane map keyed by run_task task_id. HarnessFakeLLM
# serves each LLM call from its lane's cursor, so the scripted responses stay
# correct even though the pipeline runs phases concurrently. The Counter
# scenario has one component, "Increment".
_AUTOPROVE_TAPE: dict[str, list[BaseMessage]] = {
    DESIGN_DOC_DISCOVERY_TASK_ID: _DESIGN_DOC_TAPE,
    SYSTEM_ANALYSIS_TASK_ID: _SYSTEM_ANALYSIS_TAPE,
    HARNESS_TASK_ID: _HARNESS_TAPE,
    INVARIANTS_TASK_ID: _INVARIANTS_TAPE,
    INVARIANT_CVL_TASK_ID: _INVARIANT_CVL_TAPE,
    extract_task_id(0): _BUG_TAPE,
    formalize_task_id(0): _CVL_TAPE,
    REPORT_TASK_ID: _REPORT_TAPE,
}


# The curtailment variant: identical up-front phases, budget-curtailed authoring lanes, and NO
# report lane — with zero formalized properties ``build_report`` must skip the grouping LLM call
# entirely, and a stray call fails loudly as a missing lane (the test runs with
# ``RERAISE_REPORT_FAILURES``).
_AUTOPROVE_CURTAILED_TAPE: dict[str, list[BaseMessage]] = {
    DESIGN_DOC_DISCOVERY_TASK_ID: _DESIGN_DOC_TAPE,
    SYSTEM_ANALYSIS_TASK_ID: _SYSTEM_ANALYSIS_TAPE,
    HARNESS_TASK_ID: _HARNESS_TAPE,
    INVARIANTS_TASK_ID: _INVARIANTS_TAPE,
    INVARIANT_CVL_TASK_ID: _CURTAILED_INVARIANT_CVL_TAPE,
    extract_task_id(0): _BUG_TAPE,
    formalize_task_id(0): _CURTAILED_CVL_TAPE,
}


# The stuck-rule nag variant: identical up-front phases, minimal invariant
# authoring, and the nag-exercising formalize lane. The report lane is the
# main tape's verbatim — this variant formalizes the same five properties.
_AUTOPROVE_NAG_TAPE: dict[str, list[BaseMessage]] = {
    DESIGN_DOC_DISCOVERY_TASK_ID: _DESIGN_DOC_TAPE,
    SYSTEM_ANALYSIS_TASK_ID: _SYSTEM_ANALYSIS_TAPE,
    HARNESS_TASK_ID: _HARNESS_TAPE,
    INVARIANTS_TASK_ID: _INVARIANTS_TAPE,
    INVARIANT_CVL_TASK_ID: _NAG_INVARIANT_CVL_TAPE,
    extract_task_id(0): _BUG_TAPE,
    formalize_task_id(0): _NAG_CVL_TAPE,
    REPORT_TASK_ID: _REPORT_TAPE,
}


# ---------------------------------------------------------------------------
# Install / configuration API
# ---------------------------------------------------------------------------
#
# The CEX analyzer's response is inlined at its position within the
# invariant-cvl / formalize-0 lane (see the ``CEX.1`` entry after Q13's verify_spec).
# There is no side-channel tape — each call is routed to its phase's lane by
# ``run_task`` task_id, and within a lane responses are consumed in order.


def get_autoprove_Counter_llm(with_delay: bool = True) -> HarnessFakeLLM:
    """Return a fresh fake LLM loaded with the autoprove counter tape.

    Each call returns an independent instance with its own per-lane cursors, so
    tests can run multiple scenarios without cross-contamination.
    """
    return HarnessFakeLLM(lanes=_AUTOPROVE_TAPE, with_human_delay=with_delay)


def get_autoprove_Counter_curtailment_llm(with_delay: bool = True) -> HarnessFakeLLM:
    """The budget-curtailment variant of the Counter tape (see the curtailed lanes above)."""
    return HarnessFakeLLM(lanes=_AUTOPROVE_CURTAILED_TAPE, with_human_delay=with_delay)


def _install(fake: HarnessFakeLLM) -> HarnessFakeLLM:
    import composer.spec.agent_index as a_ind
    a_ind._UNSAFE_DISABLE_CACHE = True
    install_fake_llm(fake)
    return fake


def install_harness_tape(with_delay: bool = True) -> HarnessFakeLLM:
    """Route the autoprove pipeline's models to the Counter tape's fake LLM.

    Call this BEFORE importing the entry path — ``get_provider_for`` is imported
    by name in ``composer.spec.source.autoprove_common`` at module load, so the
    patch (``install_fake_llm``) must land first (``composer/bind.py`` is that
    hook). One fake instance backs every tier, so all lanes share one set of
    cursors, keeping the per-lane tape deterministic regardless of heavy/lite.

    Returns the fake so the caller can inspect lane state for debugging.
    """
    return _install(get_autoprove_Counter_llm(with_delay))


def install_curtailment_tape(with_delay: bool = True) -> HarnessFakeLLM:
    """``install_harness_tape``, but with the budget-curtailment tape."""
    return _install(get_autoprove_Counter_curtailment_llm(with_delay))


def autoprove_nag_lanes() -> dict[str, list[BaseMessage]]:
    """The nag-variant lane map, for callers that construct their own
    ``HarnessFakeLLM`` (e.g. the nag test's reminder-sniffing subclass)."""
    return dict(_AUTOPROVE_NAG_TAPE)


def install_nag_tape(
    with_delay: bool = True, *, fake: HarnessFakeLLM | None = None
) -> HarnessFakeLLM:
    """``install_harness_tape``, but with the stuck-rule nag tape. Pass ``fake``
    (built over ``autoprove_nag_lanes()``) to install a custom subclass instead
    of the default; ``with_delay`` only applies to the default construction."""
    return _install(
        fake if fake is not None
        else HarnessFakeLLM(lanes=_AUTOPROVE_NAG_TAPE, with_human_delay=with_delay)
    )


__all__ = [
    "BAD_INV_CVL",
    "BROKEN_PARSE_CVL",
    "COMPONENT_CVL",
    "GOOD_INV_CVL",
    "NAG_COMPONENT_CVL",
    "NAG_STUCK_RULE",
    "SUBTLE_INV_CVL",
    "autoprove_nag_lanes",
    "get_autoprove_Counter_llm",
    "get_autoprove_Counter_curtailment_llm",
    "install_harness_tape",
    "install_curtailment_tape",
    "install_nag_tape",
]


# ---------------------------------------------------------------------------
# Operator notes for the interactive refinement conversation (P5b)
# ---------------------------------------------------------------------------
#
# The refinement conversation kicks in only when the auto-prove pipeline is
# invoked with ``--interactive``. The first human turn has no preceding AI
# message (state == INIT), so there is no ``[TAPE EXPECTATION]`` marker for
# it. The expected opening user prompt is:
#
#     >>> Walk me through the properties you extracted, especially
#         property 3 — I want to make sure it's right.
#
# (Or any prompt that asks the AI to discuss the properties; the AI's
# scripted P5b.1 response presupposes a question along those lines.)
#
# Subsequent human turns have ``[TAPE EXPECTATION: respond '...']`` markers
# embedded in the preceding AI message — type those verbatim to advance the
# tape.
