"""Surviving-call-graph signal — the STRONGEST summarization predictor, from a cheap SANITY run.

A `satisfy sanity` run proves every external method merely EXITABLE, so the prover does not prune the
branches that reach a method's exit — the method bodies stay intact. When such a run also emits the
prover's `SurvivingCallGraph-*-postOptimize.json` (one per rule/entry method), each file lists the
internal functions and procedures that SURVIVE optimization for that entry point:

    { "rule": "sanity-<method>...", "phase": "postOptimize",
      "procedures":        [ {"callId", "procId": "C.f(sig)", "range": {...}} ],   # top-level, with source range
      "internalFunctions": [ {"name": "C.f(sig)", "summarizable": true} ] }        # inlined callees

So "which prover-hostile primitive is still in the optimized problem, reachable from which entry method"
is read directly — no SMT, no timeout. Two facts make it decisive:
  * The already-summarized math shows up as `CVL/Ghost Function '...'` (a stand-in), so RAW (non-ghost)
    survivors are exactly the UN-summarized candidates — the signal separates "done" from "to do".
  * Each internal function carries the prover's own `summarizable` flag.

On a real protocol's sanity run this flags, from the sanity gate alone, precisely the primitives the
human CVL_GEN agent otherwise discovered only after repeated multi-hour prover timeouts: exponentiation
with a symbolic exponent, an in-memory sort, and a word-wide bit scan.

The catalog is deliberately protocol-agnostic: GENERIC_RULES recognize a hostile OPERATION by conventional
primitive-name tokens (`exp`/`pow`, `fls`/`popcount`, `sort`, `mulDiv`, …), and a CURATED overlay maps
specific PUBLIC libraries to their known-good EXACT summaries (OpenZeppelin, Solady, Uniswap, PRB, public WAD/RAY libs).
This module classifies survivors and emits per-primitive candidate records (which entry methods reach it,
why it is hostile, whether an EXACT summary exists, and a candidate summary) — the hand-off list a
downstream summary step consumes. It does NOT itself apply any over-approximation.
"""

import re
from dataclasses import asdict, dataclass, field


# ── Hostile-primitive catalog: GENERIC rules + a CURATED overlay ──────────────
@dataclass(frozen=True)
class HostileCategory:
    key: str
    match: "re.Pattern[str]"     # generic operation-name pattern
    reason: str                  # why it is prover-hostile
    default_exact: bool          # does a sound, precise-for-all summary exist for the CATEGORY in general?
    default_candidate: str       # generic candidate summary when no curated entry applies


# Linear constant-scaling conversions (× a compile-time constant) are CHEAP — exclude so a fixed-point
# name doesn't misfire (e.g. `bpsToWad`/`toRay` = value · 1e‹k›). Generic across WAD/RAY ecosystems.
_LINEAR_SCALE = re.compile(
    r"\b(bpsToWad|bpsToRay|toWad|toRay|wadToRay|rayToWad|fromWad(Down|Up)?|fromRay(Down|Up)?|"
    r"fromBps(Down|Up)?|scaleBy|normalizeDecimals)\b", re.I)

# GENERIC operation categories — matched on conventional primitive-name tokens, no project names.
GENERIC_RULES: tuple[HostileCategory, ...] = (
    HostileCategory(
        key="bitwise-scan",
        match=re.compile(r"(?:\b|_)(fls|flz|clz|ctz|msb|lsb)\b|pop[_]?count|findLastSet|findFirstSet|bitLen",
                         re.I),
        reason="word-wide bit scan / population count — under-specified bitwise ops; a major imprecision "
               "and timeout source when inlined",
        default_exact=False,
        default_candidate="=> NONDET (over-approx; sound when the property does not read the exact "
                          "bit-scan result). For EXACT, a symbolic-model generator can build a conformance-proven bit model.",
    ),
    HostileCategory(
        key="in-memory-sort",
        match=re.compile(r"(?:\b|_)(sort|quickSort|mergeSort|heapSort|insertionSort)(ByKey|Asc\w*|Desc\w*)?\b",
                         re.I),
        reason="in-place permutation sort over an in-memory list — unrolls into the loop bound; expensive",
        default_exact=False,
        default_candidate="=> NONDET (over-approx of the ordering; sound when no property constrains the "
                          "sorted order/values). No sound EXACT curated form in general.",
    ),
    HostileCategory(
        key="symbolic-exp",
        # `exp`/`pow`/`rpow` as a whole word OR a camelCase segment (`…Exp`, `expWad`, …), so a private
        # function name is matched structurally without being hard-coded.
        match=re.compile(r"(?:\b|_)(exp|pow|rpow|power)(?=[_A-Z]|\b)|"
                         r"(?<=[a-z])(Exp|Pow|Rpow|Power)(?=[_A-Z(]|\b)"),
        reason="exponentiation with a symbolic exponent — an unrolled loop; prover-hostile",
        default_exact=False,
        default_candidate="=> NONDET, unless the base/exponent is the decimal-scaling shape "
                          "(a base-b raised to a token's `decimals`), in which case an EXACT "
                          "powers table applies.",
    ),
    HostileCategory(
        key="nonlinear-mulDiv",
        match=re.compile(r"(?:\b|_)(mulDiv\w*|fullMulDiv\w*|mulWad\w*|divWad\w*|rayMul\w*|rayDiv\w*|"
                         r"wadMul\w*|wadDiv\w*|percentMul\w*|percentDiv\w*)\b", re.I),
        reason="256/512-bit multiply-divide of two symbolic operands — nonlinear SMT",
        default_exact=True,
        default_candidate="=> a mulDiv summary (EXACT floor/ceil of x·y/scale). A curated one usually "
                          "exists (OZ_Math / FixedPointMathLib / PRB / WAD-RAY libs).",
    ),
)


