"""The over-approx verify→counterexample→refine loop — composer-style verify-as-a-tool in ONE
conversation, the same shape as `agent/refine.py` but for the simpler over-approx artifact set (no shared
model, no reachable invariants: the whole model is the per-target predicate Phi).

`verify` and the full-typecheck `check_consistency` are TOOLS the agent calls inline, so the cycle —
write Phi, check_consistency, verify, read the counterexample, WEAKEN Phi (or simplify on a timeout),
verify again, … result — happens in one graph invocation with prover feedback returning as tool results.
Prover calls live HERE (minutes-long, cloud-bound), kept out of the agent's recursion budget. The bound
`OverApproxProject` persists across the conversation (set_phi mutates it in place).

Reuses the runner (`verify.verify_all_early`), the POU counterexample/difficulty fetchers, the real
typechecker (`typecheck.typecheck_conf`), and the transcript dumper — changing no existing smtool file.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from graphcore.graph import Builder

from ..overapprox_project import OverApproxProject, conformance_rule_name
from .. import verify as V
from ..typecheck import typecheck_conf
from ..cex import fetch_cex
from ..difficulty import fetch_difficulty
from .overapprox_tools import OverApproxDeps
from .overapprox_loop import build_overapprox_graph, OverApproxInput
from .refine import dump_transcript


@dataclass
class OverApproxRefineConfig:
    """The run-config the loop needs to write + prove (everything not derivable from the project)."""
    out_dir: str                       # where specs+confs are written (<sources>/certora so paths resolve)
    setup_conf: dict                   # the base setup conf dict (scene files/solc/links), rewritten per target
    sources_root: str                  # certoraRun's cwd — conf files/verify are relative to it
    local: bool = False                # LocalProverRunner vs CloudProverRunner
    certora_run_path: str = "certoraRun"
    disable_cache: bool = False
    run_url: str | None = None         # the ORIGINAL run — baseline verdicts + regression re-run


@dataclass
class OverApproxRefineResult:
    """Outcome: did every provable target's conformance rule pass, plus the last per-conf verdicts."""
    success: bool
    verified: list[str] = field(default_factory=list)     # target fns whose overApprox_<fn> VERIFIED
    results: dict[str, V.VerifyResult] = field(default_factory=dict)


async def _local_typecheck(paths: list[str], cfg: OverApproxRefineConfig) -> dict[str, V.VerifyResult]:
    """Run the FULL certoraRun typechecker (--compilation_steps_only, no cloud) on each conf; return a
    {conf: VerifyResult} of ONLY the failures — a semantic-error gate paid before any cloud round."""
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


def _make_full_typecheck(project: OverApproxProject, cfg: OverApproxRefineConfig):
    """Bound closure for the agent's check_consistency: write the project + run the REAL certoraRun
    typechecker on each conformance conf, returning (ok, diagnostics)."""
    async def tc() -> tuple[bool, str]:
        project.write(cfg.out_dir, cfg.setup_conf)
        confs = project.conf_paths(cfg.out_dir)
        if not confs:
            return True, ""
        fails = await _local_typecheck(confs, cfg)
        if not fails:
            return True, ""
        return False, "\n\n".join(r.error or "" for r in fails.values())
    return tc


async def _prove_and_verify(project: OverApproxProject, cfg: OverApproxRefineConfig):
    """One prover pass. FIRST a local typecheck gate (no cloud); if any conf fails, return those and skip
    the cloud. Otherwise verify every conformance conf (early-stop) and enrich each failure with its
    counterexample (VIOLATION → Phi too strong) or difficulty report (TIMEOUT → Phi too heavy)."""
    project.write(cfg.out_dir, cfg.setup_conf)
    conf_paths = project.conf_paths(cfg.out_dir)
    if not conf_paths:
        return {}
    tc_fail = await _local_typecheck(conf_paths, cfg)
    if tc_fail:
        return tc_fail                       # skip the cloud; feed the typecheck diagnostics back

    common = dict(sources_root=cfg.sources_root, local=cfg.local,
                  certora_run_path=cfg.certora_run_path, disable_cache=cfg.disable_cache)
    results = await V.verify_all_early(conf_paths, **common)
    for r in results.values():
        if r.success or r.cancelled or not r.job_url:
            continue
        if any(v.status == "TIMEOUT" for v in r.failures()):
            r.difficulty = (await asyncio.to_thread(fetch_difficulty, r.job_url)).format()
        else:
            r.cex = await asyncio.to_thread(fetch_cex, r.job_url, {v.rule for v in r.failures()} or None)
    return results


