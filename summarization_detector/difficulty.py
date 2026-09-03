"""Fetch a completed job's difficulty signal WITHOUT the whole zipOutput or statsdata.json.

Reads the prover's own ranked NONLINEARITY HOTSPOTS from the run (via POU — treeView only, no tarball):

  rule_live_statistics_*.json -> "nonlinearity hotspots" node -> functions ranked by % contribution to
  nonlinear ops, each carrying a source file:line.

A function appears here ONLY IF its body was INLINED into the SMT problem — a summarized or havoc'd call
contributes no nonlinear ops. So the hotspot list is the real, in-problem, expensive bytecode: the
summarization candidates.

Note on call resolutions: `get_call_resolutions` is NOT used to find inlined calls. That table is built
only from applied-summary annotations (EVMVerifier CallResolutionTable.kt:161 ->
TACProgram.topoSortedSummaryStart), so an inlined call has NO row; POU additionally filters it to
unresolved (`[?]`) callees (prover_output_utility tree_parser.py:136). It reports the OPPOSITE of inlined —
already-havoc'd unresolved externals — so it cannot surface a summarization candidate.

`statsdata.json` is avoided (can be huge, and it carries the same nonlinear-op scores WITHOUT source
locations — those are joined from the call graph only in the rendered treeView). Best-effort: any failure
(missing POU / auth / network / schema drift) returns an empty report.
"""

import json
import re
from dataclasses import dataclass, field

_MAX_HOTSPOTS = 8          # a ranked pointer, not a dump
# Schema mirrors EVMVerifier's producers (Kotlin, cited so drift is traceable):
#   report/LiveCheckInfoNode.kt          -> node fields {label, value, children, jumpToDefinition,
#                                           severity, hSev}; jumpToDefinition = TreeViewLocation
#                                           {file, start:{line,col}, end:{line,col}}.
#   statistics/data/SingleDifficultyStats.kt:219-259 -> the "nonlinearity hotspots" node:
#       parent  label = "nonlinearity hotspots"
#       child   label = "function: $procId"
#               value = "contrib. to nonlinear ops: $x %" [ \n "contrib. to max polyn. degree: $y %"]
#               jumpToDefinition = callGraphInfo.procIdToSourceLocation[procId]
#     (the prover already keeps only procs with nlOps>10 || polydeg>5, sorted by nlOps+polydeg).
_HOTSPOTS_NODE_LABEL = "nonlinearity hotspots"
_HOTSPOT_FN_RE = re.compile(r"function:\s*(?P<fn>.+)", re.S)       # $procId (may contain spaces/quotes)
_HOTSPOT_PCT_RE = re.compile(r"nonlinear ops:\s*(?P<pct>\d+)\s*%")
# The SIBLING "path count hotspots" node (SingleDifficultyStats.kt, same shape as nonlinearity): functions
# ranked by % contribution to BRANCHING (path/loop count), each with a source file:line. This is the signal
# for path-explosion-bound rules (combinatorial loops over dynamic arrays) where the nonlinearity node is
# empty — e.g. a scene whose math is already summarized, so the residual cost is loop/branch count, not
# nonlinear ops. Populated whenever the nonlinearity one is (both are byproducts of the same VC-setup phase).
_BRANCHING_NODE_LABEL = "path count hotspots"
_BRANCHING_PCT_RE = re.compile(r"branching:\s*(?P<pct>\d+)\s*%")
# The per-call absolute PATH COUNT (SingleDifficultyStats.kt: `path count` child of each `call #N` node),
# e.g. "1", "200", or "approx. 2^51". The rule's total is the entry call's — the SCALE of the explosion,
# which the per-function % alone doesn't convey. We keep the max seen across the run's calls.
_PATH_COUNT_LABEL = "path count"
_POW2_RE = re.compile(r"2\s*\^\s*(\d+)")
_INT_RE = re.compile(r"\d+")


