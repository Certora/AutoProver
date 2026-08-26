"""Summarization-target DETECTOR — from ONE prover run, rank the functions worth summarizing (and, for a
curated match, suggest how).

Run this on a SANITY run (a `satisfy true` per-method reachability check) to decide what to
summarize BEFORE the real rules exist; a downstream generator then produces the summaries.

A sanity run processes every method through the full build+optimize TAC pipeline, so the expensive paths
(nonlinear, bitwise, hashing) stay IN the program and appear in the difficulty/live statistics and the
surviving call graph. Only the SMT SOLVE is trivial — it may exit a method via one easy path without
satisfying the hard nonlinear constraints — so solve TIME is not a cost signal, but the TAC-derived
difficulty statistics ARE (they reflect the code in the problem, not the path the solver took).

FOUR signals, each catching a different cost class:

  1. NONLINEAR (SMT phase)  — the prover's own difficulty report (`difficulty.fetch_difficulty`): ranked
     functions whose INLINED body contributes nonlinear ops (mulDiv, pow, sqrt, div). Catches the
     timeouts that reach SMT.
  2. HASHING / ENCODING     — a Solidity-AST walk (`scan_ast`): calls to keccak256 / sha256 / ecrecover /
     abi.encode* in a function body. These choke the BUILD / points-to phase, so they produce NO
     nonlinear hotspot — invisible to signal 1 (the getConditionId case). We read the AST (never regex
     the .sol) so comments and `assembly {}` (storage-slot keccaks) are excluded for free — a Yul call is
     not a Solidity `FunctionCall` node.
  3. RESOLVED-EXPENSIVE EXTERNAL — a signal-1 hotspot whose owning contract is NOT the CUT: a call that
     ALREADY resolved/linked to a real contract/oracle but is expensive once inlined. We do NOT touch
     UNRESOLVED (`[?]`) calls — resolving those is call-resolution's job (a separate tool).
  4. SURVIVING HOSTILE PRIMITIVE — from the postOptimize SurvivingCallGraph dumps (`surviving_hostile`):
     a RAW (non-ghost) prover-hostile primitive — bitwise scan / in-memory sort / symbolic exp / mulDiv,
     recognized by a generic operation catalog + a curated public-library overlay — that survives
     optimization, carrying which entry methods reach it and a candidate summary. Catches bitwise/sort,
     which contribute no nonlinear op and so are invisible to signal 1.

AST acquisition (`ensure_ast`): the raw solc AST (`.asts.json`) is all we need — its `FunctionDefinition`
nodes carry name + `stateMutability` + `visibility`, so no separate methods manifest is needed. Pass an
existing `ast_path` (from a prior `--dump_asts` run), or
pass a `conf` and we run `certoraRun --compilation_steps_only --dump_asts` ourselves (standalone mode).

Pure cores (`scan_ast`, `detect_from`) take already-fetched inputs so they are unit-testable offline;
`detect` is the thin orchestrator; `cli.main` is the standalone command. It decides WHAT to summarize; a
downstream generator produces the summaries. It only REUSES AutoProver code — the local `difficulty`
module for the difficulty signal and `certora_autosetup.solidity_ast.stream_raw_units` for the AST.
"""
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from certora_autosetup.solidity_ast import stream_raw_units

from .difficulty import DifficultyReport, fetch_difficulty

# ---------------------------------------------------------------- signal 2: AST hashing/encoding calls
# The TRIGGER is an actual hash builtin — a global `Identifier` callee. Yul (assembly) calls are
# `YulFunctionCall`, never Solidity `FunctionCall`, so storage-slot keccaks are excluded automatically.
_HASH_IDENT = {"keccak256", "sha256", "ripemd160", "ecrecover"}
# `abi.encode` / `abi.encodePacked` (MemberAccess on the `abi` object) are CONTEXT — what is being hashed
# (they decide the length class), never a trigger on their own. Deliberately EXCLUDES encodeWithSelector /
# encodeCall / encodeWithSignature: those build calldata for an external CALL, not a hash.
_ENCODERS = {"encode", "encodePacked"}


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


@dataclass
class Boundary:
    """A caller of a detected leaf, offered as an alternative place to summarize. The leaf is where the
    COST is; a caller is often where the CLEAN semantic boundary is (a summary there subsumes the leaf).
    Feasibility is described by three orthogonal facts: `has_return` (a void function has no value to
    model — nothing to summarize at); `expressible` (has a return AND all param/return types are CVL-clean
    — no opaque dynamic bytes/string, mapping, or function type); `mutating` (writes state, NOT view/pure —
    summarizing it as a value would erase side effects the properties may observe). `hops` = call-graph
    distance from the candidate. `direction` = "up" (a CALLER to summarize at — the hashing-leaf case) or
    "down" (a shared nonlinear PRIMITIVE the candidate inlines — the real arg-based target). `shared` = for
    a "down" target, how many sibling candidates also inline it (fan-in). Best: expressible, non-mutating."""
    function: str
    hops: int
    signature: str
    expressible: bool
    has_return: bool
    mutating: bool
    direction: str = "up"
    shared: int = 1


@dataclass
class Candidate:
    """A function worth summarizing, with WHY (`signals`) and, for a curated match, HOW (`candidate_summary`)."""
    function: str                 # "Contract.fn" (the contract prefix is part of the identifier)
    signals: tuple[str, ...]      # subset of {"nonlinear", "hashing", "external", "surviving"}
    score: float                  # rank key (higher = summarize first)
    evidence: str                 # human-readable justification
    file: str = ""                # source file of the function ("" if unresolved)
    line: int | None = None       # 1-based start line of the function ("" -> None if unresolved)
    category: str = ""            # hostile-primitive class (bitwise-scan / in-memory-sort / symbolic-exp /
                                  # nonlinear-mulDiv) when the surviving-graph catalog matched, else ""
    reaching_methods: list[str] = field(default_factory=list)  # entry methods whose postOptimize TAC keeps it
    summarizable: bool = True     # the prover's own `summarizable` flag (surviving graph)
    candidate_summary: str = ""   # suggested summary (curated EXACT or generic over-approx)
    boundaries: list[Boundary] = field(default_factory=list)   # caller boundaries to summarize at instead


