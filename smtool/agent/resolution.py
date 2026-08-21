"""Resolution classifier (Tool 2) — given a CLUSTER of detector hotspots + the run's properties, decide
PER FUNCTION how to summarize: EXACT_CVL / OVER_APPROX / SYMBOLIC_MODEL / LEAVE_REAL.

Batch-by-cluster so the agent sees the connections: dedup shared leaves, and pick the RIGHT LEVEL (a
shared leaf, a mid-level parent, or a whole dependency) instead of classifying each method blindly.

The agent reads the SOURCE itself, so it judges "nonlinear math vs ugly implementation" natively — no
mechanical hints. The detector's `Cluster` is INJECTED (not imported), so there is no circular dependency
(summarization_detector already imports smtool.difficulty). The run's PROPERTIES are collected here via
POU (`collect_rules` -> treeView `get_all_checks`) — pass `run_url=` and they auto-populate.

Same composer shape as overapprox_loop (bind_standard + run_to_completion), but a single pass — no prover,
no refine loop. NB: no `from __future__ import annotations` (bind_standard reads `result`'s annotation raw).
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, NotRequired

from pydantic import BaseModel, Field
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState

from graphcore.graph import Builder, FlowInput
from ..ir import Signature
from ..overapprox import OverApproxTarget
from composer.spec.graph_builder import bind_standard


class Technique(str, Enum):
    EXACT_CVL = "exact_cvl"            # reimplement EXACTLY in CVL (res == f_cvl_exact): precise, sound by
                                      # equality (no invariant). Worth it ONLY when a cleaner/more-tractable
                                      # CVL form exists — ugly impl (assembly/bit-packing) or a simple closed
                                      # form. On already-clean math it buys nothing (same nonlinearity).
    OVER_APPROX = "over_approx"        # drop the exact value: havoc+Phi, or a deterministic-ghost + a
                                      # monotone/injective/zero axiom. Use when the properties tolerate it.
    SYMBOLIC_MODEL = "symbolic_model"  # whole-contract model — a DEPENDENCY only, NEVER the CUT.
    LEAVE_REAL = "leave_real"          # do not summarize (cheap enough, or no sound summary helps).


class Flag(BaseModel):
    function: str = Field(description="function to act on — 'Contract.method' or a bare free-function name")
    technique: Technique = Field(description="how to summarize it (or leave real)")
    rationale: str = Field(description="1-2 sentences: WHAT makes it costly (nonlinear MATH vs ugly "
                                       "IMPLEMENTATION), and WHY this technique fits given the properties")
    relation: str = Field(default="", description="OVER_APPROX only: the relation to preserve in English "
                                                  "(e.g. 'monotone in assets', 'injective', 'zero-preserving'); "
                                                  "'' otherwise")


class ResolutionResult(BaseModel):
    flags: list[Flag] = Field(description="holistic recommendations for the WHOLE cluster: dedup shared "
                                          "leaves, flag the RIGHT level (leaf/parent/dependency), and mark a "
                                          "method LEAVE_REAL when summarizing a shared leaf already covers it")


class ResolutionState(MessagesState):
    result: NotRequired[ResolutionResult]


class ResolutionInput(FlowInput):
    pass


@dataclass
class Cluster:
    """One connected hotspot group, injected from the detector (NOT imported)."""
    cut: str
    functions: list[str]                                              # the hotspot methods
    leaves: dict[str, int] = field(default_factory=dict)             # shared nonlinear/hashing leaf -> fan-in
    call_edges: list[tuple] = field(default_factory=list)            # (caller, callee) — the descent structure


SYSTEM_PROMPT = """\
You are a RESOLUTION CLASSIFIER for formal-verification summarization. You are given a CLUSTER of costly
functions from one prover run (identified by a difficulty detector), the run's PROPERTIES (rule names),
and source-read tools. Decide, PER function, HOW it should be summarized — or that it should be left real.

