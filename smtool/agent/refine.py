"""Piece D: the verify -> CEX -> refine loop — deterministic (graphcore-style) orchestration that
turns the fill agent's *structurally-consistent* model into a *prover-verified* one.

The fill agent (piece F) can only reach "check_consistency is clean" — a typecheck + discipline gate,
NOT a proof. This loop adds the trust anchor: it writes the project, PROVES the shared reachable
invariants (dropping the ones that don't hold), VERIFIES every conformance conf, and — on any failure —
feeds a compact counterexample summary back to the SAME agent so it refines (fix the model body, or
add a *provable* reachable invariant), then re-verifies. The agent only ever proposes; the prover
disposes. Repeats until all rules pass or the round budget is spent.

Prover calls live HERE, not in the agent's tool loop: they are minutes-long and cloud-bound, so keeping
them out of the agent's recursion budget gives clean, individually-loggable rounds and an explicit
round cap. The bound `Project` persists across rounds (mutations apply in place), so each agent
invocation sees the current model via render_model/render_conformance even without message continuity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

import asyncio
import json

from langgraph.checkpoint.memory import InMemorySaver

from graphcore.graph import Builder

from ..project import Project
from .. import verify as V
from .. import driver
from ..typecheck import typecheck_conf
from ..cex import fetch_cex
from ..difficulty import fetch_difficulty
from .tools import SmtoolDeps
from .loop import build_smtool_graph, SmtoolInput


@dataclass
class RefineConfig:
    """The run-config the loop needs to write + prove (everything not derivable from the Project).
    Mirrors what `generate_hub.verify_hub` uses: consume_setup gives sources_root / setup conf / cut."""
    out_dir: str                       # where specs+confs are written (must be <sources>/certora so paths resolve)
    setup_conf: dict                   # the base setup conf dict, rewritten per method (setup.conf)
    sources_root: str                  # certoraRun's cwd — conf files/verify are relative to it
    cut: str                           # the contract-under-test (setup.cut)
    local: bool = False                # LocalProverRunner vs CloudProverRunner
    certora_run_path: str = "certoraRun"
    disable_cache: bool = False


@dataclass
class RefineResult:
    """Outcome of the loop: did every conformance rule pass, in how many rounds, plus the last verdicts."""
    success: bool
    rounds: int
    kept_invariants: list[str] = field(default_factory=list)
    dropped_invariants: list[str] = field(default_factory=list)
    results: dict[str, V.VerifyResult] = field(default_factory=dict)


def _conf_paths(cfg: RefineConfig, project: Project) -> list[str]:
    """The conformance .conf path project.write emitted for each modeled method."""
    return [f"{cfg.out_dir}/conf/{project.inp.conformance_prefix}{driver._cap(m)}Conformance.conf"
            for m in project.conformance]


async def _local_typecheck(paths: list[str], cfg: RefineConfig) -> dict[str, V.VerifyResult]:
    """Run the FULL certoraRun typechecker (--compilation_steps_only, no cloud) on each conf. Returns a
    {conf: VerifyResult} of ONLY the ones that failed — a semantic-error gate we pay before any cloud
    round. A conformance conf imports the model + reachable + setup, so this typechecks the whole bundle
    with file:line diagnostics the standalone jar (check_consistency) can't produce."""
    async def one(c):
        ok, tail = await asyncio.to_thread(typecheck_conf, c, cfg.sources_root,
                                           certora_run_path=cfg.certora_run_path)
        return c, ok, tail
    out: dict[str, V.VerifyResult] = {}
    for c, ok, tail in await asyncio.gather(*(one(c) for c in paths)):
        if not ok:
            out[c] = V.VerifyResult(conf=c, success=False, job_url=None, rules=[],
                                    error="local typecheck failed:\n" + tail)
    return out


