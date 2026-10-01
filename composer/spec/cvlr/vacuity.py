"""Explaining a vacuous CVLR rule from the unsat core the prover wrote for it.

The CVLR counterpart of :mod:`sanity_analyzer`, which does this for CVL: an unsat core in, a
structured account of why the rule's assertion is unreachable out, produced by an agent reading the
core against the source. What differs is where the core comes from and what it is a core *of*. On
Solana the prover writes no core for the vacuity check itself, only for a rule's assertion. So the
core comes from rerunning the rule with the vacuity check off, under which a vacuous rule verifies,
and it is the core of that proof (:class:`~composer.spec.cvlr.conf.CollectUnsatCore`).

That makes one fact about the core decidable without an agent, and it is the most useful one:
whether the proof needed the rule's own assertion (:func:`core_finding`). If it did not, the
assumptions contradict each other or the program, and the core names them. If it did, the
assumptions are not what makes the rule vacuous — which is the case the author's usual move,
weakening them, cannot fix.
"""

import re
from dataclasses import dataclass
from typing import Any, NotRequired

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from composer.io.context import run_to_completion
from composer.prover.conf import Conf, dump_conf
from composer.spec.graph_builder import bind_standard
from composer.spec.service_host import ModelProvider
from composer.spec.util import uniq_thread_id
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools
from graphcore.graph import FlowInput

#: The rule's own assertion as a core line: ``[in UC] 66: ASSERT B548:bool [assertion failed] (…)``.
_CORE_ASSERT = re.compile(r"^\[in UC\]\s+\d+:\s+ASSERT\b", re.MULTILINE)


@dataclass(frozen=True)
class AssertionInCore:
    """The proof needed the rule's assertion, so the core shows no contradiction among assumptions."""

    #: The core's ``ASSERT`` line, which carries the assertion's source location.
    line: str

    def summary(self) -> str:
        return (
            f"The rule's assertion is in the unsat core (`{self.line}`). The proof the prover found "
            "needs it, so the core does not show the rule's assumptions contradicting each other or "
            "the program: that is strong evidence the path to the assertion is live, and that the "
            "cause is after the assertion, where the core cannot see — the vacuity check's satisfy "
            "sits at the end of the rule, after its locals drop. Check that the rule ends with "
            "`core::mem::forget(accounts);`. Weakening the rule's assumptions is not the fix."
        )


@dataclass(frozen=True)
class AssertionNotInCore:
    """The core is unsatisfiable without the assertion, so its constraints cut off every path."""

    def summary(self) -> str:
        return (
            "The rule's assertion is not in the unsat core. The constraints marked `[in UC]` are "
            "unsatisfiable on their own, so they cut off every path to the assertion: that "
            "contradiction is what makes the rule vacuous."
        )


type CoreFinding = AssertionInCore | AssertionNotInCore


def core_finding(core: str) -> CoreFinding:
    """Whether ``core`` — an ``UnsatCoreTAC`` dump — includes the rule's own assertion."""
    found = _CORE_ASSERT.search(core)
    if found is None:
        return AssertionNotInCore()
    end = core.find("\n", found.start())
    return AssertionInCore(line=core[found.start() : end if end != -1 else len(core)].strip())


class VacuityMitigation(BaseModel):
    config_changes: list[tuple[str, str]] = Field(
        description="If the fix is a change to the prover configuration, the keys and values to "
        "set. Only `loop_iter` and `optimistic_loop` are the author's to change."
    )