FIRST read the relevant source (the cluster's functions and their shared leaves) before deciding.

## The four techniques
- EXACT_CVL: reimplement the function EXACTLY in CVL (the summary returns f_cvl_exact, proven by an
  equality conformance). Precise, sound with NO invariant. Pick it ONLY when a clean CVL form is MORE
  TRACTABLE than the Solidity — i.e. the pain is an UGLY IMPLEMENTATION (inline assembly, bit-packing via
  shifts/masks, 512-bit mulmod tricks) or there is a simple closed form. On already-clean math (e.g.
  `(x*y)/d`) EXACT_CVL buys nothing — the same nonlinearity remains.
- OVER_APPROX: give up the exact value. Two flavors: a pure havoc+predicate (value irrelevant), or a
  DETERMINISTIC ghost carrying a monotonicity/injectivity/zero axiom (relations preserved, exact value
  dropped). Pick it when the pain is INTRINSIC NONLINEAR MATH (mulDiv, pow, exp/ln, EC) AND the properties
  do NOT need the exact value — only relations/bounds. State the relation to keep in `relation`.
- SYMBOLIC_MODEL: replace a whole contract with a CVL model. ONLY valid for a DEPENDENCY contract, NEVER
  the CUT ('%s') — you cannot model away the contract you are verifying.
- LEAVE_REAL: do not summarize (already cheap, or no sound summary preserves the properties).

## The decisive question: what do the PROPERTIES need?
Over-approx is sound but LOOSE — if a property reasons about the exact value or exact relation of a
function, an over-approx gives spurious counterexamples. So:
- property needs the exact value / exact conversion  -> EXACT_CVL (if a cleaner CVL form exists) or LEAVE_REAL
- property needs only a relation (monotone, injective, congruence like `preview==deposit`) -> OVER_APPROX
  with that relation (a deterministic ghost preserves congruence for free)
- property is indifferent to this function -> OVER_APPROX (havoc), or LEAVE_REAL if cheap

## Be HOLISTIC (this is why you get the whole cluster at once)
- If several methods share a LEAF (see the fan-in), flag the LEAF once and mark the methods LEAVE_REAL —
  summarizing the shared leaf subsumes them. Do NOT flag both a method and its leaf.
- Prefer the cleanest boundary: a shared leaf, or a mid-level parent with a clean typed signature.
- Deduplicate: one flag per function you actually want acted on.
- CALLABLE over HARNESS: conformance must CALL the summarized function. A `public`/`external` method is
  directly callable; a FREE (file-level) function or an `internal`/`private` library function is NOT — it
  would need a wrapper harness (not automated yet). So when the cost sits in a free/internal function,
  do NOT target it directly: summarize its NEAREST `public`/`external` caller instead (it subsumes the
  cost, e.g. an injective ghost over that caller removes the inner hash) and mark the inner function
  LEAVE_REAL. Only target a free/internal function directly if NO public/external caller in the cluster
  reaches it. Read the source to determine each function's visibility.

Return `flags`: one entry per function to act on (including LEAVE_REAL for methods a leaf covers), each
with the technique, a short rationale (name the cost: nonlinear math vs ugly impl), and — for OVER_APPROX
— the relation to preserve.
"""


def _cluster_brief(cluster: Cluster, rules: list) -> str:
    leaves = "\n".join(f"  - {fn}  (shared by {n} method(s))" for fn, n in sorted(
        cluster.leaves.items(), key=lambda kv: -kv[1])) or "  (none)"
    edges = "\n".join(f"  {a} -> {b}" for a, b in cluster.call_edges) or "  (none)"
    return (f"CUT (contract under verification): {cluster.cut}\n\n"
            f"Hotspot functions in this cluster:\n  " + "\n  ".join(cluster.functions) + "\n\n"
            f"Shared nonlinear/hashing LEAVES they inline (fan-in):\n{leaves}\n\n"
            f"Call edges (descent structure):\n{edges}\n\n"
            f"The run's PROPERTIES (rule names — these decide what precision is needed):\n  "
            + "\n  ".join(rules) + "\n\n"
            "Read the source of the functions/leaves, then classify each per the system prompt. "
            "Be holistic: dedup shared leaves, flag the right level, mark covered methods LEAVE_REAL.")


def build_resolution_graph(builder: Builder, cut: str, *, extra_tools: Iterable[BaseTool] = ()):
    """Assemble the classifier graph. bind_standard adds the terminal `result` tool + summarizer; we append
    the source-read tools (the agent's only other tools — it reads code, it does not run the prover)."""
    return (bind_standard(builder, state_type=ResolutionState)
            .with_input(ResolutionInput)
            .with_sys_prompt(SYSTEM_PROMPT % cut)
            .with_tools(list(extra_tools)))


_BUILTIN_RULES = {"envfreeFuncsStaticCheck"}   # prover built-ins, not user properties


def collect_rules(url: str) -> list:
    """The run's USER CVL rule names, from its treeView via POU (get_all_checks) — the property list the
    classifier keys on. Drops prover built-ins (envfreeFuncsStaticCheck / *StaticCheck). vaas-dev URLs
    need AISS_ENV=dev for POU auth (set here from the host, so callers needn't)."""
    import os
    if "vaas-dev" in url:
        os.environ.setdefault("AISS_ENV", "dev")
    from prover_output_utility import ProverOutputAPI
    api = ProverOutputAPI(use_local=False)
    seen, out = set(), []
    for c in api.get_all_checks(url):
        rn = (getattr(c, "rule_name", "") or "").strip()
        if rn and rn not in seen and rn not in _BUILTIN_RULES and "StaticCheck" not in rn:
            seen.add(rn)
            out.append(rn)
    return out


def target_from_flag(flag: Flag, sig: Signature, *, cut: str,
                     setup_spec_import=None) -> OverApproxTarget | None:
    """Adapter B (classifier -> summary builder): a Flag + the function's NATIVE `Signature` (scene-sourced
    via `smtool.scene.signature_from_scene` / `Signature.from_scene` — NOT a parsed string) -> a builder
    input. Routes by technique: OVER_APPROX / EXACT_CVL -> an OverApproxTarget (Phi left for the agent; the
    flag's relation/rationale as the GOAL, EXACT_CVL adds an exact-reimplementation goal). LEAVE_REAL and
    SYMBOLIC_MODEL -> None (nothing here / a different builder = driver)."""
    if flag.technique in (Technique.LEAVE_REAL, Technique.SYMBOLIC_MODEL):
        return None
    goal = flag.relation or flag.rationale
    if flag.technique == Technique.EXACT_CVL:
        goal = "Reimplement EXACTLY in CVL (Phi should pin res == the clean closed form). " + goal
    return OverApproxTarget(cut=cut, sig=sig, goal=goal, setup_spec_import=setup_spec_import)


async def classify_cluster(cluster: Cluster, rules: list | None = None, *, run_url: str | None = None,
                           llm: BaseChatModel | None = None,
                           builder: Builder | None = None, source_tools: Iterable[BaseTool] = (),
                           thread_id: str = "resolution", recursion_limit: int = 40,
                           callbacks=(), max_prompt_tokens: int = 100_000) -> ResolutionResult | None:
    """Classify one cluster in a single read-and-judge pass. Dev: pass `llm=<model>`; pipeline: pass
    `builder=env.builder_lite()`. Returns the holistic per-function flags."""
    if builder is None:
        if llm is None:
            raise ValueError("pass builder=env.builder_lite() (pipeline) or llm=<model> (dev)")
        builder = Builder().with_llm(llm, max_prompt_tokens=max_prompt_tokens)
    if not rules:                                     # Tool 2 owns rule collection (POU treeView)
        rules = collect_rules(run_url) if run_url else []
    task = _cluster_brief(cluster, rules)
    graph = build_resolution_graph(builder, cluster.cut, extra_tools=source_tools) \
        .with_initial_prompt(task).compile_async()
    # standalone/dev path: native ainvoke (no pipeline IO-handler context), like smtool/demo/run_agent.py
    final = await graph.ainvoke(ResolutionInput(input=[task]),
                                {"configurable": {"thread_id": thread_id},
                                 "recursion_limit": recursion_limit, "callbacks": list(callbacks)})
    return final.get("result")
