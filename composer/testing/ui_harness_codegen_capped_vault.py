"""Fake-LLM end-to-end harness tape for the **codegen** pipeline.

Replays a scripted run of ``console-codegen`` against the
``test_scenarios/codegen_capped_vault`` scenario with zero real LLM calls;
everything else (solc, the Certora prover, Postgres, the VFS, the working-spec
machinery) runs for real. Install with ``COMPOSER_TEST_TAPE=codegen_capped_vault``.

Scenario shape
--------------
A ``CappedVault`` with a per-account cap. The author's first draft is faithful
but omits the ``totalDeposited`` update; the spec's ``depositRaisesBalance`` rule
is over-strong (a zero-value deposit is a legal no-op). So the first prover run
fails BOTH rules, for different reasons:

  - ``depositIncreasesTotal``  — implementation bug → fixed by editing Solidity.
  - ``depositRaisesBalance``   — spec bug → fixed spec-side via ``cex_remediation``.

The two failures drive two PARALLEL per-rule CEX analyses, which is the whole
point of the addressing work: each runs in its own task_id lane
(``cex_rule_task_id(<prover tc id>, <rule>)``), with the cross-rule aggregator in
``cex_aggregator_task_id(<prover tc id>)``. We PIN the run-1 ``certora_prover``
tool_call_id to ``cvprun1`` so those lane keys are derivable here.

The system doc states two contradictory requirements (always-accept-deposits vs
enforce-the-cap). The requirements extractor emits both; the judge finds R2
satisfied and R1 violated; the author relaxes R1 (it contradicts R2) and delivers.

Lanes / task_ids
----------------
    requirements                          : requirements extraction agent
    codegen                               : the main author + every SEQUENTIAL
                                            sub-agent it spawns (cex_remediation,
                                            its summary_critic, the requirements
                                            judge) — all inherit the codegen
                                            task_id, so they interleave here in
                                            call order.
    cex-cvprun1-depositIncreasesTotal     : per-rule CEX analyzer (rule A)
    cex-cvprun1-depositRaisesBalance      : per-rule CEX analyzer (rule B)
    cex-agg-cvprun1                       : cross-rule CEX aggregator

Human-in-the-loop (replayed via ``COMPOSER_RESPONSE_TAPE``, not the LLM tape)
----------------------------------------------------------------------------
Two author turns trigger console HITL interrupts; the fake LLM only supplies AI
turns, so ``install_response_tape`` scripts the human replies (``_HUMAN_RESPONSES``),
consumed in call order:
  - ``commit_working_spec``            → ``ACCEPTED`` (promotes the remediated rule B
                                         to the master spec so the final master
                                         prover run stamps the ``prover`` validation).
  - ``requirement_relaxation_request`` → ``ACCEPTED`` (skips R1).

Verify loop
-----------
Prover verdicts are real, so this is a record→verify→iterate artifact like the
autoprove tapes. Spots to confirm on the first live run are flagged inline:
the exact prover verdicts, the order ``group_failing`` yields the two rules (which
fixes which cause_index the aggregator partitions reference), and whether the
post-skip ``requirements_evaluation`` (re-run) is what stamps the reqs validation.
"""

from typing import Any
import uuid

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.tool import ToolCall

from composer.testing.harness_tape import (
    HarnessFakeLLM, install_fake_llm, install_fake_responses,
)
from composer.spec.util import string_hash
from composer.workflow.executor import CODEGEN_TASK_ID, RESUME_COMMENTARY_TASK_ID
from composer.natreq.extractor import REQUIREMENTS_TASK_ID
from composer.prover.cex_task_ids import cex_rule_task_id, cex_aggregator_task_id


# The pinned tool_call_id of the run-1 certora_prover call, so the per-rule /
# aggregator CEX lane keys are derivable (the handler keys lanes off it).
_PROVER_TC = "cvprun1"


def _tc(name: str, *, _id: str | None = None, **args: Any) -> ToolCall:
    """Tool-call dict. ``_id`` pins the id (needed where downstream addressing
    derives from the tool_call_id, e.g. the run-1 prover call); otherwise random."""
    return {
        "id": _id if _id is not None else f"toolu_{uuid.uuid4().hex[:20]}",
        "name": name,
        "args": args,
        "type": "tool_call",
    }