@dataclass(frozen=True)
class CuratedEntry:
    match: "re.Pattern[str]"     # a specific "Contract.func(sig)" pattern
    category: str                # one of the GENERIC_RULES keys
    exact_summary: str           # the known-good EXACT summary text
    note: str = ""


# CURATED overlay — specific PUBLIC libraries → known EXACT summaries. Grows over time. Only public,
# widely-used third-party libraries belong here (OpenZeppelin, Solady/Solmate, Uniswap, PRB, the public
# WAD/RAY fixed-point libraries, …). A protocol's own private math is caught by the GENERIC rules above.
CURATED_SUMMARIES: tuple[CuratedEntry, ...] = (
    CuratedEntry(
        match=re.compile(r"\b(WadRayMath|PercentageMath)\.(ray|wad|percent)", re.I),
        category="nonlinear-mulDiv",
        exact_summary="=> WAD/RAY fixed-point mulDiv summary (EXACT floor/ceil of x·y/scale).",
    ),
    CuratedEntry(
        match=re.compile(r"\b(Math|MathUpgradeable)\.mulDiv\b"),
        category="nonlinear-mulDiv",
        exact_summary="=> OZ_Math.mulDiv curated summary (EXACT).",
    ),
    CuratedEntry(
        match=re.compile(r"\bFixedPointMathLib\.|\bFullMath\.mulDiv\b|\bPRBMath"),
        category="nonlinear-mulDiv",
        exact_summary="=> FixedPointMathLib / FullMath / PRB curated summary (EXACT).",
    ),
    # sort / bitwise have no sound EXACT curated form — the over-approx candidate from the generic rule
    # is carried through.
)


@dataclass(frozen=True)
class HostileMatch:
    category: str
    reason: str
    exact_summary_available: bool
    candidate_summary: str
    curated: bool                # True → the candidate summary came from the curated overlay


@dataclass
class HostileCandidate:
    name: str                    # "Contract.func(sig)"
    category: str
    reason: str
    exact_summary_available: bool
    candidate_summary: str
    curated: bool = False        # candidate summary came from the curated overlay (else a generic default)
    reaching_methods: list[str] = field(default_factory=list)  # entry methods whose optimized TAC keeps it
    summarizable: bool = True    # the prover's own flag (all reaching sites agree)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SurvivingReport:
    candidates: list[HostileCandidate] = field(default_factory=list)
    n_methods: int = 0           # entry methods analyzed
    already_summarized: list[str] = field(default_factory=list)  # CVL-ghost hostile (no action)

    def is_empty(self) -> bool:
        return not self.candidates

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        out = [f"surviving-hostile primitives (from {self.n_methods} sanity entry methods) — "
               "summarization candidates:"]
        if self.is_empty():
            out.append("  (none)")
        for c in sorted(self.candidates, key=lambda x: (-len(x.reaching_methods), x.name)):
            tag = "EXACT available" if c.exact_summary_available else "over-approx only"
            src = "curated" if c.curated else "generic"
            out.append(f"  [{c.category:16s}] {c.name}  — reaches {len(c.reaching_methods)} method(s)  "
                       f"[{tag}; {src}]")
            out.append(f"       why: {c.reason}")
            out.append(f"       candidate: {c.candidate_summary}")
        if self.already_summarized:
            out.append(f"  already summarized (CVL ghost — no action): {len(self.already_summarized)}")
        return "\n".join(out)


def _is_ghost(name: str) -> bool:
    """A `CVL/Ghost Function '...'` procId is an already-applied summary stand-in, not raw code."""
    return name.strip().startswith(("CVL/", "CVL "))


