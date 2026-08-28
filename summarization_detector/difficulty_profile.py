"""Difficulty PROFILER — from a completed prover run, find the slow rules and attribute WHERE the prover
time goes, at source granularity.

The POST-HOC counterpart to `detect.py` (the static, before-the-rules predictor): given a real-property
run, it reads the prover's difficulty tree per slow rule (`rule_live_statistics_*.json`: nonlinearity /
path-count / memory hotspots, each with a source `file:line`), rolls the hotspots up by function, and
classifies each function by kind:

  cut        a function of the contract under test
  library    an inlined library (not a scene contract)
  external   a linked/real dependency contract
  cvl-model  an already-applied CVL summary (ghost) — contributes ~0

The contract under test and the scene's linked contracts are read from the job's treeViewStatus, so no
protocol-specific configuration is baked in. Reuses POU (`ProverOutputAPI`) as the `difficulty` module
does; best-effort and tolerant of schema drift.
"""

import re
from dataclasses import asdict, dataclass, field

# `duration` in treeViewStatus nodes is WALL SECONDS. A rule is "slow" (and gets profiled) when its
# duration reaches this threshold OR its status is TIMEOUT — the TIMEOUT check catches a rule that hit the
# run's global timeout whatever that cap was. Overridable via --min-minutes.
_DEFAULT_MIN_SECONDS = 300           # 5 min
_MAX_HOTSPOTS_PER_RULE = 4           # a ranked pointer per rule, not a dump
_HOTSPOT_PARENTS = {                 # difficulty-tree nodes whose children are per-function hotspots
    "nonlinearity hotspots": "nl",
    "path count hotspots": "path",
    "memory complexity hotspots": "mem",
}
_FN_RE = re.compile(r"function:\s*(?P<fn>.+)", re.S)
_PCT_RE = re.compile(r"(\d+)\s*%")
# procId of an applied CVL summary, e.g. "CVL/Ghost Function 'cvlPrice(id)'".
_CVL_PREFIXES = ("CVL/", "CVL ", "cvl")


@dataclass
class Hotspot:
    kind: str                 # "nl" | "path" | "mem"
    function: str             # procId, e.g. "Vault.computeAccountData"
    contract: str             # the part before the first '.', or "" for a CVL ghost
    pct: int                  # % contribution to this rule's ops of that kind
    location: str             # "file:line" or ""
    klass: str                # "cut" | "library" | "external" | "cvl-model" | "unknown"


@dataclass
class SlowRule:
    name: str
    status: str
    minutes: float
    split_progress: object    # int % or None
    path_count: str           # e.g. "approx. 2^127"
    nonlinearity: str         # e.g. "nonlinear ops: 286 max polyn. degree: 6"
    hotspots: list[Hotspot] = field(default_factory=list)


@dataclass
class FunctionRollup:
    function: str
    klass: str
    location: str
    nl_pct_sum: int           # summed nonlinear-op % across the slow rules it dominates (a rank key)
    rules: int                # how many slow rules it is a top hotspot in


@dataclass
class ProfileReport:
    job: str
    cut: str
    spec: str
    n_rules: int
    slow: list[SlowRule] = field(default_factory=list)
    by_class: dict = field(default_factory=dict)      # klass -> summed nl%
    by_function: list[FunctionRollup] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        out = [f"difficulty profile — {self.cut}  ({self.job})",
               f"  spec: {self.spec}   rules: {self.n_rules}   slow (>=threshold or TIMEOUT): {len(self.slow)}"]
        for r in self.slow:
            out.append(f"  [{r.status:8}] {r.minutes:6.1f}min  split={r.split_progress}%  {r.name[-64:]}")
            out.append(f"             path={r.path_count}  nl={r.nonlinearity[:46]}")
            for h in r.hotspots:
                out.append(f"       {h.kind:4} {h.pct:3d}%  [{h.klass:9}] {h.function[:52]}  {h.location}")
        out.append("  --- where the nonlinear ops go (by class) ---")
        for k, v in sorted(self.by_class.items(), key=lambda x: -x[1]):
            out.append(f"      {v:5d}%   {k}")
        out.append("  --- top functions by prover cost (ranked) ---")
        for f in self.by_function[:12]:
            out.append(f"      {f.nl_pct_sum:5d}%  [{f.klass:9}] {f.function}  ({f.rules} rules)  {f.location}")
        return "\n".join(out)