def _refine_message(results: dict[str, V.VerifyResult]) -> str:
    """The feedback fed back to the agent — Phi vocabulary: a VIOLATION means Phi is TOO STRONG on the
    counterexample input (weaken it, never `require` it away); a TIMEOUT means Phi is TOO HEAVY (simplify
    / pivot to a looser goal-preserving form)."""
    lines = ["The over-approximation is NOT yet proven. Fix the failures below, then call verify again.", ""]
    for c, r in results.items():
        name = Path(c).name
        if r.success:
            lines.append(f"[PASS] {name}")
            continue
        if r.cancelled:
            lines.append(f"[not run] {name} (skipped after another target failed; will re-check)")
            continue
        url = f"  (report: {r.job_url})" if r.job_url else ""
        if r.error:
            lines.append(f"[{name}]\n{r.error}{url}")
        for v in r.failures():
            msg = f": {v.assert_message}" if v.assert_message else ""
            lines.append(f"[FAIL] {name}  rule {v.rule} {v.status}{msg}{url}")
        for label, xml in (r.cex or {}).items():
            lines.append(f"  counterexample [{label}] — the REAL output on this input violates Phi "
                         f"(Phi is too strong here; WEAKEN the failing clause, do NOT require it away):\n{xml}")
    timed_out = [(Path(c).name, r) for c, r in results.items()
                 if any(v.status == "TIMEOUT" for v in r.failures())]
    if timed_out:
        lines.append("")
        lines.append("TIMEOUT: Phi is too HEAVY to prove. Replace an exact/nonlinear clause with a looser "
                     "property that still meets the goal (a range instead of the closed form), or pivot "
                     "Phi through a contract view getter. The prover's difficulty report localizes the "
                     "cost — use it, don't guess.")
        for name, r in timed_out:
            if r.difficulty:
                lines.append(f"[{name}] where the nonlinear cost is:")
                lines.append(r.difficulty)
    lines.append("")
    lines.append("Apply set_phi (not just render/inspect), run check_consistency, then call verify.")
    return "\n".join(lines)


def _format_verify_result(project: OverApproxProject, results: dict[str, V.VerifyResult]) -> str:
    """The `verify` tool's message: ALL-VERIFIED, or the failures + counterexamples/timeouts to fix."""
    if not results:
        return ("no provable targets (every target is void / multi-return). Nothing to verify — the "
                "summaries are unconstrained. Call result.")
    if all(r.success for r in results.values()):
        confs = ", ".join(Path(c).name for c in results)
        return (f"PROVER: ALL over-approx conformance rules VERIFIED.\nProven: {confs}.\nEach summary is a "
                "SOUND over-approximation — call result now (state each target's final Phi in words).")
    return "PROVER results — NOT yet proven:\n\n" + _refine_message(results)


def _record_verified(project: OverApproxProject, results: dict[str, V.VerifyResult]) -> None:
    """Mark each target whose conformance rule VERIFIED — snapshotting its proven Phi as the fallback."""
    passed = {v.rule for r in results.values() if r.success for v in r.rules if v.passed}
    for fn in project.provable_targets():
        if conformance_rule_name(fn) in passed:
            project.mark_verified(fn)


def _baseline_passing(url: str) -> set:
    """User rules that VERIFIED (did not violate) in the ORIGINAL run — the ones the summary must NOT
    break. From POU get_all_checks/get_violated_rules; prover built-ins dropped. vaas-dev needs AISS_ENV=dev."""
    import os
    if "vaas-dev" in url:
        os.environ.setdefault("AISS_ENV", "dev")
    from prover_output_utility import ProverOutputAPI
    api = ProverOutputAPI(use_local=False)
    def user(rn): return bool(rn) and "StaticCheck" not in rn and rn != "envfreeFuncsStaticCheck"
    allr = {c.rule_name for c in api.get_all_checks(url) if user(c.rule_name or "")}
    violated = {c.rule_name for c in api.get_violated_rules(url) if user(c.rule_name or "")}
    return allr - violated


def _write_regression_conf(project: OverApproxProject, cfg: OverApproxRefineConfig, rules: list) -> str:
    """Install each VERIFIED summary into the run's verify spec (`import "<fn>Summary.spec";`, exactly the
    driver's consumer-install) and write a conf that re-runs `rules` on the run's ORIGINAL scene with the
    summaries active. Leaves the spec edited — the caller restores it."""
    import copy, json
    from ..project import _set_perf
    spec_rel = cfg.setup_conf["verify"].split(":", 1)[1]
    spec_path = Path(cfg.sources_root) / spec_rel
    text = spec_path.read_text()
    for fn in project.verified:
        imp = f'import "{fn}Summary.spec";'
        if imp not in text:
            text = imp + "\n" + text
    spec_path.write_text(text)
    conf = copy.deepcopy(cfg.setup_conf)
    conf["prover_args"] = [a for a in conf.get("prover_args", []) if a != "-skipFormulaChecking"]
    conf["rule"] = rules
    conf["msg"] = "overapprox regression (summary installed)"
    conf = _set_perf(conf)
    cpath = f"{cfg.out_dir}/conf/regression.conf"
    Path(cpath).parent.mkdir(parents=True, exist_ok=True)
    Path(cpath).write_text(json.dumps(conf, indent=4))
    return cpath