def _loc(node: dict) -> str:
    """`file:line` from a difficulty-tree node's jumpToDefinition (a dict, or the first of a list), or ""
    when absent. Shared by the static detector (`_parse_hotspots`) and the post-hoc profiler."""
    jd = node.get("jumpToDefinition")
    if isinstance(jd, list):
        jd = jd[0] if jd else None
    if isinstance(jd, dict) and jd.get("file"):
        return f"{jd['file']}:{jd.get('start', {}).get('line')}"
    return ""


def _path_count_value(s: str) -> float:
    """A `path count` string ("1", "200", "approx. 2^51") -> a comparable float (2^51 -> 2**51)."""
    m = _POW2_RE.search(s)
    if m:
        return 2.0 ** int(m.group(1))
    m = _INT_RE.search(s)
    return float(m.group()) if m else 0.0


@dataclass
class Hotspot:
    function: str            # e.g. "SomeLib.someNonlinearFn" (procId)
    pct: int                 # % contribution to the rule's nonlinear ops
    location: str            # "file:line" or ""


@dataclass
class DifficultyReport:
    hotspots: list[Hotspot] = field(default_factory=list)     # nonlinearity (% of nonlinear ops)
    branching: list[Hotspot] = field(default_factory=list)    # path count (% of branching)
    max_path_count: str = ""                                  # worst absolute path count seen (e.g. "approx. 2^51")

    def is_empty(self) -> bool:
        return not self.hotspots and not self.branching

    def format(self) -> str:
        """Render the ranked hotspots as a compact, source-located block plus a summarization playbook.
        Every listed function has its real body INLINED in the SMT problem (that is why it contributes
        nonlinear ops)."""
        if self.is_empty():
            return ""
        out: list[str] = []
        if self.hotspots:
            out.append("  nonlinearity hotspots (prover difficulty report — % of the rule's nonlinear ops; "
                       "each function's real body is INLINED in the problem):")
            for h in self.hotspots:
                at = f"  @{h.location}" if h.location else ""
                out.append(f"    {h.pct:3d}%  {h.function}{at}")
            out.append(
                "  -> NONDET the OFF-PATH hotspots (result not read by the checked output) to delete their "
                "nonlinear subproblem; if a hotspot stays inlined despite a `_.fn` wildcard NONDET, it is a "
                "LINKED target — summarize the CONCRETE contract: `function <Contract>.<fn>(...) external "
                "returns (...) => NONDET;` (add_nondet with contract=<Contract>). For ON-PATH math (the CUT "
                "method itself, or a getter the glue observes), mirror the scene's existing summary form so "
                "equality is congruence-trivial — do NOT NONDET it.")
        if self.branching:
            scale = f" — worst rule path count {self.max_path_count}" if self.max_path_count else ""
            out.append(f"  path-count hotspots (prover difficulty report — % of the rule's BRANCHING{scale}; "
                       "the cost is loop/path explosion, not math — relevant when a rule times out with FEW or "
                       "no nonlinearity hotspots):")
            for h in self.branching:
                at = f"  @{h.location}" if h.location else ""
                out.append(f"    {h.pct:3d}%  {h.function}{at}")
            out.append(
                "  -> NONDET a branching hotspot to delete its loop subproblem, subject to TWO soundness "
                "gates: (1) the prover REJECTS `=> NONDET` on a function returning a REFERENCE type (array / "
                "struct / bytes / string) -- summarize the nearest VALUE/VOID-returning caller/container that "
                "wraps its loop instead (e.g. a `_store...`/aggregator that returns an id or void). (2) NONDET "
                "drops a function's STATE WRITES, so NONDET-ing a state-mutating fn is sound ONLY if the "
                "property does not read the state it mutates; otherwise summarize it write-preservingly. Never "
                "NONDET a function whose derived value the property under test actually asserts on.")
        return "\n".join(out)