def _loc(node: dict) -> str:
    jd = node.get("jumpToDefinition")
    if isinstance(jd, dict):
        return f"{jd.get('file')}:{jd.get('start', {}).get('line')}"
    if isinstance(jd, list) and jd:
        return f"{jd[0].get('file')}:{jd[0].get('start', {}).get('line')}"
    return ""


def _classify(function: str, cut: str, scene_contracts: set[str]) -> tuple[str, str]:
    """Return (contract, klass) — `klass` is the hotspot's kind: cut / library / external / cvl-model."""
    fn = function.strip().strip("'\"")
    if fn.startswith(_CVL_PREFIXES):
        return "", "cvl-model"                         # an already-applied summary — nothing to do
    contract = fn.split(".", 1)[0].split("(", 1)[0].strip() if "." in fn else ""
    if not contract:
        return "", "unknown"
    if contract == cut:
        return contract, "cut"                          # a function OF the contract under test
    if contract in scene_contracts:
        return contract, "external"                     # a linked/real dependency contract in the scene
    return contract, "library"                          # not a scene contract -> an inlined library


def _parse_difficulty_tree(tree, cut: str, scene_contracts: set[str]) -> tuple[dict, list[Hotspot]]:
    """Walk a rule_live_statistics tree: collect the top metrics and the per-function hotspot children."""
    metrics: dict = {}
    hotspots: list[Hotspot] = []

    def walk(n, parent_hotspot_kind=None):
        if not isinstance(n, dict):
            return
        lbl = (n.get("label") or "").strip()
        val = (n.get("value") or "").strip()
        low = lbl.lower()
        if low.startswith("path count") and val and "path_count" not in metrics:
            metrics["path_count"] = val
        elif low.startswith("nonlinearity") and val and "nonlinearity" not in metrics:
            metrics["nonlinearity"] = val.replace("\n", " ")
        elif low.startswith("memory complexity") and val and "memory" not in metrics:
            metrics["memory"] = val.replace("\n", " ")
        if parent_hotspot_kind:
            m = _FN_RE.match(lbl)
            if m:
                fn = m.group(1).strip()
                pctm = _PCT_RE.search(val)
                pct = int(pctm.group(1)) if pctm else 0
                contract, klass = _classify(fn, cut, scene_contracts)
                hotspots.append(Hotspot(parent_hotspot_kind, fn, contract, pct, _loc(n), klass))
        child_kind = _HOTSPOT_PARENTS.get(low, parent_hotspot_kind if not low.endswith("hotspots") else None)
        for c in (n.get("children") or []):
            walk(c, child_kind)

    walk(tree if isinstance(tree, dict) else {"children": tree})
    return metrics, hotspots


def _iter_slow_leaves(rules, min_seconds):
    """Recurse the rule tree; yield leaf nodes that carry a live-stats file and are slow / TIMEOUT.
    Parametric children are prefixed with their parent's name; induction scaffolding names are skipped."""
    def rec(n, prefix=""):
        raw = n.get("name") or n.get("label") or "?"
        name = f"{prefix}::{raw}" if prefix else raw
        lci = n.get("LiveCheckInfo")
        dur = n.get("duration") or 0
        st = n.get("status")
        if isinstance(lci, str) and lci.endswith(".json") and (dur >= min_seconds or st == "TIMEOUT"):
            yield {"name": name, "status": st, "duration": dur, "lci": lci,
                   "split": n.get("splitProgress")}
        next_prefix = prefix if str(raw).startswith("Induction") else name
        for c in (n.get("children") or []):
            yield from rec(c, next_prefix)
    for r in rules:
        yield from rec(r)