def _ai(text: str = "", *tool_calls: ToolCall) -> AIMessage:
    """A tape entry: optional text plus zero or more tool_calls. A turn with no
    tool_calls ends the agent loop (returns to output_key extraction)."""
    content: list[str | dict] = []
    if text:
        content.append(text)
    content.extend(
        {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]}
        for t in tool_calls
    )
    return AIMessage(content=content, tool_calls=list(tool_calls))


# ---------------------------------------------------------------------------
# Requirements (must match VERBATIM across: the `reqs` result, the judge's
# per-requirement `requirement` field, and the relaxation. judge_res_checker
# compares the judge's text to the extracted reqs exactly.)
# ---------------------------------------------------------------------------

_R1 = "A deposit must always succeed and must never be rejected."
_R2 = (
    "An account's balance must never exceed the cap of 1000; any deposit that "
    "would exceed the cap must be rejected."
)


# ---------------------------------------------------------------------------
# Source + spec artifacts. Real tools gatekeep these: solc compiles the impl,
# the Certora prover proves/CEXes the spec.
# ---------------------------------------------------------------------------

# First draft: faithful, but deposit() omits `totalDeposited += amount`.
_BUGGY_IMPL = """\
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.29;

contract CappedVault {
    uint256 public constant CAP = 1000;
    mapping(address => uint256) public balance;
    uint256 public totalDeposited;

    function deposit(uint256 amount) external {
        require(balance[msg.sender] + amount <= CAP);
        balance[msg.sender] += amount;
    }

    function withdraw(uint256 amount) external {
        require(balance[msg.sender] >= amount);
        balance[msg.sender] -= amount;
        totalDeposited -= amount;
    }
}
"""

# The implementation fix for depositIncreasesTotal — add the missing update.
# (edit_file old/new; the `+= amount;` line is unique to deposit.)
_IMPL_FIX_OLD = """\
        balance[msg.sender] += amount;
    }"""
_IMPL_FIX_NEW = """\
        balance[msg.sender] += amount;
        totalDeposited += amount;
    }"""

# The remediated spec cex_remediation proposes for depositRaisesBalance: identical
# to the master spec except the assertion is guarded on `amount > 0`. Staged as
# the working spec, then committed to master.
_REMEDIATED_SPEC = """\
methods {
    function balance(address) external returns (uint256) envfree;
    function totalDeposited() external returns (uint256) envfree;
    function CAP() external returns (uint256) envfree;
    function deposit(uint256) external;
    function withdraw(uint256) external;
}

rule depositIncreasesTotal(uint256 amount) {
    env e;
    mathint before = totalDeposited();
    deposit(e, amount);
    assert to_mathint(totalDeposited()) == before + amount,
        "a successful deposit must increase totalDeposited by exactly amount";
}

rule depositRaisesBalance(uint256 amount) {
    env e;
    address caller = e.msg.sender;
    mathint before = balance(caller);
    deposit(e, amount);
    assert amount > 0 => to_mathint(balance(caller)) > before,
        "a positive deposit must raise the caller's balance";
}
"""


# ---------------------------------------------------------------------------
# CEX diagnoses. The aggregator partition's `diagnosis` text is what the handler
# content-addresses into the report_key (string_hash(partition.diagnosis)). So the
# author's cex_remediation(report_key=...) key is derived from _DIAG_B here, and
# the staged-proposal key from _REMEDIATED_SPEC — both match production exactly
# now that report_key / proposal_key are content-addressed.
# ---------------------------------------------------------------------------

_DIAG_A = (
    "deposit() updates the caller's balance but never updates the totalDeposited "
    "accumulator, so the running total stays put while balances grow. "
    "depositIncreasesTotal requires totalDeposited to rise by exactly the deposited "
    "amount, so any nonzero deposit violates it. Implementation defect, not a spec "
    "problem: the function is missing `totalDeposited += amount`."
)
_DIAG_B = (
    "depositRaisesBalance asserts the caller's balance strictly increases after any "
    "successful deposit, but a zero-value deposit is a legal no-op that leaves the "
    "balance unchanged (the counterexample deposits amount == 0). The rule is "
    "over-strong as written; a strict increase should only be required for amount > 0."
)

