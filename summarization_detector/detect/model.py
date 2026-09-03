"""Runtime dataclasses, scoring constants/helpers, and tool-wide low-level helpers (path normalization,
AST-span primitives, procId strings) — the detector's vocabulary.

Leaf module: every other detect submodule imports from here; it imports nothing from them.
"""
import re
from collections.abc import Callable
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path

from certora_autosetup.solidity_ast import parse_src

from ..difficulty import _path_count_value


# ---------------------------------------------------------------- shared path + AST-span primitives
def _project_relative(p: str, sources_root: str | Path | None) -> str:
    """Normalize a solc source-unit path to project-relative so every candidate's `file` is uniform (solc
    records a MIX of relative and absolute keys depending on how each was imported/remapped). Certora fetches
    every source under `.certora_sources/`, so the segment after it IS the project path — key on that first:
    it is robust to the temp-dir symlink (macOS `tempfile` yields `/var/...` while solc resolves the same
    file to `/private/var/...`) that makes a plain `relative_to(sources_root)` throw and leak the absolute
    path. Falls back to `relative_to` (with a resolved retry), else returns `p` unchanged (already relative /
    outside the root)."""
    marker = ".certora_sources/"
    i = p.rfind(marker)
    if i != -1:
        return p[i + len(marker):]
    if sources_root is None:
        return p
    try:
        return str(Path(p).relative_to(Path(sources_root)))
    except ValueError:
        try:                                            # resolve both to defeat the /var -> /private/var symlink
            return str(Path(p).resolve().relative_to(Path(sources_root).resolve()))
        except ValueError:
            return p                                    # already relative / not under the root — leave as-is



def _span3(node: dict) -> tuple[int, int, int] | None:
    """`src` = "offset:length:fileId" -> (start, end, fileId). fileId distinguishes the source files that
    share one compilation unit's node-id space. Reuses `certora_autosetup`'s canonical `src` parser (which
    yields `(offset, length, file_index)`); we carry `end = offset + length` and tolerate a missing/malformed
    `src` as None."""
    src = node.get("src")
    if not isinstance(src, str):
        return None
    try:
        loc = parse_src(src)
    except ValueError:
        return None
    return loc.offset, loc.offset + loc.length, loc.file_index


def _span(node: dict) -> tuple[int, int] | None:
    """The node's `src` byte span `(start, end)` — `_span3` without the fileId. All nodes within one
    source file share a fileId, so containment by (start, end) alone attributes a call to its function."""
    r = _span3(node)
    return (r[0], r[1]) if r else None


def _min_span(off: int, spans: list, bounds: Callable[[tuple], tuple[int, int]]) -> tuple | None:
    """The smallest entry in `spans` whose `(start, end) = bounds(entry)` contains `off` — the innermost
    enclosing span. Shared scan behind `_tightest` and `_enclosing` (they differ only in tuple shape)."""
    best: tuple | None = None
    best_w = 0
    for entry in spans:
        s, e = bounds(entry)
        if s <= off < e and (best is None or (e - s) < best_w):
            best, best_w = entry, e - s
    return best


@dataclass
class HashSignal:
    """Signal-2 record: a function whose body performs semantic hashing/encoding (build/points-to cost
    the SMT difficulty report cannot see). Mutability/visibility come from the same FunctionDefinition
    node — the classifier's pure/view test needs no separate source."""
    function: str                 # "Contract.fn" ("fn" for a file-level free function)
    contract: str                 # owning contract/library ("" for a free function)
    name: str
    mutability: str               # "pure" | "view" | "payable" | "nonpayable" | ""
    visibility: str               # "external" | "public" | "internal" | "private"
    patterns: tuple[str, ...]     # the hashing/encoding calls found, e.g. ("keccak256", "abi.encodePacked")
    file: str                     # absolute source path
    is_dependency: bool           # under a lib/ dependency tree (deprioritize vs project src/)
    dynamic_input: bool = False   # hashes UNBOUNDED-length data (bytes/string/dynamic array) — the costly
                                  # kind (the prover models up to hashing_length_bound bytes). A hash over
                                  # only fixed-size fields (the typical EIP-712 digest) is bounded/cheap.


