"""Over-approx fill-agent loop — the graphcore assembly that drives an LLM to author the predicate Phi
for each target until its conformance proof passes, as STRONG as the goal wants.

Same composer shape as `agent/loop.py` (bind_standard + run_to_completion on a Builder) so it slots into
AutoProver's pipeline the same way. The system prompt is the one real difference from the model loop:
this task is GOAL-DIRECTED. Phi=`true` always verifies but yields a useless summary, so the agent must
push Phi toward the goal and only weaken it when a counterexample forces it.

NB: no `from __future__ import annotations` — bind_standard reads `OverApproxState.result`'s annotation
with a raw get_origin (no ForwardRef resolution), so it must be a real type object, not a string.
"""
from typing import NotRequired, Iterable

from pydantic import BaseModel, Field
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState

from graphcore.graph import Builder, FlowInput
from composer.spec.graph_builder import bind_standard, run_to_completion

from .overapprox_tools import OverApproxDeps, overapprox_tools


class OverApproxResult(BaseModel):
    """The agent's terminal result — a short summary of the Phi(s) it proved."""
    summary: str = Field(description="per target: the final Phi (in words) and its verify verdict "
                                     "(VERIFIED / weaker-but-sound / not proven)")


class OverApproxState(MessagesState):
    result: NotRequired[OverApproxResult]


class OverApproxInput(FlowInput):
    pass


SYSTEM_PROMPT = """\
You author a SOUND OVER-APPROXIMATING summary of a heavy Solidity function `f` by writing ONE predicate
`Phi(params, res)` per target, using the over-approx tools. The deterministic driver already emits, from
your Phi: the summary `fCVL(x){ T res; require Phi(x,res); return res; }` and the conformance rule
`overApprox_f`: `res = f@withrevert(x); assert !reverted => Phi(x, res)`. Installing the summary is SOUND
exactly when that rule holds — the summary then admits every value the real `f` can return.

## THE OBJECTIVE: the CLOSEST sound over-approximation (default), or a GOAL-TAILORED one
Two modes, decided by your task:
- DEFAULT (no specific property given): make Phi capture `f`'s real output behavior AS CLOSELY AS CVL can
  express and the prover can discharge. We summarize `f` because it is prover-HOSTILE (assembly, heavy
  dependencies, gas-optimized bit/memory tricks), so the exact recomputation is either inexpressible in
  CVL or the very thing that times out. Your job is the tightest sound approximation up to those two
  walls — CVL-expressibility and prover-tractability. This is RECONSTRUCT-THEN-RELAX (sections 2–3).
- GOAL-TAILORED (your task names a specific property the summary must preserve): make Phi capture THAT
  property as strongly as it proves; you need not reconstruct the rest of `f`.
- In BOTH modes, `Phi = true` is sound but USELESS (it havocs the result). Never finalize on `true` if
  you can prove something tighter. But do NOT chase marginal tightening forever — see section 5 (budget).

## 1. GROUND Phi IN THE REAL SOURCE
Read `f`'s Solidity (grep_files / get_file) — its return expression and the library math behind it — so
Phi states properties the real output genuinely has. Do not guess from prose.

## 2. RECONSTRUCT as faithfully as CVL allows (default mode)
Start AMBITIOUS: state the strongest true relationship between `f`'s inputs and its result that you can
write in CVL — mirror the parts of the computation CVL can express (arithmetic, comparisons, field/bit
extraction). Phi is a boolean predicate: statements ending in `return <bool>`; you may declare locals and
use `require` (they scope the existential the summary's `require Phi` introduces).
- CVL arithmetic is `mathint` (`a*b`, `a+b`, `a/b`); compare via mathint. Relate a `uintN`/UDVT result to
  an int with the casts (`to_mathint`, `require_uintN`, `assert_uintN`) or the byte-extract idiom for a
  bytesN UDVT: `uint248 v; require to_bytes31(v) == res; return (v >> 240) == 3;`.
- If a call is REJECTED with a parse/typecheck error you don't understand, call `cvl_manual_search`.

## 3. RELAX only at the wall (the three back-off signals)
Reconstructing the whole of a prover-hostile `f` will hit a wall. Relax the OFFENDING clause only, keeping
everything you can still prove — over-approximate that residue with the strongest property you can state
AND discharge (a RANGE/BRACKET `r*r <= x && x < (r+1)*(r+1)`, a TAG/bit-field on the result, a SIGN, a
RELATION to inputs `res <= a && (res==a||res==b)`, monotonicity, nonzero). The signals:
- VIOLATED — Phi is TOO STRONG. The counterexample gives a concrete input `x` and the real `res=f(x)`
  with `!Phi(x,res)`: your Phi excludes a value the real `f` returns. WEAKEN the failing clause (relax an
  equality to a bracket, drop an over-eager conjunct). NEVER `require` the counterexample away — that
  makes the summary UNSOUND (it must admit EVERY real output).
- TIMEOUT — Phi is TOO HEAVY. The feedback carries the prover's difficulty report (hotspots). Replace the
  heavy clause with a looser property that still holds (a range instead of the closed form), or PIVOT
  through a contract view getter that computes the same quantity. Weaker is always sound.
- INEXPRESSIBLE — you cannot write the clause in CVL (assembly, an opaque dependency). Drop it to the
  strongest thing you CAN write about that sub-result, down to leaving it unconstrained as a last resort.

## 4. CVL IS NOT SOLIDITY
When unsure how to write a cast/quantifier/type, call `cvl_manual_search` and follow the manual rather
than guessing from Solidity intuition.

## 5. BUDGET — converge, don't loop forever
Prover runs are minutes each and BOUNDED: `verify` refuses further runs once the budget is spent (it will
say so). Spend it well — reconstruct ambitiously FIRST (one strong Phi), then relax on the actual signal;
don't burn calls on tiny speculative tweaks. Each verified-then-tightened step is progress; a
verified Phi is always retained as the fallback, so if the budget runs out you still ship the tightest
one that PROVED. STOP tightening (call `result`) when any of: the reconstruction verified and you cannot
tighten it further without a wall; two successive tightenings both failed (you have converged); or the
budget is spent. Do not re-verify an unchanged Phi.

## 6. WORKFLOW (one conversation)
1. Read `f`. set_phi with your best (ambitious) Phi — for a named goal, target that property instead.
2. check_consistency — runs the REAL typechecker; fix every error at the reported file:line.
3. verify — runs the PROVER: ALL-VERIFIED, or failing rules with COUNTEREXAMPLES / TIMEOUTS (section 3),
   or a budget-spent notice.
4. Relax the offending clause (section 3) and verify AGAIN, until it holds as tightly as the walls allow
   or the budget is spent (section 5).
5. Call `result` per section 5. State, per target, the final Phi in words and whether it is the faithful
   reconstruction or a relaxed/partial one (a sound approximation is the deliverable; an UNPROVEN Phi is
   not — the retained fallback is always a proven one). Use render_phi / render_conformance /
   render_summary to inspect."""