def _make_full_typecheck(project: Project, cfg: RefineConfig):
    """A bound closure for the agent's check_consistency: write the project + run the REAL certoraRun
    typechecker, returning (ok, diagnostics). Typechecks ONE conformance conf — it imports the shared
    model + reachable spec, so a single compile covers the agent's main free-form surface (the mirrors
    and <f>CVL bodies) and catches the semantic errors (assign-after-access, mathint) the standalone jar
    misses. (Per-method conformance-spec errors are rare — that CVL is driver-generated — and the loop's
    verify pass covers them.)"""
    async def tc() -> tuple[bool, str]:
        project.write(cfg.out_dir, cfg.setup_conf)
        confs = _conf_paths(cfg, project)
        if not confs:
            return True, ""
        return await asyncio.to_thread(typecheck_conf, confs[0], cfg.sources_root,
                                       certora_run_path=cfg.certora_run_path)
    return tc


async def _prove_and_verify(project: Project, cfg: RefineConfig):
    """One prover pass. FIRST a local typecheck gate (no cloud) — if any conf fails to typecheck, return
    those failures and skip the cloud entirely (the agent gets file:line errors fast). Only when the
    bundle typechecks do we prove+prune the shared reachable invariants once and verify every conformance
    conf on the prover. Returns (kept, dropped, results)."""
    project.write(cfg.out_dir, cfg.setup_conf)
    conf_paths = _conf_paths(cfg, project)
    tc_targets = list(conf_paths)
    if project.reachable_invariant_names():
        tc_targets.append(f"{cfg.out_dir}/conf/{cfg.cut}Reachable.conf")
    tc_fail = await _local_typecheck(tc_targets, cfg)
    if tc_fail:
        return [], [], tc_fail   # skip the cloud; feed the typecheck diagnostics back to the agent

    kept: list[str] = []
    dropped: list[str] = []
    common = dict(sources_root=cfg.sources_root, local=cfg.local,
                  certora_run_path=cfg.certora_run_path, disable_cache=cfg.disable_cache)
    if project.reachable_invariant_names():
        reach = f"{cfg.out_dir}/conf/{cfg.cut}Reachable.conf"
        kept, dropped, _ = await V.prune_reachable(project, reach, **common)
        project.write(cfg.out_dir, cfg.setup_conf)   # reflect the pruning before conformance runs
    # verify_all_early: stop + cancel the rest as soon as one conf fails, instead of blocking on the
    # slowest job (a violated method needn't wait 40 min for another still running).
    results = await V.verify_all_early(conf_paths, **common)
    # enrich each real prover VIOLATION with its counterexample (targeted POU fetch, no zipOutput), so the
    # refine message shows the concrete inputs/values on which the model diverges. Skip CANCELLED confs
    # (not run to completion) — they have no counterexample.
    for r in results.values():
        if r.success or r.cancelled or not r.job_url:
            continue
        # a TIMEOUT is a performance problem (no counterexample) — fetch the prover's own difficulty
        # signal (ranked nonlinearity hotspots + inlined-without-summary calls) so the refine message
        # points the agent at the concrete off-path cost. A VIOLATION has a counterexample instead.
        if any(v.status == "TIMEOUT" for v in r.failures()):
            r.difficulty = (await asyncio.to_thread(fetch_difficulty, r.job_url)).format()
        else:
            r.cex = await asyncio.to_thread(fetch_cex, r.job_url, {v.rule for v in r.failures()} or None)
    return kept, dropped, results