# Output caps — keep the report (and the prompt it renders into) bounded. The magnitude signals (nonlinear
# degree, branching path count) are normalized to 0..100 across the run's candidates and SUMMED across a
# candidate's signals (so a multi-signal candidate can exceed 100); surviving/hashing add flat amounts. Even
# so the mix is not fully cross-category comparable, so each category is capped on its own rather than by a
# single cross-category top-N (which would let one category crowd out the surviving/hashing targets).
MAX_PER_CATEGORY = {"primitive": 10, "nonlinear": 10, "hashing": 10, "already-summarised": 10, "branching": 10}
NONLINEAR_MIN_PCT = 15       # drop difficulty hotspots below this % of the rule's nonlinear ops (long tail)
BRANCHING_MIN_PCT = 15       # drop path-count hotspots below this % of the rule's branching (long tail)
# The % floor is only a within-rule share. The branching signal is for path EXPLOSION, so also require the
# paths this function CONTRIBUTES (2^(share% of the rule's branching bits), the number shown in the evidence)
# to clear an absolute floor — a function contributing a handful of paths is not a meaningful source of the
# explosion, even if the rule as a whole is large. Contributed <= rule total, so this subsumes a rule floor.
BRANCHING_MIN_PATHS = 16
# The same for nonlinearity: the % floors above are within-rule shares, so a big share of a barely-nonlinear
# rule (e.g. an already-applied summary that adds ~2 ops in a degree-3 rule) still clears them. Also require
# an absolute severity — the magnitude `degree x contributed_ops` — to clear a floor. Using the product (not
# ops or degree alone) keeps a high-degree-but-few-ops rule while dropping a low-degree, few-op one.
NONLINEAR_MIN_MAGNITUDE = 12


def _nl_magnitude(degree: int, nl_ops: int, pct: int) -> float:
    """A nonlinear candidate's RAW severity (normalized later across the run's nonlinear candidates): the
    rule's absolute max polynomial degree — the SCALE of the nonlinearity — times the ABSOLUTE nonlinear ops
    this function contributes (``nl_ops * pct/100`` — the same count shown in the evidence). Using the
    absolute count, not the within-rule share %, keeps the score consistent with the displayed ops and
    comparable across rules of different size (a big share of a tiny rule must not out-rank a smaller share
    of a huge one). Falls back to the share (``pct``) when the absolute op count is unavailable (older
    data); ``max(degree, 1)`` likewise degrades to ranking by ops when the degree is unavailable."""
    return max(degree, 1) * (nl_ops * pct / 100.0 if nl_ops else pct)


def _br_magnitude(path_count: str, pct: int) -> float:
    """A branching candidate's RAW severity (normalized later across the run's branching candidates): the
    ``log2`` of the rule's absolute path count — the SCALE of the explosion — times the within-rule
    contribution %. Falls back to the % alone (severity 1) when no path count is attached."""
    from math import log2
    v = _path_count_value(path_count)
    severity = log2(v) if v > 1 else 1.0
    return severity * pct


def _nl_ops_contributed(nl_ops: int, pct: int) -> int:
    """The nonlinear ops attributable to this function for the evidence line: its within-rule % of the
    rule's absolute count. Nonlinear ops ADD, so the share is a direct fraction."""
    return round(nl_ops * pct / 100.0)


def _paths_contributed_value(path_count: str, pct: int) -> float:
    """The number of paths attributable to this function: ``2 ** (pct% of the rule's branching bits)`` —
    paths MULTIPLY, so the function's share is of the log, not of the count. This (not the rule's total) is
    what measures whether the function is a meaningful source of the path explosion."""
    from math import log2
    v = _path_count_value(path_count)
    if v <= 1:
        return 1.0
    return 2.0 ** (log2(v) * pct / 100.0)


def _paths_contributed(path_count: str, pct: int) -> str:
    """`_paths_contributed_value` for the evidence line — an int for small counts, ``approx. 2^k`` for large."""
    from math import log2
    n = _paths_contributed_value(path_count, pct)
    return str(round(n)) if n < 2 ** 20 else f"approx. 2^{round(log2(n))}"


def _normalize(raw: dict[str, float], cap: float) -> dict[str, float]:
    """Scale raw per-candidate severities to 0..100 by the sample max (`cap`). `cap` is passed explicitly
    so a category whose candidates are scored in more than one place — nonlinearity, where toxic-entrypoint
    boundaries are added later — can share ONE scale. Empty / all-zero cap -> `{}` (no scores added)."""
    return {k: round(v / cap * 100.0, 1) for k, v in raw.items()} if cap > 0 else {}
# An ALREADY-SUMMARISED (CVL/Ghost) hotspot still contributing nonlinear ops means its summary is itself
# still nonlinear (an EXACT mulDiv / ray-math summary, say) — a coarsening target that the raw signal cannot
# see, because the moment a function has any summary it stops appearing as raw hostile code.
# Gated LOWER than NONLINEAR_MIN_PCT: the prover also attributes a summary's cost to its callers, so a ghost
# under-reports as a standalone proc, and recoarsening an existing summary is a safe, cheap win — so a hot
# ghost clears a lower bar than a raw candidate would.
ALREADY_SUMMARISED_MIN_PCT = 10