@dataclass
class DetectionReport:
    candidates: list[Candidate] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.candidates

    def to_dict(self) -> dict:
        """Machine-readable form for a consuming pipeline: each candidate (the problematic
        function, why, and rank) with its caller-boundary shortlist (call chain + summarizability)."""
        return asdict(self)

    def format(self) -> str:
        if self.is_empty():
            return "no summarization candidates detected"
        out = ["summarization candidates (what to summarize first, and how):"]
        for c in self.candidates:
            cat = f" {{{c.category}}}" if c.category else ""
            reach = f"  reaches {len(c.reaching_methods)}" if c.reaching_methods else ""
            loc = f"  {c.file}:{c.line}" if c.file else ""
            out.append(f"  {c.score:5.1f}  {c.function}{cat}  <{','.join(c.signals)}>{reach}{loc}")
            out.append(f"                          {c.evidence}")
            if c.candidate_summary:
                out.append(f"                          candidate: {c.candidate_summary}")
            for b in c.boundaries:
                if not b.has_return:
                    feas = "no return value"                    # void — nothing to model as a summary
                elif not b.expressible:                         # has a return, so the TYPE is the problem
                    feas = "opaque sig"
                elif b.mutating:
                    feas = "state-changing — prefer a pure boundary"
                else:
                    feas = "summarizable here"
                if b.direction == "down":                       # a shared nonlinear primitive (descend)
                    arrow = "↓"
                    tag = f"shared ×{b.shared}, {feas}" if b.shared > 1 else feas
                else:                                           # a caller boundary (walk up)
                    arrow, tag = "↑", feas
                out.append(f"                          {arrow} +{b.hops} {b.signature}  [{tag}]")
        return "\n".join(out)


# ---------------------------------------------------------------- signal 2 core: the AST walk
def _span(node: dict) -> tuple[int, int] | None:
    """The node's `src` = "offset:length:fileId" -> (start, end) byte offsets. All nodes within one
    source file share a fileId, so containment by (start, end) alone attributes a call to its function."""
    src = node.get("src")
    if not isinstance(src, str):
        return None
    try:
        off, length, _fid = src.split(":")
        return int(off), int(off) + int(length)
    except ValueError:
        return None


def _classify_call(call: dict) -> tuple[str, str] | None:
    """Classify a FunctionCall as a hash TRIGGER or an ENCODER (context), or None. Returns ("hash",
    "keccak256") for a hash builtin, ("encode", "abi.encodePacked") for an abi encoder (base guarded so a
    user type's own `.encode()` is not mistaken for `abi.encode`). Only a "hash" makes a function a
    candidate; "encode" merely informs the length class."""
    ex = call.get("expression") or {}
    nt = ex.get("nodeType")
    if nt == "Identifier" and ex.get("name") in _HASH_IDENT:
        return ("hash", ex["name"])
    if nt == "MemberAccess" and ex.get("memberName") in _ENCODERS:
        base = ex.get("expression") or {}
        if base.get("nodeType") == "Identifier" and base.get("name") == "abi":
            return ("encode", "abi." + ex["memberName"])
    return None


def _arg_types(call: dict) -> tuple[str, ...]:
    """The solc typeStrings of a call's arguments (e.g. ('address','bytes32','uint256'))."""
    out = []
    for a in call.get("arguments") or []:
        if isinstance(a, dict):
            out.append((a.get("typeDescriptions") or {}).get("typeString") or "")
    return tuple(out)


def _is_dynamic_type(ts: str) -> bool:
    """True if a solc typeString denotes UNBOUNDED-length data: `bytes`, `string`, or a dynamic array
    `T[]`. Fixed-width (`bytes32`, `uint256`, `address`, a fixed array `T[5]`, a struct) is False —
    the storage location suffix (` memory`/` calldata`/` storage`) is dropped first."""
    if not ts:
        return False
    base = ts.split(" ", 1)[0]                    # "uint256[] memory" -> "uint256[]"; "bytes memory" -> "bytes"
    if base.endswith("[]"):
        return True
    if base in ("bytes", "string"):
        return True
    return False


def _refs(node) -> set:
    """All positive `referencedDeclaration` ids in an expression subtree — the declarations it reads."""
    out: set = set()
    if isinstance(node, dict):
        rd = node.get("referencedDeclaration")
        if isinstance(rd, int) and rd > 0:
            out.add(rd)
        for v in node.values():
            out |= _refs(v)
    elif isinstance(node, list):
        for v in node:
            out |= _refs(v)
    return out


def _dynamic_input(sites: list, param_ids: set) -> bool:
    """Whether a function's hashing consumes unbounded-length USER data. A site counts only when it has a
    dynamic-typed operand AND the call references a function PARAMETER (`param_ids`) — so a hash over a
    constant/immutable/literal (e.g. `keccak256(bytes(name))` for a fixed EIP-712 name) is NOT dynamic
    despite the `bytes` type. `sites` are (pattern, arg_types, refs) per hash/encode call. An `abi.encode*`
    with a dynamic arg is the source; a bare `keccak256(x)` counts only when no encoder feeds it (else its
    `bytes memory` arg is just the encode result, whose class the encode site already decided)."""
    has_encode = any(p.startswith("abi.") for p, _, _ in sites)
    for pattern, args, refs in sites:
        if not (refs & param_ids):                        # not user-controlled -> not the expensive case
            continue
        if pattern.startswith("abi."):
            if any(_is_dynamic_type(t) for t in args):
                return True
        elif not has_encode and any(_is_dynamic_type(t) for t in args):
            return True
    return False


def _tightest(off: int, spans: list) -> tuple | None:
    """The smallest span in `spans` (each `((start,end), *payload)`) that contains offset `off` — the
    innermost enclosing function/contract."""
    best = None
    for entry in spans:
        (s, e) = entry[0]
        if s <= off < e and (best is None or (e - s) < (best[0][1] - best[0][0])):
            best = entry
    return best