def _refine_message(dropped: list[str], results: dict[str, V.VerifyResult]) -> str:
    """The counterexample fed back to the agent: exactly what failed (the typechecker's file:line text,
    or the prover's violated rule + assert message + report URL), and the tools to fix it. We do NOT
    enumerate specific CVL rules — the typechecker output says what is wrong, and `cvl_manual_search`
    is the agent's reference for how CVL works."""
    lines = ["The model is NOT yet verified. Fix the failures below, then I will re-check.", ""]
    if dropped:
        lines.append(f"REACHABLE INVARIANT(S) DROPPED (did not verify against the real CUT): {dropped}. "
                     "A reachable invariant must hold of the REAL contract alone (real getters only, no "
                     "model readers). Either add a genuinely provable one, or make the model body robust "
                     "without that assumption.")
    for c, r in results.items():
        name = Path(c).name
        if r.success:
            lines.append(f"[PASS] {name}")
            continue
        if r.cancelled:
            # not run to completion — the round stopped at the first failure below; re-checked next round
            lines.append(f"[not run] {name} (skipped after another method failed; will re-check)")
            continue
        url = f"  (report: {r.job_url})" if r.job_url else ""
        if r.error:
            lines.append(f"[{name}]\n{r.error}{url}")
        for v in r.failures():
            msg = f": {v.assert_message}" if v.assert_message else ""
            lines.append(f"[FAIL] {name}  rule {v.rule} {v.status}{msg}{url}")
        for label, xml in (r.cex or {}).items():
            # the counterexample: concrete inputs + the call trace on which the model diverges from real
            lines.append(f"  counterexample [{label}]:\n{xml}")
    lines += ["",
              "Read the diagnostics above. For a TYPECHECK error, fix the CVL at the reported file:line; "
              "if you are unsure why it is rejected, call cvl_manual_search to look up the rule. For a "
              "prover VIOLATION, read the counterexample call trace: it gives the CONCRETE inputs and the "
              "step-by-step values where the model's result diverges from the real contract. Trace which "
              "line of your model produced the wrong value and correct it (or add/prove a reachable "
              "invariant that rules the input out)."]
    timed_out = [(Path(c).name, r) for c, r in results.items()
                 if any(v.status == "TIMEOUT" for v in r.failures())]
    if timed_out:
        lines.append(
            f"TIMEOUT ({', '.join(n for n, _ in timed_out)}): a PERFORMANCE problem (the model is likely "
            "correct). Apply a technique from section 4 of your instructions — NONDET an off-path "
            "view/pure fn, PIVOT the return through a contract view getter (add_helper_lemma), or align a "
            "mirror for CONGRUENCE. The prover's OWN difficulty report below localizes the cost — use it "
            "instead of guessing. Do NOT just re-render/inspect; you MUST apply a mutation before result.")
        for name, r in timed_out:
            if r.difficulty:
                lines.append(f"[{name}] where the nonlinear cost is:")
                lines.append(r.difficulty)
    lines.append("Apply mutations (not just render/inspect), run check_consistency, then call result.")
    return "\n".join(lines)


def _fmt_msg(item) -> str:
    """Render one load_timeline item (a BaseMessage or a SummarizationMarker) as readable text — full
    content + tool calls for AI turns, full tool RESULTS for tool turns (the typechecker message, the
    RAG answer, the mutation verdicts — untruncated, since that is the diagnostic value)."""
    content = getattr(item, "content", None)
    if content is None:                                   # SummarizationMarker (or other non-message)
        return f"----- [{type(item).__name__}] -----"
    role = type(item).__name__.replace("Message", "").upper()
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    out = [f"===== {role} ====="]
    if text.strip():
        out.append(text)
    for tc in getattr(item, "tool_calls", None) or []:
        out.append(f"  >>> call {tc.get('name')}({json.dumps(tc.get('args', {}), default=str)})")
    return "\n".join(out)


async def dump_transcript(checkpointer, thread_id: str, path: str) -> None:
    """Write the FULL agent conversation (system/human/AI+reasoning+tool-calls/tool-results, across all
    rounds) to `path`, via composer's checkpoint walker (survives summarization). Best-effort."""
    try:
        from composer.io.thread_timeline import load_timeline
        items = await load_timeline(checkpointer, thread_id)
        Path(path).write_text("\n\n".join(_fmt_msg(it) for it, _cp in items))
    except Exception as e:
        Path(path).write_text(f"(transcript unavailable: {type(e).__name__}: {e})")