def classify(name: str) -> HostileMatch | None:
    """Resolve a solidity function name to a hostile match, or None. A GENERIC operation rule must fire
    first (that is what makes it protocol-agnostic); a CURATED overlay entry, if any, then upgrades the
    candidate to a known EXACT summary. Linear constant-scaling conversions are excluded up front."""
    if _LINEAR_SCALE.search(name):
        return None
    cat = next((c for c in GENERIC_RULES if c.match.search(name)), None)
    if cat is None:
        return None
    cur = next((e for e in CURATED_SUMMARIES if e.match.search(name)), None)
    if cur is not None:
        return HostileMatch(cat.key, cat.reason, exact_summary_available=True,
                            candidate_summary=cur.exact_summary, curated=True)
    return HostileMatch(cat.key, cat.reason, exact_summary_available=cat.default_exact,
                        candidate_summary=cat.default_candidate, curated=False)


def parse_surviving_graph(obj: dict) -> tuple[str, list[tuple[str, bool]]]:
    """From one `SurvivingCallGraph-*.json`, return (entry_method, [(function_name, summarizable), …]).
    Merges `procedures` (procId) and `internalFunctions` (name + summarizable)."""
    rule = obj.get("rule", "")
    entry = rule
    m = re.match(r"sanity-(?P<sig>.+?)-Satisfy", rule)   # `sanity-<method>-Satisfy_...`
    if m:
        entry = m.group("sig")
    names: list[tuple[str, bool]] = []
    for p in obj.get("procedures", []) or []:
        pid = p.get("procId")
        if pid:
            names.append((pid, True))
    for f in obj.get("internalFunctions", []) or []:
        nm = f.get("name") or f.get("procId")
        if nm:
            names.append((nm, bool(f.get("summarizable", True))))
    return entry, names


def scan_surviving(graphs: list[dict]) -> SurvivingReport:
    """Classify the raw (non-ghost) hostile primitives that survive across a set of postOptimize
    surviving-call-graph files (one per entry method). Aggregates reaching methods per primitive."""
    by_name: dict[str, HostileCandidate] = {}
    ghosts: set[str] = set()
    methods: set[str] = set()
    for g in graphs:
        if (g.get("phase") or "").lower() not in ("postoptimize", ""):
            continue                                  # postOptimize is the signal; skip a preOptimize file
        entry, names = parse_surviving_graph(g)
        methods.add(entry)
        for name, summarizable in names:
            match = classify(name)
            if match is None:
                continue
            if _is_ghost(name):
                ghosts.add(name)                      # already-summarized hostile — no action
                continue
            c = by_name.get(name)
            if c is None:
                c = HostileCandidate(
                    name=name, category=match.category, reason=match.reason,
                    exact_summary_available=match.exact_summary_available,
                    candidate_summary=match.candidate_summary, curated=match.curated,
                    reaching_methods=[], summarizable=summarizable)
                by_name[name] = c
            if entry not in c.reaching_methods:
                c.reaching_methods.append(entry)
            c.summarizable = c.summarizable and summarizable
    return SurvivingReport(candidates=list(by_name.values()), n_methods=len(methods),
                           already_summarized=sorted(ghosts))


# ── Live fetch (POU) ──────────────────────────────────────────────────────────
def fetch_surviving_postoptimize(job_url: str) -> list[dict]:
    """Fetch every `SurvivingCallGraph-*-postOptimize.json` for a run via POU's single-file endpoint,
    using the `survivingCallGraph_map.json` manifest. Returns [] on any error (best-effort). Needs a run
    that emitted the surviving-call-graph collector (a prover-branch / flagged feature)."""
    import json
    try:
        from .sources import _aiss_env_for
        _aiss_env_for(job_url)
        from prover_output_utility import ProverOutputAPI
        api = ProverOutputAPI(use_local=False)
        raw = api.fetch_output_file(job_url, "survivingCallGraph_map.json")  # type: ignore[attr-defined]
        manifest = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    out: list[dict] = []
    for files in manifest.values():
        for fn in files:
            if "postOptimize" not in fn:
                continue
            try:
                content = api.fetch_output_file(job_url, fn)  # type: ignore[attr-defined]
                out.append(json.loads(content) if isinstance(content, str) else content)
            except Exception:
                continue
    return out


def detect_surviving(job_url: str) -> SurvivingReport:
    """End-to-end: fetch a run's postOptimize surviving graphs and classify the hostile candidates."""
    return scan_surviving(fetch_surviving_postoptimize(job_url))


def main(argv: list[str] | None = None) -> int:
    """`python -m summarization_detector.surviving <job-url|hash> [--json]` — classify a run's surviving
    hostile primitives into summarization candidates (the CVL_GEN hand-off list)."""
    import argparse
    import json
    p = argparse.ArgumentParser(
        prog="surviving-detector",
        description="From a sanity run that emitted SurvivingCallGraph-*-postOptimize.json, list the raw "
                    "prover-hostile primitives that survive optimization (summarization candidates).")
    p.add_argument("job", help="prover job URL or hash (the sanity run with surviving-graph dumps).")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text.")
    a = p.parse_args(argv)
    from .surviving import detect_surviving
    rep = detect_surviving(a.job)
    print(json.dumps(rep.to_dict(), indent=2) if a.json else rep.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