async def _regression_check(project: OverApproxProject, cfg: OverApproxRefineConfig):
    """AFTER conformance passes: install the summaries + re-run the run's OWN rules. Returns (regressed
    rules, job_url) — a rule that PASSED at baseline but now fails means the summary is TOO COARSE."""
    if not cfg.run_url:
        return [], None
    baseline = await asyncio.to_thread(_baseline_passing, cfg.run_url)
    if not baseline:
        return [], None
    spec_rel = cfg.setup_conf["verify"].split(":", 1)[1]
    spec_path = Path(cfg.sources_root) / spec_rel
    pristine = spec_path.read_text()
    try:
        cpath = _write_regression_conf(project, cfg, sorted(baseline))
        common = dict(sources_root=cfg.sources_root, local=cfg.local,
                      certora_run_path=cfg.certora_run_path, disable_cache=cfg.disable_cache)
        results = await V.verify_all_early([cpath], **common)
        r = next(iter(results.values()), None)
        if r is None:
            return [], None
        return sorted(baseline & {v.rule for v in r.failures()}), r.job_url
    finally:
        spec_path.write_text(pristine)              # restore the run's spec (install was transient)


def _make_verify(project: OverApproxProject, cfg: OverApproxRefineConfig, budget: list[int]):
    """Bound closure for the agent's `verify` tool: write, prove every conformance conf, enrich failures,
    record the verified targets, and return it all as one message (run INLINE in the conversation).
    ENFORCES a prover-run budget (`budget[0]` remaining): once spent, refuses to run and tells the agent
    to finalize — the loop keeps the last VERIFIED Phi as the fallback, so this bounds cost without
    losing soundness. The count lives in `budget` (a 1-elem list) so the caller can inspect it after."""
    async def verify_now() -> str:
        if budget[0] <= 0:
            return ("BUDGET SPENT: no prover runs remain. Do NOT set_phi/verify again — finalize now: "
                    "call `result`. The tightest Phi that VERIFIED is retained and will be shipped; a "
                    "tighter-but-unproven Phi is discarded. State the final Phi(s) in your result.")
        budget[0] -= 1
        results = await _prove_and_verify(project, cfg)
        _record_verified(project, results)
        msg = _format_verify_result(project, results)
        if results and all(r.success for r in results.values()) and cfg and cfg.run_url:  # conformance PASSED -> regression gate
            regressed, rurl = await _regression_check(project, cfg)
            if regressed:
                msg = ("PROVER: conformance PASSED, but installing the summary REGRESSED the run's own "
                       "rules (they VERIFIED without it) -> Phi is TOO COARSE. TIGHTEN it so these pass "
                       "again; do NOT call result:\n  " + "\n  ".join(regressed)
                       + (f"\n  (regression run: {rurl})" if rurl else ""))
        return msg + f"\n\n(prover runs remaining: {budget[0]})"
    return verify_now


async def run_overapprox_loop(project: OverApproxProject, task: str, cfg: OverApproxRefineConfig, *,
                              llm: BaseChatModel | None = None, builder: Builder | None = None,
                              extra_tools: Iterable[BaseTool] = (), thread_id: str = "overapprox",
                              fill_steps: int = 120, max_prompt_tokens: int = 100_000,
                              max_verify_calls: int = 6,
                              callbacks: Iterable[BaseCallbackHandler] = (),
                              transcript_path: str | None = None) -> OverApproxRefineResult:
    """Composer-style single-conversation fill→prove→refine for the over-approx summaries. `verify` and
    the full-typecheck check_consistency are TOOLS the agent calls, so feedback arrives inline. TWO
    budgets bound the run: `fill_steps` caps the agent's tool-call recursion, and `max_verify_calls` caps
    the (minutes-each, cloud) PROVER runs — the verify tool refuses once spent, so tightening can't loop
    forever. At the end we RESTORE each target to its last-proven Phi (so a tighter-but-failing Phi left
    when the budget ran out is discarded) and take one authoritative verdict — a content-hash cache hit,
    since that Phi already proved. Dev: pass `llm=`; pipeline: `builder=env.builder_lite()` (in a
    with_handler scope) + `extra_tools=env source tools`."""
    if builder is None:
        if llm is None:
            raise ValueError("pass llm=<model> (dev) or builder=env.builder_lite() (pipeline)")
        builder = Builder().with_llm(llm, max_prompt_tokens=max_prompt_tokens)
    ckpt = InMemorySaver()
    budget = [max_verify_calls]
    deps = OverApproxDeps(project, full_typecheck=_make_full_typecheck(project, cfg),
                          verify=_make_verify(project, cfg, budget))
    graph = (build_overapprox_graph(builder, deps, extra_tools=extra_tools)
             .with_initial_prompt(task).compile_async(checkpointer=ckpt))
    try:
        await graph.ainvoke(OverApproxInput(input=[]),
                            {"configurable": {"thread_id": thread_id}, "recursion_limit": fill_steps,
                             "callbacks": list(callbacks)})
    except Exception:
        pass   # budget exhausted / API error — fall through to the authoritative verdict below
    finally:
        if transcript_path:
            await dump_transcript(ckpt, thread_id, transcript_path)
    # ship the tightest PROVEN Phi, not whatever the agent last set (it may have left a failing tighten).
    project.restore_best_verified()
    results = await _prove_and_verify(project, cfg)   # authoritative; cache hit (that Phi already proved)
    _record_verified(project, results)
    success = bool(results) and all(r.success for r in results.values())
    return OverApproxRefineResult(success=success, verified=sorted(project.verified), results=results)