def profile_job(job_url: str, *, min_seconds: int = _DEFAULT_MIN_SECONDS, cut: str | None = None,
                api=None) -> ProfileReport:
    """Profile one prover job: its slow rules and where (by source function/class) the nonlinear ops go.
    Best-effort — returns whatever it can fetch. `cut` overrides the contract-under-test (else read from
    the job's treeViewStatus). Pass a shared `api` to reuse a connection across jobs."""
    if api is None:
        from .sources import _aiss_env_for
        _aiss_env_for(job_url)
        from prover_output_utility import ProverOutputAPI
        api = ProverOutputAPI(use_local=False)
    tv = api.get_treeview_status(job_url)
    rules = tv.get("rules", []) if isinstance(tv, dict) else []
    cut = str(cut or (tv.get("contract") if isinstance(tv, dict) else "") or "")
    spec = tv.get("spec", "") if isinstance(tv, dict) else ""
    scene_contracts = {c.get("name") for c in (tv.get("availableContracts") or []) if c.get("name")}

    slow: list[SlowRule] = []
    by_class: dict = {}
    fn_agg: dict = {}       # function -> [nl_pct_sum, klass, location, rule_count]
    for leaf in sorted(_iter_slow_leaves(rules, min_seconds), key=lambda x: -x["duration"]):
        try:
            tree = api.fetch_treeview_output_by_filename(job_url, leaf["lci"])
        except Exception:
            continue
        metrics, hs = _parse_difficulty_tree(tree, cut, scene_contracts)
        nl = sorted([h for h in hs if h.kind == "nl"], key=lambda h: h.pct, reverse=True)
        pc = sorted([h for h in hs if h.kind == "path"], key=lambda h: h.pct, reverse=True)[:2]
        top = nl[:_MAX_HOTSPOTS_PER_RULE] + pc
        slow.append(SlowRule(leaf["name"], leaf["status"], leaf["duration"] / 60.0, leaf["split"],
                             metrics.get("path_count", "?"), metrics.get("nonlinearity", "?"), top))
        for h in nl:
            by_class[h.klass] = by_class.get(h.klass, 0) + h.pct
        # a function is a "target" if it is a top nonlinear hotspot of this rule (pct-weighted)
        for h in nl[:2]:
            slot = fn_agg.setdefault(h.function, [0, h.klass, h.location, 0])
            slot[0] += h.pct
            slot[3] += 1
            if h.location and not slot[2]:
                slot[2] = h.location

    by_function = sorted(
        (FunctionRollup(fn, v[1], v[2], v[0], v[3]) for fn, v in fn_agg.items()),
        key=lambda f: f.nl_pct_sum, reverse=True)
    return ProfileReport(job=job_url, cut=cut, spec=spec, n_rules=len(rules),
                         slow=slow, by_class=by_class, by_function=by_function)


def profile_jobs(job_urls: list[str], *, min_seconds: int = _DEFAULT_MIN_SECONDS,
                 cut: str | None = None) -> list[ProfileReport]:
    """Profile several jobs (e.g. every component of one autoprover run) with a shared POU connection."""
    if not job_urls:
        return []
    from .sources import _aiss_env_for
    _aiss_env_for(job_urls[0])
    from prover_output_utility import ProverOutputAPI
    api = ProverOutputAPI(use_local=False)
    return [profile_job(u, min_seconds=min_seconds, cut=cut, api=api) for u in job_urls]


def aggregate_by_class(reports: list[ProfileReport]) -> dict:
    """Sum the by-class nonlinear-op attribution across reports — the one-line 'where does the time go'."""
    total: dict = {}
    for r in reports:
        for k, v in r.by_class.items():
            total[k] = total.get(k, 0) + v
    return dict(sorted(total.items(), key=lambda x: -x[1]))


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(
        prog="difficulty-profile",
        description="From completed prover run(s), attribute slow-rule prover time to source functions "
                    "and classify each as cut / library / external / cvl-model.")
    p.add_argument("jobs", nargs="+", help="prover job URL(s) or hash(es) — e.g. every component of a run.")
    p.add_argument("--min-minutes", type=float, default=5.0, help="slow-rule threshold (default 5).")
    p.add_argument("--cut", default=None, help="override the contract under test (else from treeViewStatus).")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text.")
    a = p.parse_args(argv)
    reports = profile_jobs(a.jobs, min_seconds=int(a.min_minutes * 60), cut=a.cut)
    if a.json:
        print(json.dumps({"reports": [r.to_dict() for r in reports],
                          "aggregate_by_class": aggregate_by_class(reports)}, indent=2))
    else:
        for r in reports:
            print(r.format())
            print()
        agg = aggregate_by_class(reports)
        print("### aggregate — where the nonlinear ops go across all jobs (by class)")
        for k, v in agg.items():
            print(f"   {v:6d}%   {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