def _parse_hotspots(node, out: dict[str, Hotspot], label: str, pct_re: "re.Pattern[str]") -> None:
    """Walk a rule_live_statistics tree; collect the children of every node whose label is `label`
    (a `<X> hotspots` node), reading the `pct_re`-matched percentage from each child's value. Used for
    both the 'nonlinearity hotspots' and 'path count hotspots' nodes (same shape, different metric)."""
    if isinstance(node, dict):
        if node.get("label") == label:
            for c in node.get("children", []) or []:
                mfn = _HOTSPOT_FN_RE.search(str(c.get("label", "")))
                mpct = pct_re.search(str(c.get("value", "")))
                if not (mfn and mpct):
                    continue
                fn, pct = mfn.group("fn").strip(), int(mpct.group("pct"))
                loc = _loc(c)
                prev = out.get(fn)
                if prev is None or pct > prev.pct:      # dedupe across split rules, keep the worst
                    out[fn] = Hotspot(function=fn, pct=pct, location=loc)
        for c in node.get("children", []) or []:
            _parse_hotspots(c, out, label, pct_re)
    elif isinstance(node, list):
        for c in node:
            _parse_hotspots(c, out, label, pct_re)


def _scan_max_path_count(node, best: list) -> None:
    """Walk the tree; keep the max `path count` value seen (best = [float_value, original_string])."""
    if isinstance(node, dict):
        if node.get("label") == _PATH_COUNT_LABEL:
            v = node.get("value")
            s = (v[0] if isinstance(v, list) and v else v)
            if s is not None:
                val = _path_count_value(str(s))
                if val > best[0]:
                    best[0], best[1] = val, str(s).strip()
        for c in node.get("children", []) or []:
            _scan_max_path_count(c, best)
    elif isinstance(node, list):
        for c in node:
            _scan_max_path_count(c, best)


_LIVE_STATS_RE = re.compile(r"rule_live_statistics_(\d+)\.json")
_PROBE_MAX = 64            # fallback if the status doesn't reference the live-stats files by name


def _live_stats_indices(api, job_url: str) -> list[int]:
    """The rule_live_statistics_*.json indices for this job. POU's `fetch_job_treeview` bulk-download
    does NOT include these files (only rule_output_* + treeViewStatus), but they ARE served per-file by
    `fetch_treeview_output_by_filename`. Enumerate them from the treeViewStatus (which references the
    per-rule output files); fall back to a bounded probe if none are named."""
    try:
        idx = {int(n) for n in _LIVE_STATS_RE.findall(json.dumps(api.get_treeview_status(job_url)))}
        if idx:
            return sorted(idx)
    except Exception:
        pass
    return list(range(_PROBE_MAX))


def fetch_difficulty(job_url: str, limit: int | None = _MAX_HOTSPOTS) -> DifficultyReport:
    """Best-effort difficulty report for a completed job: the prover's ranked nonlinearity hotspots
    (function, % of nonlinear ops, source file:line). Returns an empty report on any error. Uses ONLY
    the treeView `rule_live_statistics_*.json` files (fetched per-file via POU's public API) — never
    statsdata.json or the output tarball. `limit` caps the returned hotspots (default `_MAX_HOTSPOTS`);
    pass ``None`` for the full ranked set."""
    report = DifficultyReport()
    if not job_url:
        return report
    try:
        from prover_output_utility import ProverOutputAPI
        api = ProverOutputAPI(use_local=False)
    except Exception:
        return report

    hs: dict[str, Hotspot] = {}          # nonlinearity
    br: dict[str, Hotspot] = {}          # path count / branching
    max_pc: list = [0.0, ""]             # [value, original string] of the worst absolute path count
    for n in _live_stats_indices(api, job_url):
        try:
            tree = api.fetch_treeview_output_by_filename(job_url, f"rule_live_statistics_{n}.json")
        except Exception:
            continue      # index not present (expected when probing) / transient fetch error
        _parse_hotspots(tree, hs, _HOTSPOTS_NODE_LABEL, _HOTSPOT_PCT_RE)
        _parse_hotspots(tree, br, _BRANCHING_NODE_LABEL, _BRANCHING_PCT_RE)
        _scan_max_path_count(tree, max_pc)
    report.max_path_count = max_pc[1]
    nl_ranked = sorted(hs.values(), key=lambda h: h.pct, reverse=True)
    br_ranked = sorted(br.values(), key=lambda h: h.pct, reverse=True)
    report.hotspots = nl_ranked if limit is None else nl_ranked[:limit]
    report.branching = br_ranked if limit is None else br_ranked[:limit]
    return report