def scan_ast(ast_path: str | Path) -> list[HashSignal]:
    """Signal 2: walk the raw solc AST dump (`.asts.json`) and return every function that performs
    semantic hashing/encoding. Streams the file (it is often hundreds of MB) and processes each source
    file ONCE — a file recurs under every compilation unit that imports it, with re-numbered node ids but
    identical content, so we dedup by absolute path. Nodes are pre-flattened (every descendant is a
    top-level entry), so a single pass over each file's nodes finds all FunctionCall/FunctionDefinition/
    ContractDefinition nodes; attribution is by src-offset containment."""
    out: dict[str, HashSignal] = {}
    sites: dict[str, list] = {}       # fnkey -> [(pattern, arg_types, refs)...] for the dynamic/fixed decision
    hash_pats: dict[str, set] = {}    # fnkey -> the set of HASH-builtin names it calls (for ecrecover-only)
    param_ids: dict[str, set] = {}    # fnkey -> its parameter declaration ids (for the user-input test)
    seen: set[str] = set()
    for _rel, pdata in stream_raw_units(Path(ast_path)):
        if not isinstance(pdata, dict):
            continue
        for absp, nodes in pdata.items():
            if absp in seen or not isinstance(nodes, dict):
                continue
            seen.add(absp)
            contracts: list = []          # ((start,end), name)
            funcs: list = []              # ((start,end), name, mutability, visibility, param_ids)
            hits: list = []               # (offset, kind, pattern, arg_types, refs)
            for node in nodes.values():
                if not isinstance(node, dict):
                    continue
                sp = _span(node)
                if sp is None:
                    continue
                nt = node.get("nodeType")
                if nt == "ContractDefinition":
                    contracts.append((sp, node.get("name") or ""))
                elif nt == "FunctionDefinition":
                    pids = {p.get("id") for p in ((node.get("parameters") or {}).get("parameters") or [])
                            if isinstance(p, dict) and isinstance(p.get("id"), int)}
                    funcs.append((sp, node.get("name") or "", node.get("stateMutability") or "",
                                  node.get("visibility") or "", pids))
                elif nt == "FunctionCall":
                    kp = _classify_call(node)
                    if kp:
                        hits.append((sp[0], kp[0], kp[1], _arg_types(node), _refs(node.get("arguments"))))
            # AST source keys are project-relative (e.g. "lib/solady/src/tokens/ERC20.sol"), so test for a
            # dependency-root path COMPONENT rather than a "/lib/" substring (which misses a leading "lib/").
            parts = set(Path(absp).parts)
            is_dep = bool(parts & {"lib", "node_modules", ".certora_internal"})
            per_fn: dict[str, tuple] = {}     # fnkey -> (contract, name, mut, vis)
            for off, kind, pat, argtypes, refs in hits:
                f = _tightest(off, funcs)
                if f is None:                         # a call at file scope — no owning fn
                    continue
                (_fsp, fname, mut, vis, pids) = f
                c = _tightest(f[0][0], contracts)
                cname = c[1] if c else ""
                key = f"{cname}.{fname}" if cname else fname
                per_fn[key] = (cname, fname, mut, vis)
                sites.setdefault(key, []).append((pat, argtypes, refs))
                param_ids[key] = pids
                if kind == "hash":
                    hash_pats.setdefault(key, set()).add(pat)
            for key, (cname, fname, mut, vis) in per_fn.items():
                if not hash_pats.get(key):            # encoder-only (calldata/serialization) — not hashing
                    continue
                pats = tuple(sorted({p for p, _, _ in sites[key]}))
                rec = out.get(key)
                if rec is None:
                    out[key] = HashSignal(function=key, contract=cname, name=fname, mutability=mut,
                                          visibility=vis, patterns=pats, file=absp, is_dependency=is_dep)
                else:
                    rec.patterns = tuple(sorted({*rec.patterns, *pats}))
    # ecrecover ONLY (signature recovery) is not usefully over-approximable — its result is a recovered
    # address with no sound property tighter than havoc. Drop those (a function that also hashes stays).
    out = {k: v for k, v in out.items() if not (hash_pats.get(k, set()) <= {"ecrecover"})}
    for key, rec in out.items():
        rec.dynamic_input = _dynamic_input(sites.get(key, []), param_ids.get(key, set()))
    return sorted(out.values(), key=lambda h: h.function)


def _function_locations(ast_path: str | Path,
                        sources_root: str | Path | None = None) -> dict[str, tuple[str, int | None]]:
    """`Contract.fn` -> (source_file, 1-based start line | None). The FILE is the AST node group's path.
    The LINE resolves the FunctionDefinition byte-offset against the source when it is readable under
    `sources_root` (project root the AST paths are relative to); None when no `sources_root` or the file
    can't be read. Streams the AST once, deduping repeated source files by path (as `scan_ast` does)."""
    import bisect
    out: dict[str, tuple[str, int | None]] = {}
    newlines: dict[str, list[int] | None] = {}          # file -> newline byte offsets (None = unreadable)

    def _line_of(file: str, offset: int) -> int | None:
        if sources_root is None:
            return None
        if file not in newlines:
            try:
                data = (Path(sources_root) / file).read_bytes()
                newlines[file] = [i for i, b in enumerate(data) if b == 0x0A]
            except Exception:
                newlines[file] = None
        nl = newlines[file]
        return None if nl is None else bisect.bisect_right(nl, offset) + 1

    seen: set[str] = set()
    for _rel, pdata in stream_raw_units(Path(ast_path)):
        if not isinstance(pdata, dict):
            continue
        for absp, nodes in pdata.items():
            if absp in seen or not isinstance(nodes, dict):
                continue
            seen.add(absp)
            contracts: list = []
            funcs: list = []
            for node in nodes.values():
                if not isinstance(node, dict):
                    continue
                sp = _span(node)
                if sp is None:
                    continue
                nt = node.get("nodeType")
                if nt == "ContractDefinition":
                    contracts.append((sp, node.get("name") or ""))
                elif nt == "FunctionDefinition" and node.get("name"):
                    funcs.append((sp, node["name"]))
            for fsp, fname in funcs:
                c = _tightest(fsp[0], contracts)
                cname = c[1] if c else ""
                qual = f"{cname}.{fname}" if cname else fname
                out[qual] = (absp, _line_of(absp, fsp[0]))
    return out


# ---------------------------------------------------------------- signal 4: surviving hostile primitives
# From a sanity run's postOptimize SurvivingCallGraph dumps (one per entry method): the functions still in
# the optimized TAC. A RAW (non-ghost) prover-hostile primitive that survives is a summarization candidate;
# an already-applied summary appears as a `CVL/Ghost Function` stand-in (excluded — it IS the summary).
# GENERIC_RULES recognize the hostile OPERATION by conventional primitive-name tokens (protocol-agnostic);
# a CURATED overlay maps specific PUBLIC libraries to their known EXACT summaries.
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
# match reports WHAT (category + why) only; it suggests NO summary — the agent writes the one that is sound
# for the property at hand.
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


@dataclass(frozen=True)
class CuratedEntry:
    match: "re.Pattern[str]"     # a specific "Contract.func(sig)" pattern for a known PUBLIC library
    category: str                # one of the GENERIC_RULES keys
    summary: str                 # the concrete summary text, INCLUDING any soundness caveat — a curated
                                 # entry may be exact, or a documented over-/under-approximation
    note: str = ""