def build_overapprox_graph(builder: Builder, deps: OverApproxDeps, *,
                           extra_tools: Iterable[BaseTool] = ()):
    """Assemble the over-approx fill-agent graph on a pre-configured `builder` (env.builder_lite() in the
    pipeline, or Builder().with_llm(llm) for dev). bind_standard adds the terminal `result` tool +
    summarizer; we append the over-approx tools + any source/manual tools."""
    return (bind_standard(builder, state_type=OverApproxState)
            .with_input(OverApproxInput)
            .with_sys_prompt(SYSTEM_PROMPT)
            .with_tools([*overapprox_tools(deps), *extra_tools]))


async def run_overapprox_agent(deps: OverApproxDeps, task: str, *, builder: Builder | None = None,
                               llm: BaseChatModel | None = None, thread_id: str = "overapprox",
                               recursion_limit: int = 80,
                               max_prompt_tokens: int = 100_000) -> OverApproxResult | None:
    """Run the fill agent to completion and return its `result`. Dev/standalone: pass `llm=<model>`;
    pipeline: pass `builder=env.builder_lite()` inside a with_handler scope."""
    if builder is None:
        if llm is None:
            raise ValueError("pass builder=env.builder_lite() (pipeline) or llm=<model> (dev)")
        builder = Builder().with_llm(llm, max_prompt_tokens=max_prompt_tokens)
    graph = build_overapprox_graph(builder, deps).with_initial_prompt(task).compile_async()
    res = await run_to_completion(graph, OverApproxInput(input=[task]), thread_id=thread_id,
                                  recursion_limit=recursion_limit, description="overapprox fill agent")
    return res.get("result")