_KEY_A = string_hash(_DIAG_A)
_KEY_B = string_hash(_DIAG_B)
_PROPOSAL_KEY = string_hash(_REMEDIATED_SPEC)


# ---------------------------------------------------------------------------
# Lane: requirements extraction
# ---------------------------------------------------------------------------
# Tools: memory, reqs (result), human_in_the_loop, cvl_manual_search, rough draft.
# _extraction_res_checker gates `reqs` behind read_rough_draft (did_read).

_REQUIREMENTS_TAPE: list[BaseMessage] = [
    _ai(
        "Reading the system doc + spec to pull out the stated requirements.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "The system doc states two requirements that pull against each "
                "other: (1) deposits must always succeed, (2) a per-account cap of "
                "1000 must be enforced, rejecting over-cap deposits. Extract both "
                "verbatim; the downstream judge + relaxation resolve the conflict."
            ),
        ),
    ),
    _ai("Reading the draft back before emitting.", _tc("read_rough_draft")),
    _ai("Requirements extracted.", _tc("result", value=[_R1, _R2])),
]


# ---------------------------------------------------------------------------
# Lane: per-rule CEX analyzer — depositIncreasesTotal (rule A)
# ---------------------------------------------------------------------------
# Tools: rough draft + report-scoped fs tools. Validator gates `result` behind
# read_rough_draft. result payload = _PerCexCommitWrapper{commit}.

_CEX_RULE_A_TAPE: list[BaseMessage] = [
    _ai(
        "Analyzing the depositIncreasesTotal counterexample.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "CEX deposits a nonzero amount; balance updates but totalDeposited "
                "is unchanged in the post-state. The source's deposit() has no "
                "totalDeposited write. Root cause: missing accumulator update — an "
                "implementation defect."
            ),
        ),
    ),
    _ai("Reading the draft.", _tc("read_rough_draft")),
    _ai(
        "Committing the root cause.",
        _tc(
            "result",
            commit={
                "decision": "new",
                "text": _DIAG_A,
                "evidence": (
                    "Post-state totalDeposited equals its pre-state value while "
                    "balance[caller] increased by the deposited amount; deposit() "
                    "contains no write to totalDeposited."
                ),
            },
        ),
    ),
]


# ---------------------------------------------------------------------------
# Lane: per-rule CEX analyzer — depositRaisesBalance (rule B)
# ---------------------------------------------------------------------------

_CEX_RULE_B_TAPE: list[BaseMessage] = [
    _ai(
        "Analyzing the depositRaisesBalance counterexample.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "CEX has amount == 0. deposit(0) is a legal no-op, so the strict "
                "`balance > before` assertion fails. The implementation is fine; the "
                "rule over-constrains the zero case."
            ),
        ),
    ),
    _ai("Reading the draft.", _tc("read_rough_draft")),
    _ai(
        "Committing the root cause.",
        _tc(
            "result",
            commit={
                "decision": "new",
                "text": _DIAG_B,
                "evidence": (
                    "Counterexample operand amount == 0; balance[caller] is identical "
                    "in pre- and post-state, so the strict-increase assertion fails."
                ),
            },
        ),
    ),
]


# ---------------------------------------------------------------------------
# Lane: cross-rule CEX aggregator
# ---------------------------------------------------------------------------
# Sees the two per-rule root causes (indices 0 and 1, in group_failing order) and
# partitions them. The two causes are distinct, so two singleton partitions. The
# partition `diagnosis` texts are content-addressed into the report_keys.
#
# VERIFY: cause_indices below assume group_failing yields depositIncreasesTotal
# first (index 0) and depositRaisesBalance second (index 1). If the live order is
# reversed, swap the two cause_indices. Either way the run won't error (the keys
# resolve by diagnosis text), but the rendered per-rule attribution depends on it.