# CURATED overlay — specific PUBLIC libraries → a known-good, concrete summary. Only public, widely-used
# third-party libraries belong here; a protocol's own private math is caught by the GENERIC rules above and
# carries no suggested summary.
CURATED_SUMMARIES: tuple[CuratedEntry, ...] = (
    CuratedEntry(
        match=re.compile(r"\b(WadRayMath|PercentageMath)\.(ray|wad|percent)", re.I),
        category="nonlinear-mulDiv",
        summary="=> WAD/RAY fixed-point mulDiv summary (EXACT floor/ceil of x·y/scale).",
    ),
    CuratedEntry(
        match=re.compile(r"\b(Math|MathUpgradeable)\.mulDiv\b"),
        category="nonlinear-mulDiv",
        summary="=> OZ_Math.mulDiv curated summary (EXACT).",
    ),
    CuratedEntry(
        match=re.compile(r"\bFixedPointMathLib\.|\bFullMath\.mulDiv\b|\bPRBMath"),
        category="nonlinear-mulDiv",
        summary="=> FixedPointMathLib / FullMath / PRB curated summary (EXACT).",
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
    """The entry-method name from a sanity rule name (`sanity-<method>-Satisfy_...` -> `<method>`)."""
    m = re.match(r"sanity-(?P<sig>.+?)-Satisfy", rule or "")
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


# ---------------------------------------------------------------- AST acquisition (optional-arg design)
_MISSING_IMPORT_RE = re.compile(r'\d+:\d+:"([^"]+)"')


def _touch_missing_imported_specs(*outputs: str) -> list:
    """Recreate empty placeholders for imports certoraRun reports as missing. The prover prints
    `... import declarations do not import existing .spec files:` then `<line>:<col>:"<path>"` entries.
    The real files were skipped on upload precisely because they are EMPTY, so an empty placeholder is
    faithful. Returns the paths created (empty list if the failure is something else)."""
    created: list = []
    for out in outputs:
        if "do not import existing" not in out:
            continue
        for m in _MISSING_IMPORT_RE.finditer(out):
            p = Path("".join(m.group(1).split()))     # certoraRun wraps long paths across lines in captured
            if not p.exists():                         # (non-tty) output — rejoin before treating as a path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("")
                created.append(p)
    return created


def ensure_ast(ast_path: str | Path | None = None, *, conf: str | Path | None = None,
               solc_dir: str | Path | None = None) -> Path:
    """Return a path to a solc AST dump. If `ast_path` is given (from a prior `--dump_asts` run, or a
    prior run), use it. Otherwise run `certoraRun <conf> --compilation_steps_only --dump_asts` (standalone
    mode) and return the freshest `.asts.json` it writes under `.certora_internal/`. `solc_dir` is
    prepended to PATH so the conf's `solcN.NN` resolves. Raises if neither input suffices."""
    if ast_path is not None:
        p = Path(ast_path)
        if not p.exists():
            raise FileNotFoundError(f"ast_path does not exist: {p}")
        return p
    if conf is None:
        raise ValueError("provide ast_path (existing AST) or conf (to generate one via certoraRun)")
    conf = Path(conf)
    work = conf.parent
    env_path = None
    if solc_dir is not None:
        import os
        env_path = {**os.environ, "PATH": f"{solc_dir}:{os.environ.get('PATH', '')}"}
    cmd = ["certoraRun", conf.name, "--compilation_steps_only", "--dump_asts"]
    proc = subprocess.run(cmd, cwd=work, env=env_path, capture_output=True, text=True)
    # TEMPORARY WORKAROUND: the source-files upload skips EMPTY files, so an empty importable source that
    # another file imports is dropped while its importer is kept — the fetched scene then fails to compile
    # on the missing import (fixed upstream for NEW runs; existing runs stay broken). The missing file is
    # empty by definition, so recreate the missing imported spec(s) as empty placeholders and retry.
    for _ in range(5):
        if proc.returncode == 0:
            break
        created = _touch_missing_imported_specs(proc.stdout or "", proc.stderr or "")
        if not created:
            break
        print(f"[detect] created {len(created)} empty placeholder spec(s) for skipped-empty imports; retrying",
              file=sys.stderr)
        proc = subprocess.run(cmd, cwd=work, env=env_path, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-25:])
        raise RuntimeError(
            f"certoraRun AST generation failed (exit {proc.returncode}) in {work} for {conf.name}:\n{tail}\n"
            f"(hint: if the error is a missing solc, pass solc_dir / --solc-dir so the conf's solcN.NN "
            f"resolves; if it is a missing source/package, the fetched scene may be incomplete.)")
    dumps = sorted((work / ".certora_internal").rglob("*.asts.json"), key=lambda p: p.stat().st_mtime)
    if not dumps:
        raise FileNotFoundError(f"certoraRun produced no .asts.json under {work}/.certora_internal")
    return dumps[-1]


# ---------------------------------------------------------------- fusion + classifier
def _contract_of(procid: str) -> str:
    """Contract/library prefix of a difficulty procId ('SomeLib.fn' -> 'SomeLib'; bare -> '')."""
    return procid.split(".", 1)[0] if "." in procid else ""


def _is_cvl_ghost(function: str) -> bool:
    """A `CVL/Ghost Function '...'` hotspot is an ALREADY-APPLIED CVL summary / ghost, not a Solidity
    function to summarize — so it is never a candidate (it IS the summary). The prover labels these
    procIds distinctly; real Solidity hotspots are `Contract.fn` with a source location."""
    return function.lstrip().startswith(("CVL/Ghost", "CVL Function", "Ghost"))


def _strip_procid(function: str) -> str:
    """Normalize a prover procId to `Contract.fn` for the detector: drop a leading `(internal)` /
    `(external)` marker the prover prefixes to inlined-function hotspots. Without this the marker leaks
    into the contract parse, so the CUT's OWN internal function (`(internal) Stonks.foo`) is misread as a
    different contract and wrongly classified as a resolved-external. (The raw marker stays intact in the
    shared DifficultyReport, which a downstream refine step may reuse.)"""
    return re.sub(r"^\((?:internal|external)\)\s*", "", function.strip())


def _parse_location(loc: str) -> tuple[str, int | None]:
    """Split a difficulty hotspot location (`file:line`, or just `file`) into `(file, line)`. `line` is
    `None` when it is absent or non-numeric. Source paths carry no `:`, so the last segment is the line."""
    file, _, line = loc.rpartition(":")
    if file and line.isdigit():
        return file, int(line)
    return loc, None


def detect_from(hash_signals: list[HashSignal], difficulty: DifficultyReport, *, cut: str,
                include_dependencies: bool = False,
                cone_weight: dict[str, int] | None = None,
                surviving: dict[str, dict] | None = None) -> DetectionReport:
    """Fuse the four signals into a ranked candidate list. `difficulty` supplies signals 1 (nonlinear)
    and 3 (its hotspots whose contract != `cut` are resolved-expensive externals); `hash_signals` supply
    signal 2; `surviving` (from `surviving_hostile`) supplies signal 4 — raw prover-hostile primitives
    (bitwise / sort / exp / mulDiv) that survive optimization, keyed by function, carrying a category, the
    entry methods that reach it, the prover's `summarizable` flag, and a candidate summary. `cut` is the
    verified contract (its own methods are NOT "external"). Dependency-tree functions (lib/) are dropped
    from the hashing signal unless `include_dependencies` (surviving primitives are kept regardless — they
    are in the real problem). `cone_weight` re-weights the build-phase hashing signal by how much code
    consumes the result. CVL/ghost hotspots (already-applied summaries) are excluded — they are the
    summary, not a summarization target."""
    hotspots = [h for h in difficulty.hotspots if not _is_cvl_ghost(h.function)]
    cand: dict[str, Candidate] = {}

    def _bump(key: str, sig: str, score: float, evidence: str):
        c = cand.get(key)
        if c is None:
            cand[key] = Candidate(function=key, signals=(sig,), score=score, evidence=evidence)
        else:
            if sig not in c.signals:
                c.signals = (*c.signals, sig)
            c.score += score
            c.evidence += " | " + evidence

    # signal 1 (nonlinear) + 3 (external): the difficulty hotspots
    for h in hotspots:
        fn = _strip_procid(h.function)
        contract = _contract_of(fn)
        _bump(fn, "nonlinear", float(h.pct),
              f"{h.pct}% of nonlinear ops" + (f" @{h.location}" if h.location else ""))
        # The difficulty report already carries the hotspot's source location; lift it into the
        # structured fields. The AST pass in `detect` still overrides for functions it can resolve,
        # but it keys on the defining contract, so a hotspot the prover attributes to a derived
        # contract (`SpokeInstance.fn` defined on a base in another file) would otherwise have none.
        if h.location:
            cand[fn].file, cand[fn].line = _parse_location(h.location)
        if contract and contract != cut:
            _bump(fn, "external", 10.0, f"resolved external in {contract} (not the CUT)")

    # signal 2 (hashing/encoding): the AST scan. Dynamic-input hashing (unbounded bytes/string/array) is
    # the costly kind; a fixed-size digest (typical EIP-712) is bounded — score it far lower so the noise
    # of signature digests sinks below the real candidates rather than being dropped outright.
    for h in hash_signals:
        if h.is_dependency and not include_dependencies:
            continue
        cls = "dynamic-input" if h.dynamic_input else "fixed-size"
        _bump(h.function, "hashing", 20.0 if h.dynamic_input else 4.0,
              f"{'/'.join(h.patterns)} [{cls}] ({h.mutability or 'n/a'} {h.visibility})")

    # signal 4 (surviving hostile primitives): raw prover-hostile primitives still in the sanity run's
    # postOptimize TAC — a direct candidate. Score by reach (how many entry methods keep it); attach the
    # catalog category, candidate summary, reaching methods, and the prover's summarizable flag. A surviving
    # name carries a signature (`C.f(sig)`) while the difficulty/hashing keys are sig-less (`C.f`) — strip it
    # so the same function unifies into one candidate.
    for raw_fn, rec in (surviving or {}).items():
        fn = raw_fn.split("(", 1)[0]
        _bump(fn, "surviving", 20.0 + 2.0 * len(rec["reaching_methods"]),
              f"{rec['category']} survives optimization, reaches {len(rec['reaching_methods'])} method(s)")
        c = cand[fn]
        c.category = rec["category"]
        c.reaching_methods = list(rec["reaching_methods"])
        c.summarizable = rec["summarizable"]
        c.candidate_summary = rec["candidate_summary"]

    # cone-of-influence re-weighting for the hashing signal: build-phase cost has no per-function measure,
    # so scale a hashing candidate by how much reachable code consumes its result (normalized within the
    # candidate set, factor in [1, 2], so it stays comparable to the measured nonlinear pct). A hash whose
    # id threads the protocol rises; a leaf digest (tiny cone) stays low.
    if cone_weight:
        hashing = [c for c in cand.values() if "hashing" in c.signals]
        mx = max((cone_weight.get(c.function, 0) for c in hashing), default=0) or 1
        for c in hashing:
            w = cone_weight.get(c.function, 0)
            c.score *= 1 + w / mx
            c.evidence += f" | cone={w}"

    ranked = sorted(cand.values(), key=lambda c: c.score, reverse=True)
    return DetectionReport(candidates=ranked)


def _survives(function: str, surviving_set: set[str], survivors_bare: set[str]) -> bool:
    """Whether `function` (a scan_ast candidate) reaches SMT per the prover's surviving set. A host-less
    free (file-level) function has no contract host in the AST (e.g. `computeBaseHash`), but the prover
    attributes it to an arbitrary host in the surviving set (e.g. `Ownable.computeBaseHash`) — so it's
    matched by bare name. A hosted candidate matches on its exact qualified name, so a genuinely-
    unreachable `CTHelpers.getConditionId` is NOT resurrected by a same-named method on another contract
    (`survivors_bare` is `{name after last '.'}` over the surviving set)."""
    return function in surviving_set or ("." not in function and function in survivors_bare)


def _surviving_reach(graphs: list[dict]) -> set[str]:
    """The reachability set (sig-stripped `Contract.fn`) over the postOptimize surviving graphs — the
    functions that actually reach SMT. This is the authoritative signal-2 gate."""
    return {name.split("(", 1)[0] for g in graphs for name, _ in _surviving_names(g)}


def detect(job_url: str | None = None, *, ast_path: str | Path | None = None,
           conf: str | Path | None = None, cut: str, solc_dir: str | Path | None = None,
           include_dependencies: bool = False,
           external_call_graph: str | Path | None = None,
           surviving_graphs: list[dict] | None = None,
           sources_root: str | Path | None = None) -> DetectionReport:
    """Orchestrate the detector. `job_url` (optional) supplies the difficulty report (signals 1+3); with
    none, only the static signal-2 (hashing) runs. The AST is resolved by `ensure_ast` (`ast_path` if
    given, else generated from `conf`). `cut` is the verified contract name.

    `surviving_graphs` are the prover's postOptimize SurvivingCallGraph dumps: they drive signal 4 (the
    surviving hostile primitives) AND give the authoritative signal-2 reachability gate (the functions that
    actually reach SMT). Absent them, signal-2 falls back to AST + `external_call_graph` reachability from
    the CUT (see `reachable_from_main`). `sources_root` (the project root the AST paths are relative to,
    defaulting to the conf's directory) lets each candidate's start line be resolved from the source."""
    ast = ensure_ast(ast_path, conf=conf, solc_dir=solc_dir)
    if sources_root is None and conf is not None:
        sources_root = Path(conf).parent
    hash_signals = scan_ast(ast)
    surviving = surviving_hostile(surviving_graphs or [])
    surviving_set = _surviving_reach(surviving_graphs) if surviving_graphs else None
    cone: dict[str, int] = {}
    reachable: set[str] = set()
    edges: dict[str, set[str]] = {}
    sigs: dict[str, tuple[str, bool, bool, bool]] = {}
    merged = _unit_declaring(ast, cut)                     # the CUT's compilation unit, built once
    if merged is not None:
        edges, roots, by_name, sizes = _ast_call_graph(merged, cut)
        if external_call_graph is not None:
            _add_external_edges(edges, by_name, external_call_graph)
        reachable = _bfs(edges, roots)                     # for cone (rank) + the ecg fallback gate
        cone = _cone_weights(edges, reachable, sizes)
        sigs = _signatures(merged)                         # for caller-boundary expressibility
    if surviving_set:                                     # authoritative: exactly what reached SMT
        survivors_bare = {n.rpartition(".")[2] for n in surviving_set}
        hash_signals = [h for h in hash_signals if _survives(h.function, surviving_set, survivors_bare)]
    elif merged is not None and external_call_graph is not None:   # fallback: complete cross-contract edges
        hash_signals = [h for h in hash_signals if h.function in reachable]
    difficulty = fetch_difficulty(job_url) if job_url else DifficultyReport()
    report = detect_from(hash_signals, difficulty, cut=cut, cone_weight=cone,
                         include_dependencies=include_dependencies, surviving=surviving)
    # A hashing or surviving candidate is the cost LEAF (the exact function to summarize) — offer caller
    # boundaries too: where else the agent can place the summary (a clean-signature caller subsumes the
    # leaf and is often more feasible to model — see Boundary).
    bounds_reach = surviving_set if surviving_set else (reachable or None)
    if edges:
        # nonlinear-only candidates: descend to the shared nonlinear PRIMITIVE they inline, and count how
        # many candidates share each (fan-in) — a primitive shared across many methods is the real target.
        # Filter by AST reachability, NOT the surviving set: library `using`-for primitives (e.g.
        # MathLib.mulDivDown) get no INTERNAL_FUNC_START annotation, so they are absent from the surviving
        # set even though they are genuinely inlined into a surviving method.
        nl = [c.function for c in report.candidates
              if "nonlinear" in c.signals and "hashing" not in c.signals and "surviving" not in c.signals]
        prim_reach = {m: _descend_to_prims(edges, m, reachable or None) for m in nl}
        fanin: dict[str, int] = {}
        for prims in prim_reach.values():
            for p in prims:
                fanin[p] = fanin.get(p, 0) + 1
        for c in report.candidates:
            if "hashing" in c.signals or "surviving" in c.signals:   # walk UP to a clean caller boundary
                c.boundaries = _caller_boundaries(edges, sigs, c.function, bounds_reach)
            elif "nonlinear" in c.signals:                 # descend DOWN to the shared nonlinear primitive
                targets = sorted(prim_reach.get(c.function, {}).items(),
                                 key=lambda kv: (-fanin.get(kv[0], 0), kv[1], kv[0]))[:4]
                c.boundaries = [Boundary(p, d, *sigs.get(p, (p, False, False, True)),
                                         direction="down", shared=fanin.get(p, 1))
                                for p, d in targets]
    # attach each candidate's source location (file always; line when the source is readable)
    locations = _function_locations(ast, sources_root)
    for c in report.candidates:
        if c.function in locations:
            c.file, c.line = locations[c.function]
    return report


# ---------------------------------------------------------------- reachability-from-main (signal-2 gate)
# Signal 2 sweeps EVERY function in the scene; most are noise (an EIP-712 digest in a module the CUT never
# reaches). We prune to functions reachable from the CUT's external/public entry points over the real call
# graph. Edges come from two authoritative sources, never guessed: DIRECT internal/library calls from the
# solc AST (`referencedDeclaration`), and CROSS-CONTRACT calls from the prover's `externalCallGraph.json`
# (linking + dispatch — a static AST can't resolve which impl an interface call hits). Reachability
# OVER-approximates (dispatch matched by selector across the scene), which is the safe direction for a
# prune: we keep a maybe-reachable candidate rather than drop a real one.
def _span3(node: dict) -> tuple[int, int, int] | None:
    """`src` = "offset:length:fileId" -> (start, end, fileId). fileId distinguishes the source files that
    share one compilation unit's node-id space."""
    src = node.get("src")
    if not isinstance(src, str):
        return None
    try:
        off, length, fid = src.split(":")
        return int(off), int(off) + int(length), int(fid)
    except ValueError:
        return None


def _enclosing(off: int, spans: list) -> str | None:
    """The tightest (start, end, name) span containing `off` — the enclosing contract/function name."""
    best = None
    for s, e, name in spans:
        if s <= off < e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e, name)
    return best[2] if best else None