class VacuityAnalysis(BaseModel):
    """The shape of :class:`sanity_analyzer.analysis.SanityAnalysisResult`, for a CVLR rule."""

    issue_type: str = Field(
        description='Category of the cause, like "Rule Assumptions", "Handler Always Fails", '
        '"Model Assumptions", "Prover Configuration", or "Not a Contradiction".'
    )
    mitigation_options: list[VacuityMitigation] = Field(
        description="Fixes expressible as configuration changes, and only those the core shows are "
        "needed — not contingencies. Empty when the fix is of another form, which is the usual case."
    )
    short_summary: str = Field(description="A short summary of the issue and possible fixes.")
    root_cause: str = Field(
        description="""Explanation of the root cause of the issue of the form:
[Brief title describing the main issue]
[2-3 sentence summary of what makes the rule's assertion unreachable]"""
    )
    detailed_analysis: str = Field(
        description="""Textual analysis giving more details about the issue in the form:
### 1. **The Problematic Constraint Sequence**
[Trace through the [in UC] commands, naming the Rust each comes from]

### 2. **Why This Leaves the Assertion Unreachable**
[Explain the contradiction, or, when the assertion is in the core, why there is none among the assumptions]"""
    )
    solution: str = Field(
        description="""Explanation of how to solve the issue of the form:
## Solution: [Title of the recommended fix]
[Detailed explanation of how to fix the issue, including:]
- Rule changes if needed
- `loop_iter` / `optimistic_loop` changes, with the rationale for each value
- Summaries, or changes to how the program is compiled for verification, if needed"""
    )

    def format(self) -> str:
        lines = [
            "# Vacuity Analysis",
            "",
            f"**Issue Type:** {self.issue_type}",
            "",
            "## Summary",
            "",
            self.short_summary,
            "",
            "## Root Cause",
            "",
            self.root_cause,
            "",
            "## Detailed Analysis",
            "",
            self.detailed_analysis,
            "",
            self.solution,
        ]
        if any(opt.config_changes for opt in self.mitigation_options):
            lines += ["", "## Mitigation Options", ""]
            for i, opt in enumerate(self.mitigation_options, 1):
                if opt.config_changes:
                    lines.append(f"**Option {i}:**")
                    lines.append("```")
                    lines.extend(f"{key} = {value}" for key, value in opt.config_changes)
                    lines.append("```")
        return "\n".join(lines)


class _VacuityInput(FlowInput, RoughDraftState):
    pass


class _VacuityST(MessagesState, RoughDraftState):
    result: NotRequired[VacuityAnalysis]


_CompiledVacuityGraph = CompiledStateGraph[_VacuityST, None, _VacuityInput, Any]


def _wrote_draft(s: _VacuityST, _: object) -> str | None:
    if not s.get("drafted"):
        return "You must write a rough draft before delivering your analysis"
    return None


@dataclass(frozen=True)
class VacuityAnalyzer:
    """The compiled analyzer graph plus how a call into it is run."""

    graph: _CompiledVacuityGraph
    recursion_limit: int

    async def explain(
        self,
        *,
        rule: str,
        finding: CoreFinding,
        core: str,
        harness: str,
        conf: Conf,
        within_tool: str | None,
    ) -> VacuityAnalysis:
        state = await run_to_completion(
            graph=self.graph,
            context=None,
            description=f"Vacuity analysis: {rule}",
            thread_id=uniq_thread_id("cvlr-vacuity"),
            recursion_limit=self.recursion_limit,
            input=_VacuityInput(
                input=[
                    f"The rule being analyzed is: {rule}",
                    finding.summary(),
                    f"The prover conf the rule was reported vacuous under:\n{dump_conf(conf)}",
                    f"The harness module that declares the rule:\n```rust\n{harness}\n```",
                    f"Unsat core data:\n{core}",
                ],
                drafted=False,
                memory=None,
            ),
            within_tool=within_tool,
        )
        assert "result" in state
        return state["result"]


def vacuity_analyzer(
    models: ModelProvider, tools: tuple[BaseTool, ...], *, recursion_limit: int
) -> VacuityAnalyzer:
    """The analyzer, reading the program and the harness through ``tools``.

    ``tools`` are the author's own source and reference tools: the analyzer has to map core commands
    back to the same Rust the author reads, and to the CVLR crates the harness calls into.

    On the heavy tier, as the counterexample analysis is: tracing a core back to the assumption that
    cuts off every path is reasoning, not retrieval.
    """
    graph = (
        bind_standard(models.builder_heavy(), _VacuityST, validator=_wrote_draft)
        .with_input(_VacuityInput)
        .with_tools(tools)
        .with_tools(get_rough_draft_tools(_VacuityST))
        .with_sys_prompt_template("cvlr_vacuity_system_prompt.j2")
        .with_initial_prompt_template("cvlr_vacuity_prompt.j2")
        .compile_async()
    )
    return VacuityAnalyzer(graph=graph, recursion_limit=recursion_limit)
