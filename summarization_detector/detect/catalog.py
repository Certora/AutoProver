"""Signal 4 — the prover-hostile operation catalog (generic name rules + curated public-library overlay)
and aggregation of the surviving hostile primitives from the postOptimize call graphs."""
import re
from dataclasses import dataclass

from .model import _is_cvl_ghost


# ---------------------------------------------------------------- signal 4: surviving hostile primitives
# From a sanity run's postOptimize SurvivingCallGraph dumps (one per entry method): the functions still in
# the optimized TAC. A RAW (non-ghost) prover-hostile primitive that survives is a summarization candidate;
# an already-applied summary appears as a `CVL/Ghost Function` stand-in and is excluded HERE. It is NOT
# ignored, though: a ghost still contributing nonlinear ops / path count is flagged by the `already-summarised`
# signal (in `fuse`, off the difficulty hotspots) as a coarsening target — signal 4 only covers RAW code.
# GENERIC_RULES recognize the hostile OPERATION by conventional primitive-name tokens (protocol-agnostic);
# a CURATED overlay maps specific PUBLIC libraries to a suggested OVER-/UNDER-approximation (never an exact
# summary — that is autosetup's job; see CURATED_SUMMARIES).
@dataclass(frozen=True)
class HostileCategory:
    key: str
    match: "re.Pattern[str]"     # generic operation-name pattern
    reason: str                  # why it is prover-hostile


# Linear constant-scaling conversions (× a compile-time constant) are cheap — exclude so a fixed-point
# name doesn't misfire (e.g. `bpsToWad` = value·1e‹k›).
_LINEAR_SCALE = re.compile(
    r"\b(bpsToWad|bpsToRay|toWad|toRay|wadToRay|rayToWad|fromWad(Down|Up)?|fromRay(Down|Up)?|"
    r"fromBps(Down|Up)?|scaleBy|normalizeDecimals)\b", re.I)

# GENERIC operation categories — matched on conventional primitive-name tokens, no project names. A generic
# match reports WHAT (category + why) only; it suggests NO summary — the consumer writes the one that is
# sound for the property at hand.
GENERIC_RULES: tuple[HostileCategory, ...] = (
    HostileCategory(
        key="bitwise-scan",
        match=re.compile(r"(?:\b|_)(fls|flz|clz|ctz|msb|lsb)\b|pop[_]?count|findLastSet|findFirstSet|bitLen",
                         re.I),
        reason="word-wide bit scan / population count — under-specified bitwise ops; a major imprecision "
               "and timeout source when inlined",
    ),
    HostileCategory(
        key="in-memory-sort",
        match=re.compile(r"(?:\b|_)(sort|quickSort|mergeSort|heapSort|insertionSort)(ByKey|Asc\w*|Desc\w*)?\b",
                         re.I),
        reason="in-place permutation sort over an in-memory list — unrolls into the loop bound; expensive",
    ),
    HostileCategory(
        key="symbolic-exp",
        match=re.compile(r"(?:\b|_)(exp|pow|rpow|power)(?=[_A-Z]|\b)|(?<=[a-z])(Exp|Pow|Rpow|Power)(?=[_A-Z(]|\b)"),
        reason="exponentiation with a symbolic exponent — an unrolled loop; prover-hostile",
    ),
    HostileCategory(
        key="nonlinear-mulDiv",
        match=re.compile(r"(?:\b|_)(mulDiv\w*|fullMulDiv\w*|mulWad\w*|divWad\w*|rayMul\w*|rayDiv\w*|"
                         r"wadMul\w*|wadDiv\w*|percentMul\w*|percentDiv\w*)\b", re.I),
        reason="256/512-bit multiply-divide of two symbolic operands — nonlinear SMT",
    ),
)

# The catalog categories double as SIGNALS: a catalog-matched candidate carries its category
# (`in-memory-sort`, `bitwise-scan`, …) directly in `signals` rather than a generic `primitive` + a
# separate `category` field. This set is how the per-category cap groups them all under "primitive".
_PRIMITIVE_CATEGORIES = frozenset(c.key for c in GENERIC_RULES)


@dataclass(frozen=True)
class CuratedEntry:
    match: "re.Pattern[str]"     # a specific "Contract.func(sig)" pattern for a known PUBLIC library
    category: str                # one of the GENERIC_RULES keys
    summary: str                 # the concrete summary text (an OVER-/UNDER-approximation, INCLUDING its
                                 # soundness caveat) — never an exact summary; those are autosetup's job