def _unit_declaring(ast_path: str | Path, cut: str) -> dict | None:
    """Merge (by node id) all nodes of the compilation unit that DECLARES contract `cut`. Node ids are
    unique within a unit's single solc id space, so `referencedDeclaration` resolves inside the merge; the
    CUT's unit already contains every file it imports (its full reachable closure)."""
    for _rel, pdata in stream_raw_units(Path(ast_path)):
        if not isinstance(pdata, dict):
            continue
        merged: dict[str, dict] = {}
        declares = False
        for _abs, nodes in pdata.items():
            if not isinstance(nodes, dict):
                continue
            for nid, node in nodes.items():
                if isinstance(node, dict):
                    merged[nid] = node
                    if node.get("nodeType") == "ContractDefinition" and node.get("name") == cut:
                        declares = True
        if declares:
            return merged
    return None


def _ast_call_graph(merged: dict, cut: str):
    """From the CUT's merged compilation unit build: internal call edges (caller qual -> callee qual via
    FunctionCall `referencedDeclaration`), the CUT's external/public entry points (BFS roots), and a
    name->quals index (to resolve dispatch selectors to scene methods). `qual` = "Contract.fn"."""
    contracts_by_fid: dict[int, list] = {}
    for node in merged.values():
        if node.get("nodeType") == "ContractDefinition":
            sp = _span3(node)
            if sp:
                contracts_by_fid.setdefault(sp[2], []).append((sp[0], sp[1], node.get("name") or ""))

    fndefs_by_fid: dict[int, list] = {}
    qual_by_id: dict[str, str] = {}
    roots: set[str] = set()
    by_name: dict[str, set[str]] = {}
    sizes: dict[str, int] = {}                              # qual -> body size (source bytes), for cone weight
    for nid, node in merged.items():
        if node.get("nodeType") != "FunctionDefinition":
            continue
        sp = _span3(node)
        if sp is None:
            continue
        cname = _enclosing(sp[0], contracts_by_fid.get(sp[2], [])) or ""
        fname = node.get("name") or ""
        qual = f"{cname}.{fname}" if cname else fname
        fndefs_by_fid.setdefault(sp[2], []).append((sp[0], sp[1], qual))
        qual_by_id[nid] = qual
        sizes[qual] = max(sizes.get(qual, 0), sp[1] - sp[0])
        if fname:
            by_name.setdefault(fname, set()).add(qual)
        if cname == cut and node.get("visibility") in ("external", "public"):
            roots.add(qual)

    edges: dict[str, set[str]] = {}
    for node in merged.values():
        if node.get("nodeType") != "FunctionCall":
            continue
        ref = (node.get("expression") or {}).get("referencedDeclaration")
        callee = qual_by_id.get(str(ref)) if ref is not None else None
        if callee is None:
            continue
        sp = _span3(node)
        if sp is None:
            continue
        caller = _enclosing(sp[0], fndefs_by_fid.get(sp[2], []))
        if caller:
            edges.setdefault(caller, set()).add(callee)
    return edges, roots, by_name, sizes