# Flat score for a surviving hostile primitive (signal 4). It does NOT scale with how many entry methods
# reach it: reach is breadth, not summarization priority, so a primitive reached by 2 methods ranks with one
# reached by 20. The reach count rides along as context only (`reaching_count`).
SURVIVING_SCORE = 50.0


@dataclass
class Boundary:
    """A caller (or, for a "down" target, a shared inlined primitive) offered as an alternative place to
    summarize the leaf. Only EXPRESSIBLE, INTERNAL boundaries are kept — has a return AND all param/return
    types are CVL-clean (no void, opaque dynamic bytes/string, mapping, or function type), and not a
    public/external entry point (the rule's subject, never a summarization site) — so the signature alone
    conveys feasibility. `mutating` (writes state, NOT view/pure — summarizing it as a value would erase
    side effects the properties may observe) is the one remaining caveat. `hops` = call-graph distance from
    the candidate. Direction is carried by which candidate field holds it — `caller_boundaries` (walk UP to a
    container to summarize) vs `callee_boundaries` (descend DOWN to a shared inlined primitive) — never both,
    so it is not repeated per entry. `shared` = for a callee, how many sibling candidates also inline it
    (fan-in). Best: non-mutating, nearest."""
    function: str
    hops: int
    signature: str
    mutating: bool
    shared: int = 1


@dataclass
class Candidate:
    """A function worth summarizing, with WHY (`signals`) and, for a curated match, HOW (`candidate_summary`)."""
    function: str                 # "Contract.fn" (the contract prefix is part of the identifier)
    signals: tuple[str, ...]      # why flagged: "nonlinear" (difficulty %), "hashing" (AST), "external"
                                  # (cross-contract modifier), "already-summarised" (a CVL/ghost summary
                                  # still contributing nonlinear ops -> coarsen it), "branching" (a
                                  # path-count/loop hotspot -> NONDET the loop, or its value/void container),
                                  # or a catalog hard-op class naming the exact primitive: "in-memory-sort" /
                                  # "bitwise-scan" / "symbolic-exp" / "nonlinear-mulDiv". No liveness label — every candidate reaches SMT.
    score: float                  # rank key (higher = summarize first)
    evidence: str                 # human-readable justification
    file: str = ""                # source file of the function ("" if unresolved)
    line: int | None = None       # 1-based start line of the function (None if unresolved)
    signature: str = ""           # readable `fn(paramTypes) -> returnTypes` (from the AST; "" if unresolved)
    mutating: bool | None = None  # True if state-changing (not view/pure); None if unresolved from the AST
    reaching_count: int = 0       # how many entry methods' postOptimize TAC keeps this primitive (breadth)
    summarizable: bool = True     # the prover's own `summarizable` flag (surviving graph)
    candidate_summary: str = ""   # suggested over-/under-approximation (curated overlay), "" for a generic match
    # Alternative places to summarize instead of the leaf. A candidate uses ONE direction, never both:
    # `caller_boundaries` = containers to walk UP to (hashing/primitive/branching); `callee_boundaries` =
    # shared inlined primitives to descend DOWN to (nonlinear). The field name carries the direction.
    caller_boundaries: list[Boundary] = field(default_factory=list)
    callee_boundaries: list[Boundary] = field(default_factory=list)


#: Per-field default of every OPTIONAL `Candidate` field (one with a default / default_factory), derived
#: from the dataclass itself — the single source of truth for `to_dict`'s default-pruning. Required fields
#: (function/signals/score/evidence) have no default and so are absent here, hence never pruned. Add or
#: rename a `Candidate` field and this tracks it automatically (see `test_candidate_schema_parity`).
_CANDIDATE_DEFAULTS = {
    f.name: (f.default if f.default is not MISSING else f.default_factory())  # type: ignore[misc]
    for f in fields(Candidate)
    if f.default is not MISSING or f.default_factory is not MISSING
}

#: Candidate fields kept for INTERNAL use (ranking) but NOT serialized. `score` orders the candidates — the
#: output is emitted in that order, so the sequence conveys the ranking — but the number itself is not
#: cross-comparable (nonlinear is population-relative, branching absolute) and so would mislead if shown.
_UNSERIALIZED_FIELDS = frozenset({"score"})


