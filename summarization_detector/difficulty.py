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
_HOTSPOT_FN_RE = re.compile(r"function:\s*(?P<fn>.+)")             # $procId (may contain spaces/quotes)
_HOTSPOT_PCT_RE = re.compile(r"nonlinear ops:\s*(?P<pct>\d+)\s*%")


@dataclass
class Hotspot:
    function: str            # e.g. "SomeLib.someNonlinearFn" (procId)
    pct: int                 # % contribution to the rule's nonlinear ops
    location: str            # "file:line" or ""


@dataclass
class DifficultyReport:
    hotspots: list[Hotspot] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.hotspots

    def format(self) -> str:
        """Render the ranked hotspots as a compact, source-located block plus a summarization playbook.
        Every listed function has its real body INLINED in the SMT problem (that is why it contributes
        nonlinear ops)."""
        if self.is_empty():
            return ""
        out = ["  nonlinearity hotspots (prover difficulty report — % of the rule's nonlinear ops; each "
               "function's real body is INLINED in the problem):"]
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
        return "\n".join(out)


def _parse_hotspots(node, out: dict[str, Hotspot]) -> None:
    """Walk a rule_live_statistics tree; collect the children of every 'nonlinearity hotspots' node."""
    if isinstance(node, dict):
        if node.get("label") == _HOTSPOTS_NODE_LABEL:
            for c in node.get("children", []) or []:
                mfn = _HOTSPOT_FN_RE.search(str(c.get("label", "")))
                mpct = _HOTSPOT_PCT_RE.search(str(c.get("value", "")))
                if not (mfn and mpct):
                    continue
                fn, pct = mfn.group("fn").strip(), int(mpct.group("pct"))
                jd = c.get("jumpToDefinition") or {}
                loc = ""
                if isinstance(jd, dict) and jd.get("file"):
                    loc = f"{jd['file']}:{jd.get('start', {}).get('line')}"
                prev = out.get(fn)
                if prev is None or pct > prev.pct:      # dedupe across split rules, keep the worst
                    out[fn] = Hotspot(function=fn, pct=pct, location=loc)
        for c in node.get("children", []) or []:
            _parse_hotspots(c, out)
    elif isinstance(node, list):
        for c in node:
            _parse_hotspots(c, out)


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

    hs: dict[str, Hotspot] = {}
    for n in _live_stats_indices(api, job_url):
        try:
            _parse_hotspots(api.fetch_treeview_output_by_filename(job_url, f"rule_live_statistics_{n}.json"), hs)
        except Exception:
            continue      # index not present (expected when probing) / transient fetch error
    ranked = sorted(hs.values(), key=lambda h: h.pct, reverse=True)
    report.hotspots = ranked if limit is None else ranked[:limit]
    return report