def _add_external_edges(edges: dict, by_name: dict, external_call_graph: str | Path) -> None:
    """Fold the prover's resolved/dispatch cross-contract edges into `edges`. For a RESOLVED target we add
    `caller -> targetContract.selectorMethod`; for a dispatch/symbolic target (contract unknown) we add
    `caller -> X.selectorMethod` for every scene contract X declaring that method (selector-matched
    over-approx). Selectors give the callee method name; unresolved targets with no scene match are skipped
    (they leave the scene — the linker's concern, not ours)."""
    data = json.loads(Path(external_call_graph).read_text())
    known = {q for qs in by_name.values() for q in qs}
    for host, call_edges in data.items():
        for e in call_edges:
            caller = f"{host}.{e['caller'].split('(', 1)[0]}"
            sel_names = [s["signature"].split("(", 1)[0] for s in e.get("selectors", [])
                         if s.get("signature")]
            for t in e.get("targets", []):
                contract = t.get("contract")
                for sel in sel_names:
                    if contract:
                        callee = f"{contract}.{sel}"
                        if callee in known:
                            edges.setdefault(caller, set()).add(callee)
                    else:                                   # dispatch: any scene method of that name
                        for callee in by_name.get(sel, ()):
                            edges.setdefault(caller, set()).add(callee)