@dataclass
class DetectionReport:
    candidates: list[Candidate] = field(default_factory=list)
    # The CUT's own prover-hostile EXTERNAL methods (the rules' subjects): (qualified name, rule max polyn.
    # degree, raw nonlinear magnitude), worst-magnitude first. They are never summarized themselves, but
    # detect_url descends each to its shallowest sound inner boundary and adds THAT as a `toxic-entrypoint`
    # candidate, scored on the entrypoint's magnitude against `nl_max`. Not serialized (consumed in-process).
    toxic_entrypoints: list[tuple[str, int, float]] = field(default_factory=list)
    nl_max: float = 0.0   # the run's peak nonlinear magnitude (shared scale for toxic-entrypoint boundaries)

    def is_empty(self) -> bool:
        return not self.candidates

    def to_dict(self) -> dict:
        """Machine-readable form for a consuming pipeline: each candidate (the problematic function, why,
        and rank) with its caller-boundary shortlist, emitted in RANK ORDER (the sequence conveys priority;
        the numeric `score` is `_UNSERIALIZED_FIELDS`, not emitted). Fields left at their default (unresolved
        location, no reach, no summary, no boundaries, summarizable) are OMITTED — the consumer assumes the
        default, and the prompt this renders into stays lean. The emitted shape is the `schema.py` TypedDicts
        (`HostileCandidate` / `HostileBoundary`); `test_candidate_schema_parity` locks those to the
        `Candidate`/`Boundary` fields so they cannot silently drift from this."""
        candidates = []
        for c in self.candidates:
            d = {k: v for k, v in asdict(c).items()
                 if k not in _UNSERIALIZED_FIELDS and (k not in _CANDIDATE_DEFAULTS or _CANDIDATE_DEFAULTS[k] != v)}
            candidates.append(d)
        return {"candidates": candidates}

    def format(self) -> str:
        if self.is_empty():
            return "no summarization candidates detected"
        out = ["summarization candidates (what to summarize first, why, and where):"]
        for c in self.candidates:
            reach = f"  reaches {c.reaching_count}" if c.reaching_count else ""
            loc = f"  {c.file}:{c.line}" if c.file else ""
            out.append(f"  {c.score:5.1f}  {c.function}  <{','.join(c.signals)}>{reach}{loc}")
            out.append(f"                          {c.evidence}")
            if c.candidate_summary:
                out.append(f"                          candidate: {c.candidate_summary}")
            for b in c.caller_boundaries:                       # containers to walk UP to
                # only expressible, internal boundaries survive the filter, so mutating is the last caveat
                feas = "state-changing — prefer a pure boundary" if b.mutating else "summarizable here"
                out.append(f"                          ↑ +{b.hops} {b.signature}  [{feas}]")
            for b in c.callee_boundaries:                       # shared nonlinear primitives to descend DOWN to
                feas = "state-changing — prefer a pure boundary" if b.mutating else "summarizable here"
                tag = f"shared ×{b.shared}, {feas}" if b.shared > 1 else feas
                out.append(f"                          ↓ +{b.hops} {b.signature}  [{tag}]")
        return "\n".join(out)




# ---------------------------------------------------------------- procId string helpers
def _contract_of(procid: str) -> str:
    """Contract/library prefix of a difficulty procId ('SomeLib.fn' -> 'SomeLib'; bare -> '')."""
    return procid.split(".", 1)[0] if "." in procid else ""


def _is_cvl_ghost(function: str) -> bool:
    """A `CVL/Ghost Function '...'` hotspot is an ALREADY-APPLIED CVL summary / ghost, not a Solidity
    function to summarize — so it is never a candidate (it IS the summary). The prover labels these
    procIds distinctly; real Solidity hotspots are `Contract.fn` with a source location."""
    return function.lstrip().startswith(("CVL/Ghost", "CVL Function", "Ghost"))


_GHOST_NAME_RE = re.compile(r"""['"](?P<name>[^'"]+)['"]""")


def _ghost_summary_name(procid: str) -> str:
    """The readable summary name inside a ``CVL/Ghost Function '<name>'`` procId (e.g.
    ``mulDivUpSummary256(a,b,10^27)``), used as the key of an already-summarised candidate. Falls back to
    the stripped procId when there is no quoted name."""
    m = _GHOST_NAME_RE.search(procid)
    return m.group("name").strip() if m else procid.strip()


def _strip_procid(function: str) -> str:
    """Normalize a prover procId to `Contract.fn` for the detector: drop a leading `(internal)` /
    `(external)` marker the prover prefixes to inlined-function hotspots. Without this the marker leaks
    into the contract parse, so the CUT's OWN internal function (`(internal) Stonks.foo`) is misread as a
    different contract and wrongly classified as a resolved-external. (The raw marker stays intact in the
    shared DifficultyReport, which a downstream refine step may reuse.)"""
    return re.sub(r"^\((?:internal|external)\)\s*", "", function.strip())