# CURATED overlay — specific PUBLIC libraries → a suggested OVER-/UNDER-approximation the consumer can adopt
# (and decide fits its rules). Deliberately NOT exact / meaning-preserving summaries: those are autosetup's
# job and are applied there (an exactly-summarized call never reaches the detector as hostile). So a public
# library whose only sound-and-useful summary is exact (e.g. WadRayMath / OZ Math / FixedPointMathLib mulDiv)
# is NOT listed here — it is still flagged as toxic by the GENERIC `nonlinear-mulDiv` rule and by the
# nonlinearity hotspots (signal 1), just with no over-approx suggestion. Only entries offering a genuine
# over-/under-approximation belong here; a protocol's own private math is caught by the GENERIC rules.
CURATED_SUMMARIES: tuple[CuratedEntry, ...] = (
    CuratedEntry(
        match=re.compile(r"\bLibBit\.(popCount|fls|flz|clz|ctz|ffs|findLastSet|findFirstSet)\b"),
        category="bitwise-scan",
        summary="=> Solady LibBit bit-scan: replace the word-wide bit loop with a NONDET result bounded to "
                "the op's range (popCount 0..256; fls/clz/ctz/ffs a bit index 0..255) — a sound "
                "over-approximation.",
    ),
)


@dataclass(frozen=True)
class HostileMatch:
    category: str
    reason: str
    candidate_summary: str       # a concrete summary from the curated overlay, or "" for a generic match
    curated: bool


def classify_hostile(name: str) -> HostileMatch | None:
    """Resolve a solidity function name to a hostile match, or None. A GENERIC operation rule fires first
    (protocol-agnostic) and reports only WHAT (category + why); a CURATED overlay entry, if any, attaches a
    concrete summary. Linear constant-scaling conversions are excluded up front."""
    if _LINEAR_SCALE.search(name):
        return None
    cat = next((c for c in GENERIC_RULES if c.match.search(name)), None)
    if cat is None:
        return None
    cur = next((e for e in CURATED_SUMMARIES if e.match.search(name)), None)
    if cur is not None:
        return HostileMatch(cat.key, cat.reason, candidate_summary=cur.summary, curated=True)
    return HostileMatch(cat.key, cat.reason, candidate_summary="", curated=False)


def _entry_of_rule(rule: str) -> str:
    """The entry-method name from a sanity rule name. Each method yields TWO surviving graphs — the
    `-Satisfy_sanity_check_failed_...` (reachability) and the `-Assertions` (assertion) run — so strip
    either suffix to the bare `<method>`; both then dedup to one entry (`sanity-<method>-Satisfy...` and
    `sanity-<method>-Assertions` -> `<method>`)."""
    m = re.match(r"sanity-(?P<sig>.+?)-(?:Satisfy|Assertions)", rule or "")
    return m.group("sig") if m else (rule or "")


def _surviving_names(graph: dict) -> list[tuple[str, bool]]:
    """(function_name, summarizable) for every procedure/internal function in one SurvivingCallGraph."""
    names: list[tuple[str, bool]] = []
    for p in graph.get("procedures", []) or []:
        if p.get("procId"):
            names.append((p["procId"], True))
    for f in graph.get("internalFunctions", []) or []:
        nm = f.get("name") or f.get("procId")
        if nm:
            names.append((nm, bool(f.get("summarizable", True))))
    return names


def surviving_hostile(graphs: list[dict]) -> dict[str, dict]:
    """Aggregate raw (non-ghost) hostile primitives across postOptimize SurvivingCallGraph dumps. Returns
    function -> {category, reason, reaching_methods, summarizable, candidate_summary} (candidate_summary is
    "" for a generic match, the curated text for a curated one). A `CVL/Ghost` survivor is an
    already-applied summary and is skipped."""
    out: dict[str, dict] = {}
    for g in graphs:
        if (g.get("phase") or "").lower() not in ("postoptimize", ""):
            continue
        entry = _entry_of_rule(g.get("rule", ""))
        for name, summarizable in _surviving_names(g):
            if _is_cvl_ghost(name):
                continue
            m = classify_hostile(name)
            if m is None:
                continue
            rec = out.get(name)
            if rec is None:
                rec = out[name] = {"category": m.category, "reason": m.reason, "reaching_methods": [],
                                   "summarizable": True, "candidate_summary": m.candidate_summary}
            if entry not in rec["reaching_methods"]:
                rec["reaching_methods"].append(entry)
            rec["summarizable"] = rec["summarizable"] and summarizable
    return out