def _build_graph(ast_path: str | Path, cut: str, external_call_graph: str | Path | None):
    """(edges, roots, sizes) for the CUT's compilation unit — AST internal edges (+ prover external/dispatch
    edges when given). None if the CUT's unit isn't found. Shared by reachability and the cone weight."""
    merged = _unit_declaring(ast_path, cut)
    if merged is None:
        return None
    edges, roots, by_name, sizes = _ast_call_graph(merged, cut)
    if external_call_graph is not None:
        _add_external_edges(edges, by_name, external_call_graph)
    return edges, roots, sizes


def _bfs(edges: dict, roots) -> set[str]:
    seen = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for nxt in edges.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _cone_weights(edges: dict, reachable: set, sizes: dict) -> dict[str, int]:
    """Per reachable function, the total body size of its TRANSITIVE CONSUMERS — the code that (transitively)
    calls it, hence reasons about its output. Invert the edges (callee -> callers) and walk from f toward its
    callers, staying inside `reachable`."""
    consumers: dict[str, set[str]] = {}
    for caller, callees in edges.items():
        for callee in callees:
            consumers.setdefault(callee, set()).add(caller)
    out: dict[str, int] = {}
    for f in reachable:
        seen: set[str] = set()
        stack = [c for c in consumers.get(f, ()) if c in reachable]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            stack.extend(x for x in consumers.get(c, ()) if x in reachable and x not in seen)
        out[f] = sum(sizes.get(g, 0) for g in seen)
    return out


_NON_EXPRESSIBLE_ELEM = {"bytes", "string"}   # dynamic byte blobs; fixed `bytesN` / value types are fine


def _param_infos(node: dict, key: str) -> list[tuple[str, object]]:
    """(typeString for display, typeName node for the structural check) per `parameters`/`returnParameters`."""
    ps = ((node.get(key) or {}).get("parameters")) or []
    return [(((p.get("typeDescriptions") or {}).get("typeString") or ""), p.get("typeName")) for p in ps]


def _expressible_typename(tn: object, merged: dict, seen: frozenset = frozenset()) -> bool:
    """Whether a solc `typeName` is modelable as a typed CVL summary value — walked RECURSIVELY, so a blob
    nested inside an array or struct is caught (unlike a flat typeString check). Expressible: value types,
    UDVTs, enums, contracts; arrays (fixed or dynamic) of an expressible element; structs whose fields are
    all expressible. NOT expressible, wherever nested: dynamic `bytes`/`string` (abi-encoded blobs — the
    point of an opaque boundary), mappings, and function types."""
    if not isinstance(tn, dict):
        return False
    nt = tn.get("nodeType")
    if nt == "ElementaryTypeName":
        return (tn.get("name") or "") not in _NON_EXPRESSIBLE_ELEM
    if nt == "ArrayTypeName":
        return _expressible_typename(tn.get("baseType"), merged, seen)
    if nt == "UserDefinedTypeName":
        ref = tn.get("referencedDeclaration")
        decl = merged.get(str(ref)) if ref is not None else None
        if not isinstance(decl, dict):
            return True                                   # external/interface type not in unit -> value-like
        if decl.get("nodeType") == "StructDefinition":
            sid = str(ref)
            if sid in seen:                               # recursive struct: no new blob on this cycle
                return True
            return all(_expressible_typename((m or {}).get("typeName"), merged, seen | {sid})
                       for m in (decl.get("members") or []))
        return True                                       # enum / UDVT / contract -> value-like
    if nt in ("Mapping", "FunctionTypeName"):
        return False
    return False                                          # unknown node -> conservative