_CEX_AGGREGATOR_TAPE: list[BaseMessage] = [
    _ai(
        "Partitioning the two root causes.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "Two unrelated causes: (0) a missing totalDeposited update — an "
                "implementation defect; (1) an over-strong spec assertion on the "
                "zero-deposit case. No cross-rule equivalence; one partition each."
            ),
        ),
    ),
    _ai("Reading the draft.", _tc("read_rough_draft")),
    _ai(
        "Two distinct diagnoses.",
        _tc(
            "result",
            partitions=[
                {"diagnosis": _DIAG_A, "cause_indices": [0]},
                {"diagnosis": _DIAG_B, "cause_indices": [1]},
            ],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Lane: codegen — the main author plus every sequential sub-agent it spawns
# (cex_remediation, that remediator's summary_critic, the requirements judge),
# all in call order.
# ---------------------------------------------------------------------------

_CODEGEN_TAPE: list[BaseMessage] = [
    # T1 — initial (buggy) draft.
    _ai(
        "Drafting the CappedVault implementation from the interface + spec.",
        _tc("put_file", files={"src/CappedVault.sol": _BUGGY_IMPL}),
    ),

    # T2 — first prover run (all rules). PINNED id so the CEX lanes are derivable.
    # Triggers the two per-rule analyzers + aggregator (lanes above).
    _ai(
        "Running the prover on the first draft.",
        _tc(
            "certora_prover",
            _id=_PROVER_TC,
            source_files=["src/CappedVault.sol"],
            target_contract="CappedVault",
            compiler_version="solc8.29",
            loop_iter=1,
            rule=None,
            use_working_spec=False,
        ),
    ),

    # T3 — fix the implementation defect (depositIncreasesTotal).
    _ai(
        "The CEX report shows deposit() never updates totalDeposited — adding it.",
        _tc(
            "edit_file",
            path="src/CappedVault.sol",
            old_string=_IMPL_FIX_OLD,
            new_string=_IMPL_FIX_NEW,
        ),
    ),

    # T4 — the other failure is a spec bug; route it to cex_remediation. Author is
    # blocked from editing the spec inline; report_key is the content-addressed key
    # for diagnosis B (derived above).
    _ai(
        "The depositRaisesBalance failure is a spec over-constraint, not a code "
        "bug. Handing it to cex_remediation.",
        _tc("cex_remediation", report_key=_KEY_B, target_spec_path="rules.spec"),
    ),

    # --- remediation sub-agent (runs inside the codegen lane) ---
    # RM1 — remediator runs its proposal past the critic before delivering.
    _ai(
        "Proposing a guarded assertion; checking it with the summary critic first.",
        _tc(
            "summary_critic",
            proposed_cvl=_REMEDIATED_SPEC,
            proposed_addendum=None,
            target_call="deposit",
            rule_under_repair="depositRaisesBalance",
            cex_diagnosis=_DIAG_B,
        ),
    ),
    # --- summary_critic sub-agent (rough-draft gated) ---
    _ai(
        "Reviewing the proposed guard for soundness + faithfulness.",
        _tc(
            "write_rough_draft",
            rough_draft=(
                "`amount > 0 => balance' > balance` is sound: a zero deposit is a "
                "legal no-op per the system doc, and for positive deposits the cap "
                "bounds the add so balance strictly increases. Faithful to the doc. "
                "Verdict: sound."
            ),
        ),
    ),
    _ai("Reading the draft.", _tc("read_rough_draft")),
    _ai(
        "Critic verdict.",
        _tc("result", sound=True, issues=[], suggested_direction=None),
    ),
    # RM2 — remediator delivers the proposal (stored under _PROPOSAL_KEY).
    _ai(
        "Critic approved; delivering the remediation.",
        _tc(
            "result",
            proposed_cvl=_REMEDIATED_SPEC,
            rationale=(
                "Root cause B (over-strong assertion): guard the strict-increase "
                "assertion on `amount > 0`, leaving the zero-deposit no-op out of "
                "scope. No implementation change required."
            ),
            addendum=None,
        ),
    ),

    # T5 — stage the proposal as the working spec.
    _ai(
        "Staging the remediated spec as my working copy.",
        _tc("apply_remediation_proposal", proposal_key=_PROPOSAL_KEY),
    ),

    # T6 — re-run against the WORKING spec: fixed impl + guarded rule B → both pass.
    # (Working-spec run does not stamp the prover validation; that needs master.)
    _ai(
        "Re-running the prover against the working spec.",
        _tc(
            "certora_prover",
            source_files=["src/CappedVault.sol"],
            target_contract="CappedVault",
            compiler_version="solc8.29",
            loop_iter=1,
            rule=None,
            use_working_spec=True,
        ),
    ),

    # T7 — promote the working spec to master (HITL: driver approves).
    _ai(
        "Both rules verify; committing the spec fix to master.",
        _tc(
            "commit_working_spec",
            explanation=(
                "Guards depositRaisesBalance on `amount > 0` so a zero-value deposit "
                "(a legal no-op) no longer counts as a violation. Behavior-preserving "
                "for positive deposits."
            ),
        ),
    ),

    # T8 — final MASTER-spec run (rule=None, use_working_spec=False): stamps prover.
    _ai(
        "Verifying the committed master spec.",
        _tc(
            "certora_prover",
            source_files=["src/CappedVault.sol"],
            target_contract="CappedVault",
            compiler_version="solc8.29",
            loop_iter=1,
            rule=None,
            use_working_spec=False,
        ),
    ),

    # T9 — evaluate the requirements (spawns the judge sub-agent).
    _ai(
        "Code + spec verify. Evaluating the requirements.",
        _tc("requirements_evaluation"),
    ),
    # --- judge sub-agent: no rough draft (memory unset → no did_read gate). Each
    # requirement text must match the extracted reqs verbatim, by 1-based number.
    _ai(
        "Requirements verdict.",
        _tc(
            "result",
            judgement_result=[
                {
                    "classification": "VIOLATED",
                    "requirement": _R1,
                    "requirement_number": 1,
                    "commentary": (
                        "deposit() reverts once the cap would be exceeded, so a "
                        "deposit is not always accepted."
                    ),
                },
                {
                    "classification": "SATISFIED",
                    "requirement": _R2,
                    "requirement_number": 2,
                    "commentary": "The cap require() rejects any over-cap deposit.",
                },
            ],
        ),
    ),

    # T10 — R1 is violated only because it contradicts R2 (the enforced cap). Relax
    # it (HITL: driver replies ACCEPTED → skipped_reqs = {1}).
    _ai(
        "R1 (always accept) directly contradicts R2 (enforce the cap); the cap is "
        "the real requirement. Requesting to relax R1.",
        _tc(
            "requirement_relaxation_request",
            context="The cap (R2) is enforced, which necessarily makes some deposits revert.",
            req_number=1,
            req_text=_R1,
            judgment="VIOLATED: deposit() reverts once the cap would be exceeded.",
            explanation=(
                "R1 (deposits always succeed) is mutually exclusive with R2's enforced "
                "cap; R2 is the real requirement, so R1 should be relaxed."
            ),
        ),
    ),

    # T11 — re-evaluate now that R1 is skipped; this is what should stamp the reqs
    # validation (R1 ignored, R2 satisfied). VERIFY this is the stamping path.
    _ai(
        "Re-evaluating with R1 relaxed.",
        _tc("requirements_evaluation"),
    ),
    _ai(
        "Requirements verdict (R1 relaxed).",
        _tc(
            "result",
            judgement_result=[
                {
                    "classification": "VIOLATED",
                    "requirement": _R1,
                    "requirement_number": 1,
                    "commentary": "Still violated, but relaxed as contradictory with R2.",
                },
                {
                    "classification": "SATISFIED",
                    "requirement": _R2,
                    "requirement_number": 2,
                    "commentary": "The cap require() rejects any over-cap deposit.",
                },
            ],
        ),
    ),

    # T12 — deliver. check_completion requires the prover (T8) + reqs validations.
    _ai(
        "All rules verify and the requirements are resolved. Delivering.",
        _tc(
            "result",
            source=["src/CappedVault.sol"],
            comments=(
                "CappedVault implements per-account deposits with a 1000 cap. The "
                "totalDeposited accumulator update was added after the prover flagged "
                "depositIncreasesTotal. depositRaisesBalance was over-strong on the "
                "zero-deposit case and was remediated spec-side to guard on amount > 0. "
                "Requirement R1 (always accept deposits) was relaxed: it contradicts "
                "the enforced cap (R2)."
            ),
        ),
    ),

]




# ---------------------------------------------------------------------------
# Lane map + install
# ---------------------------------------------------------------------------

# Post-run resume-commentary lane. ``create_resume_commentary`` runs after the
# main graph as a structured-output call: ``with_structured_output(ResumeCommentary)``
# → ``bind_tools`` (no-op'd by the fake) piped through ``PydanticToolsParser``, which
# matches a tool call named after the schema class. So the scripted response is a
# ``ResumeCommentary`` tool call. Its own lane keeps the agent from consuming it.
_RESUME_COMMENTARY_TAPE: list[BaseMessage] = [
    _ai(
        "",
        _tc(
            "ResumeCommentary",
            commentary=(
                "Implemented CappedVault: added the missing totalDeposited update, "
                "remediated depositRaisesBalance to guard on amount > 0, and relaxed "
                "the contradictory deposits-always-succeed requirement."
            ),
            interface_path="src/CappedVault.sol",
        ),
    ),
]


_CODEGEN_LANES: dict[str, list[BaseMessage]] = {
    REQUIREMENTS_TASK_ID: _REQUIREMENTS_TAPE,
    CODEGEN_TASK_ID: _CODEGEN_TAPE,
    RESUME_COMMENTARY_TASK_ID: _RESUME_COMMENTARY_TAPE,
    cex_rule_task_id(_PROVER_TC, "depositIncreasesTotal"): _CEX_RULE_A_TAPE,
    cex_rule_task_id(_PROVER_TC, "depositRaisesBalance"): _CEX_RULE_B_TAPE,
    cex_aggregator_task_id(_PROVER_TC): _CEX_AGGREGATOR_TAPE,
}


def get_codegen_capped_vault_llm(with_delay: bool = True) -> HarnessFakeLLM:
    """A fresh fake LLM loaded with the codegen tape (independent lane cursors)."""
    return HarnessFakeLLM(lanes=_CODEGEN_LANES, with_human_delay=with_delay)


def install_harness_tape(with_delay: bool = True) -> HarnessFakeLLM:
    """Route the codegen pipeline's models to the fake LLM + disable the
    agent-index cache. ``composer/bind.py`` calls this when
    ``COMPOSER_TEST_TAPE=codegen_capped_vault`` is set, before the entry path
    imports ``get_provider_for`` by name (so the patch lands first)."""
    fake = get_codegen_capped_vault_llm(with_delay)
    import composer.spec.agent_index as a_ind
    a_ind._UNSAFE_DISABLE_CACHE = True
    install_fake_llm(fake)
    return fake


# ---------------------------------------------------------------------------
# Console HITL replies — replayed via COMPOSER_RESPONSE_TAPE, alongside the LLM
# tape. Consumed in call order: the commit_working_spec approval (T7) then the R1
# relaxation (T10). Both interrupts accept on a leading "ACCEPTED".
# ---------------------------------------------------------------------------

_HUMAN_RESPONSES = [
    "ACCEPTED — commit the guarded depositRaisesBalance fix to the master spec.",
    "ACCEPTED — R1 (deposits always succeed) contradicts the enforced cap (R2); relax R1.",
]


def install_response_tape() -> None:
    """Replay this scenario's console HITL replies. ``composer/bind.py`` calls
    this when ``COMPOSER_RESPONSE_TAPE=codegen_capped_vault`` is set."""
    install_fake_responses(_HUMAN_RESPONSES)


__all__ = [
    "get_codegen_capped_vault_llm",
    "install_harness_tape",
    "install_response_tape",
]