def _format_verify_result(kept: list[str], dropped: list[str],
                          results: dict[str, V.VerifyResult]) -> str:
    """The `verify` tool's message back to the agent: ALL-VERIFIED, or the failures + counterexamples/
    timeouts to fix (reusing _refine_message's per-failure rendering)."""
    if results and all(r.success for r in results.values()) and not dropped:
        confs = ", ".join(Path(c).name for c in results)
        return ("PROVER: ALL conformance rules VERIFIED"
                + (f" (reachable invariants proven: {kept})" if kept else "")
                + f".\nProven: {confs}.\nThe model is fully verified — call result now.")
    return "PROVER results — the model is NOT yet fully verified:\n\n" + _refine_message(dropped, results)


def _make_verify(project: Project, cfg: RefineConfig):
    """A bound closure for the agent's `verify` tool: write the project, prove+prune the shared reachable
    invariants, verify every conformance conf (early-stop), enrich violations with their counterexamples,
    and return it all as one message. This is the trust anchor, run INLINE in the agent's conversation
    (composer-style) so prover feedback arrives as a tool result — no orchestrated re-invocation."""
    async def verify_now() -> str:
        kept, dropped, results = await _prove_and_verify(project, cfg)
        return _format_verify_result(kept, dropped, results)
    return verify_now


async def run_refine_loop(project: Project, fill_task: str, cfg: RefineConfig, *,
                          llm: BaseChatModel | None = None, builder: Builder | None = None,
                          extra_tools: Iterable[BaseTool] = (), thread_id: str = "smtool",
                          fill_steps: int = 90, max_rounds: int = 4,
                          callbacks: Iterable[BaseCallbackHandler] = (),
                          transcript_path: str | None = None,
                          on_round: Callable[[int, list[str], list[str], dict], None] | None = None
                          ) -> RefineResult:
    """Composer-style single-conversation fill→prove→refine: `verify` (and the full-typecheck
    check_consistency) are TOOLS the agent calls, so the whole cycle — fill, check_consistency, verify,
    read the counterexample, fix, verify again, … result — happens in ONE graph invocation with feedback
    arriving inline as tool results. (The previous orchestrated re-invocation re-seeded the initial prompt
    each round and buried the counterexample; verify-as-a-tool avoids that structurally.) `fill_steps` is
    the recursion budget for that conversation; `max_rounds` is vestigial (kept for call compatibility).
    After the agent finishes, one authoritative verify records the verdict (a content-hash cache hit if
    the model is unchanged since the agent's last verify). Dev: pass `llm=`. Pipeline: pass
    `builder=env.builder_lite()` (inside a with_handler scope) + `extra_tools=env source tools`."""
    if builder is None:
        if llm is None:
            raise ValueError("pass llm=<model> (dev) or builder=env.builder_lite() (pipeline)")
        builder = Builder().with_llm(llm)
    ckpt = InMemorySaver()
    deps = SmtoolDeps(project, full_typecheck=_make_full_typecheck(project, cfg),
                      verify=_make_verify(project, cfg))
    graph = (build_smtool_graph(builder, deps, extra_tools=extra_tools)
             .with_initial_prompt(fill_task).compile_async(checkpointer=ckpt))
    try:
        await graph.ainvoke(SmtoolInput(input=[]),
                            {"configurable": {"thread_id": thread_id}, "recursion_limit": fill_steps,
                             "callbacks": list(callbacks)})
    except Exception:
        pass   # budget exhausted / API error — fall through to the authoritative verdict below
    finally:
        if transcript_path:
            await dump_transcript(ckpt, thread_id, transcript_path)
    # authoritative final verdict (cache hit if the model is unchanged since the agent's last verify)
    kept, dropped, results = await _prove_and_verify(project, cfg)
    if on_round is not None:
        on_round(0, kept, dropped, results)
    success = bool(results) and all(r.success for r in results.values()) and not dropped
    return RefineResult(success, 1, kept, dropped, results)