def _signatures(merged: dict) -> dict[str, tuple[str, bool, bool, bool]]:
    """qual -> (readable `fn(pt,..) -> rt,..` signature, is-CVL-expressible, has-a-return, is-mutating).
    Expressible = has a return AND every param/return type is CVL-expressible (see `_expressible_typename`).
    has_return is kept SEPARATELY so a void function (no value to model) is distinguished from one with an
    opaque type. Mutating = stateMutability not view/pure (summarizing it as a value erases side effects)."""
    contracts_by_fid: dict[int, list] = {}
    for node in merged.values():
        if node.get("nodeType") == "ContractDefinition":
            sp = _span3(node)
            if sp:
                contracts_by_fid.setdefault(sp[2], []).append((sp[0], sp[1], node.get("name") or ""))
    out: dict[str, tuple[str, bool, bool, bool]] = {}
    for node in merged.values():
        if node.get("nodeType") != "FunctionDefinition":
            continue
        sp = _span3(node)
        if sp is None:
            continue
        cname = _enclosing(sp[0], contracts_by_fid.get(sp[2], [])) or ""
        fname = node.get("name") or ""
        qual = f"{cname}.{fname}" if cname else fname
        params = _param_infos(node, "parameters")
        rets = _param_infos(node, "returnParameters")
        sig = f"{fname}({', '.join(t for t, _ in params)})" + (
            f" -> {', '.join(t for t, _ in rets)}" if rets else "")
        has_return = bool(rets)
        types_ok = all(_expressible_typename(tn, merged) for _, tn in params + rets)
        mutating = (node.get("stateMutability") or "") not in ("view", "pure")
        out[qual] = (sig, has_return and types_ok, has_return, mutating)
    return out


def _caller_boundaries(edges: dict, sigs: dict, leaf: str, reachable: set[str] | None,
                       max_hops: int = 4, limit: int = 4) -> list[Boundary]:
    """Walk UP the call graph from `leaf` (a detected hasher) and return caller boundaries — places to
    summarize instead of the leaf — ranked expressible-first then nearest. Restricted to `reachable`
    (host-less free functions matched by bare name, as in the gate) when given, so suggestions are real."""
    consumers: dict[str, set[str]] = {}
    for caller, callees in edges.items():
        for callee in callees:
            consumers.setdefault(callee, set()).add(caller)
    bare = {n.rpartition(".")[2] for n in reachable} if reachable is not None else set()
    hops: dict[str, int] = {}
    frontier, depth = {leaf}, 0
    while frontier and depth < max_hops:
        depth += 1
        nxt: set[str] = set()
        for f in frontier:
            for c in consumers.get(f, ()):
                if c not in hops and c != leaf:
                    hops[c] = depth
                    nxt.add(c)
        frontier = nxt
    out: list[Boundary] = []
    for q, h in hops.items():
        if reachable is not None and not _survives(q, reachable, bare):
            continue
        sig, expressible, has_return, mutating = sigs.get(q, (q, False, False, True))
        out.append(Boundary(q, h, sig, expressible, has_return, mutating))
    # expressible first, then view/pure over state-changing, then nearest — the cleanest, safest boundary
    out.sort(key=lambda b: (not b.expressible, b.mutating, b.hops, b.function))
    return out[:limit]


# Nonlinear PRIMITIVES: arg-based math-library fns (Morpho MathLib, Solmate FixedPointMathLib, OZ Math,
# ds-math, ...). A nonlinear hotspot METHOD usually just INLINES one of these; the difficulty report
# attributes the ops to the method, so descending to the shared primitive names the real, arg-based
# over-approx target. Name-based for now — extensible to an AST nonlinear-op body scan.
_NONLINEAR_PRIMS = {
    "mulDiv", "mulDivDown", "mulDivUp", "fullMulDiv", "fullMulDivUp", "mulDivRoundingUp",
    "mulWad", "mulWadDown", "mulWadUp", "divWad", "divWadDown", "divWadUp", "sMulWad", "sDivWad",
    "rpow", "wpow", "pow", "rmul", "rdiv", "wmul", "wdiv", "sqrt", "cbrt", "exp", "expWad", "ln", "lnWad",
}


def _is_nonlinear_prim(qual: str) -> bool:
    return qual.rpartition(".")[2] in _NONLINEAR_PRIMS


def _descend_to_prims(edges: dict, method: str, reachable: set[str] | None,
                      max_depth: int = 4) -> dict[str, int]:
    """BFS DOWN the call graph from `method`; return {nonlinear-primitive qual -> min call depth} for the
    primitives it (transitively) inlines. Restricted to `reachable` (free functions bare-name matched, as
    in the gate) so dead code is never suggested."""
    bare = {n.rpartition(".")[2] for n in reachable} if reachable is not None else set()
    prims: dict[str, int] = {}
    seen = {method}
    frontier, depth = {method}, 0
    while frontier and depth < max_depth:
        depth += 1
        nxt: set[str] = set()
        for f in frontier:
            for callee in edges.get(f, ()):
                if callee in seen:
                    continue
                seen.add(callee)
                nxt.add(callee)
                if (_is_nonlinear_prim(callee) and callee not in prims
                        and (reachable is None or _survives(callee, reachable, bare))):
                    prims[callee] = depth
        frontier = nxt
    return prims


def reachable_from_main(ast_path: str | Path, cut: str, external_call_graph: str | Path | None = None) -> set[str]:
    """The set of `Contract.fn` reachable from the CUT's external/public entry points, over the combined
    call graph (AST internal edges + prover external/dispatch edges). Empty if the CUT's compilation unit
    can't be found (caller treats empty as "don't prune")."""
    g = _build_graph(ast_path, cut, external_call_graph)
    return _bfs(g[0], g[1]) if g else set()


def cone_weights(ast_path: str | Path, cut: str, external_call_graph: str | Path | None = None) -> dict[str, int]:
    """Heuristic cone-of-influence weight per reachable function — the size of the code that consumes its
    output (`_cone_weights`). The prover exposes no COI, so we approximate it over the call graph; it
    over-approximates the true data-flow cone but orders candidates by how widely their result propagates
    (a hash whose id threads the protocol outranks a leaf digest)."""
    g = _build_graph(ast_path, cut, external_call_graph)
    if g is None:
        return {}
    edges, roots, sizes = g
    return _cone_weights(edges, _bfs(edges, roots), sizes)
