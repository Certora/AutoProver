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
from collections.abc import Callable
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path

from certora_autosetup.solidity_ast import stream_raw_units

from .difficulty import DifficultyReport, Hotspot, fetch_difficulty

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


# Output caps — keep the report (and the prompt it renders into) bounded. The score is only comparable
# WITHIN a signal category (nonlinear = % of the rule's nonlinear ops; surviving = flat; hashing = fixed),
# so each category is capped on its own rather than by a single cross-category top-N (which would let the
# nonlinear %s crowd out the surviving/hashing targets).
MAX_PER_CATEGORY = {"primitive": 10, "nonlinear": 10, "hashing": 10, "already-summarised": 10, "branching": 10}
NONLINEAR_MIN_PCT = 15       # drop difficulty hotspots below this % of the rule's nonlinear ops (long tail)
BRANCHING_MIN_PCT = 15       # drop path-count hotspots below this % of the rule's branching (long tail)
# An ALREADY-SUMMARISED (CVL/Ghost) hotspot still contributing nonlinear ops means its summary is itself
# still nonlinear (an EXACT mulDiv / ray-math summary, say) — a per-group coarsening target that the raw
# signal cannot see, because the moment a function has any summary it stops appearing as raw hostile code.
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
    the candidate. `direction` = "up" (a CALLER to summarize at — the hashing-leaf case) or "down" (a shared
    nonlinear PRIMITIVE the candidate inlines — the real arg-based target). `shared` = for a "down" target,
    how many sibling candidates also inline it (fan-in). Best: non-mutating, nearest."""
    function: str
    hops: int
    signature: str
    mutating: bool
    direction: str = "up"
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
    candidate_summary: str = ""   # suggested summary (curated EXACT or generic over-approx)
    boundaries: list[Boundary] = field(default_factory=list)   # caller boundaries to summarize at instead


#: Per-field default of every OPTIONAL `Candidate` field (one with a default / default_factory), derived
#: from the dataclass itself — the single source of truth for `to_dict`'s default-pruning. Required fields
#: (function/signals/score/evidence) have no default and so are absent here, hence never pruned. Add or
#: rename a `Candidate` field and this tracks it automatically (see `test_candidate_schema_parity`).
_CANDIDATE_DEFAULTS = {
    f.name: (f.default if f.default is not MISSING else f.default_factory())  # type: ignore[misc]
    for f in fields(Candidate)
    if f.default is not MISSING or f.default_factory is not MISSING
}


@dataclass
class DetectionReport:
    candidates: list[Candidate] = field(default_factory=list)
    dropped: int = 0   # candidates cut by the per-category caps (0 = the whole ranked list is present)
    # The CUT's own prover-hostile EXTERNAL methods (the rules' subjects): (qualified name, % nonlinear ops),
    # highest-% first. They are never summarized themselves, but detect_url descends each to its shallowest
    # sound inner boundary and adds THAT as a `toxic-entrypoint` candidate. Not serialized (consumed in-process).
    toxic_entrypoints: list[tuple[str, float]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.candidates

    def to_dict(self) -> dict:
        """Machine-readable form for a consuming pipeline: each candidate (the problematic function, why,
        and rank) with its caller-boundary shortlist. Fields left at their default (unresolved location,
        no reach, no summary, no boundaries, summarizable) are OMITTED — the consumer assumes the default,
        and the prompt this renders into stays lean. The emitted shape is the `schema.py` TypedDicts
        (`HostileCandidate` / `HostileBoundary`); `test_candidate_schema_parity` locks those to the
        `Candidate`/`Boundary` fields so they cannot silently drift from this."""
        candidates = []
        for c in self.candidates:
            d = {k: v for k, v in asdict(c).items()
                 if k not in _CANDIDATE_DEFAULTS or _CANDIDATE_DEFAULTS[k] != v}
            candidates.append(d)
        return {"candidates": candidates, "dropped": self.dropped}

    def format(self) -> str:
        if self.is_empty():
            return "no summarization candidates detected"
        out = ["summarization candidates (what to summarize first, and how):"]
        for c in self.candidates:
            reach = f"  reaches {c.reaching_count}" if c.reaching_count else ""
            loc = f"  {c.file}:{c.line}" if c.file else ""
            out.append(f"  {c.score:5.1f}  {c.function}  <{','.join(c.signals)}>{reach}{loc}")
            out.append(f"                          {c.evidence}")
            if c.candidate_summary:
                out.append(f"                          candidate: {c.candidate_summary}")
            for b in c.boundaries:
                # only expressible, internal boundaries survive the filter, so mutating is the last caveat
                feas = "state-changing — prefer a pure boundary" if b.mutating else "summarizable here"
                if b.direction == "down":                       # a shared nonlinear primitive (descend)
                    arrow = "↓"
                    tag = f"shared ×{b.shared}, {feas}" if b.shared > 1 else feas
                else:                                           # a caller boundary (walk up)
                    arrow, tag = "↑", feas
                out.append(f"                          {arrow} +{b.hops} {b.signature}  [{tag}]")
        return "\n".join(out)


# ---------------------------------------------------------------- signal 2 core: the AST walk
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
    return _min_span(off, spans, lambda entry: entry[0])


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

    def _relativize(p: str) -> str:
        # solc records source-unit keys as a MIX of project-relative and absolute paths (depending on how
        # each was imported/remapped); normalize to project-relative so every entry's `file` is uniform.
        if sources_root is None:
            return p
        try:
            return str(Path(p).relative_to(Path(sources_root)))
        except ValueError:
            return p                                    # already relative / not under the root — leave as-is

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
                out[qual] = (_relativize(absp), _line_of(absp, fsp[0]))   # read line from absp, store relative
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

# The catalog categories double as SIGNALS: a catalog-matched candidate carries its category
# (`in-memory-sort`, `bitwise-scan`, …) directly in `signals` rather than a generic `primitive` + a
# separate `category` field. This set is how the per-category cap groups them all under "primitive".
_PRIMITIVE_CATEGORIES = frozenset(c.key for c in GENERIC_RULES)


@dataclass(frozen=True)
class CuratedEntry:
    match: "re.Pattern[str]"     # a specific "Contract.func(sig)" pattern for a known PUBLIC library
    category: str                # one of the GENERIC_RULES keys
    summary: str                 # the concrete summary text, INCLUDING any soundness caveat — a curated
                                 # entry may be exact, or a documented over-/under-approximation


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


def _decode_hotspot(h: Hotspot) -> tuple[bool, str, str, str]:
    """Decode a difficulty hotspot's procId into `(internal, fn, contract, bare)` — the shared prefix of
    the nonlinear and branching signal loops. `internal` = the prover's `(internal)` inlined-fn marker;
    `fn` = `Contract.fn` (marker stripped); `contract` = its host; `bare` = the name after the last `.`."""
    internal = h.function.strip().startswith("(internal)")
    fn = _strip_procid(h.function)
    return internal, fn, _contract_of(fn), fn.rpartition(".")[2]


def _parse_location(loc: str) -> tuple[str, int | None]:
    """Split a difficulty hotspot location (`file:line`, or just `file`) into `(file, line)`. `line` is
    `None` when it is absent or non-numeric. Source paths carry no `:`, so the last segment is the line."""
    file, _, line = loc.rpartition(":")
    if file and line.isdigit():
        return file, int(line)
    return loc, None


def _bucket(c: Candidate) -> str:
    """The cap bucket a candidate falls in (its score is only comparable within it). Any catalog hard-op
    signal -> "primitive"; else nonlinear -> "nonlinear"; else "hashing". `external` is a modifier, not a
    bucket of its own."""
    if any(s in _PRIMITIVE_CATEGORIES for s in c.signals):
        return "primitive"
    if "already-summarised" in c.signals:
        return "already-summarised"
    if "branching" in c.signals and "nonlinear" not in c.signals:
        return "branching"
    if "nonlinear" in c.signals:
        return "nonlinear"
    return "hashing"


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
    consumes the result. CVL/ghost hotspots are already-applied summaries: a cheap one is dropped (it IS
    the summary), but one still contributing nonlinear ops is surfaced as an `already-summarised` candidate
    to coarsen (its summary is itself still nonlinear) — handled in-loop, not pre-filtered."""
    hotspots = difficulty.hotspots
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

    # signal 1 (nonlinear) + 3 (external): the difficulty hotspots. The prover's procIds carry a visibility
    # marker; three kinds are handled distinctly:
    #  - unmarked `<CUT>.m`     -> the CUT's OWN external method = a rule subject, never summarized -> DROP
    #  - unmarked `<other>.m`   -> a cross-contract callee (e.g. HubInstanceHarness.previewRemoveByShares)
    #                              -> keep + flag as a resolved external (signal 3)
    #  - `(internal) <ctx>.m`   -> an inlined internal fn (the ray/bit math). The prover attributes it to the
    #                              CALLING contract, so the same fn recurs under several contexts and (as
    #                              `<CUT>.fls`) collides with a surviving primitive -> dedup by bare name,
    #                              keeping the highest-contribution instance (hotspots are pct-sorted).
    surviving_bare = {raw.split("(", 1)[0].rpartition(".")[2] for raw in (surviving or {})}
    seen_internal: set[str] = set()
    toxic_entrypoints: list[tuple[str, float]] = []
    for h in hotspots:
        if _is_cvl_ghost(h.function):                        # an already-applied summary still in the problem
            if h.pct < ALREADY_SUMMARISED_MIN_PCT:           # cheap ghost -> genuinely handled, drop
                continue
            name = _ghost_summary_name(h.function)
            _bump(name, "already-summarised", float(h.pct),
                  f"already-applied summary still {h.pct}% of nonlinear ops — its summary is itself "
                  f"nonlinear; coarsen it to an uninterpreted/NONDET summary where the property does not "
                  f"read its result (a sound over-approximation), especially per verification-group")
            continue
        if h.pct < NONLINEAR_MIN_PCT:                        # drop the long, low-contribution tail
            continue
        internal, fn, contract, bare = _decode_hotspot(h)
        if contract == cut and not internal:                # the CUT's own external method (rule subject)
            toxic_entrypoints.append((fn, float(h.pct)))     # not summarizable itself; descend to a boundary later
            continue
        if bare in surviving_bare:                          # already a correctly-named surviving candidate
            continue
        if internal:
            if bare in seen_internal:                       # caller-attribution dup -> keep the top one
                continue
            seen_internal.add(bare)
        _bump(fn, "nonlinear", float(h.pct),
              f"{h.pct}% of nonlinear ops" + (f" @{h.location}" if h.location else ""))
        if h.location:                                      # the difficulty report carries the location
            cand[fn].file, cand[fn].line = _parse_location(h.location)
        if contract and contract != cut and not internal:   # a genuine cross-contract external call
            _bump(fn, "external", 10.0, f"resolved external in {contract} (not the CUT)")

    # signal 5 (branching / path count): the SIBLING difficulty node. When a rule times out on loop/path
    # explosion rather than math, the nonlinearity node can be empty (the math is already summarized) while
    # this one names the loop-heavy functions. Same procId conventions as the nonlinear loop. The CUT's own
    # external method (the rule subject) is skipped — its internal loop callees carry the actionable signal.
    # The value/void-return soundness gate (a reference-returning branch fn can't be `=> NONDET`ed) is
    # applied in `detect()`, where the AST return types are available.
    seen_branch: set[str] = set()
    pc_ctx = f", in a rule with up to {difficulty.max_path_count} paths" if difficulty.max_path_count else ""
    for h in difficulty.branching:
        if h.pct < BRANCHING_MIN_PCT:                        # drop the long, low-contribution tail
            continue
        internal, fn, contract, bare = _decode_hotspot(h)
        if contract == cut and not internal:                # CUT's own external method = rule subject -> skip
            continue
        if bare in seen_branch:                             # caller-attribution dup -> keep the top one
            continue
        seen_branch.add(bare)
        _bump(fn, "branching", float(h.pct),
              f"{h.pct}% of branching (loop/path count){pc_ctx}" + (f" @{h.location}" if h.location else ""))
        if h.location and cand[fn].file == "":              # keep the nonlinear location if already set
            cand[fn].file, cand[fn].line = _parse_location(h.location)

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
        n = len(rec["reaching_methods"])
        _bump(fn, rec["category"], SURVIVING_SCORE,       # the hard-op class IS the signal
              f"{rec['category']}, reaches {n} method(s)")
        c = cand[fn]
        c.reaching_count = n
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

    ranked = sorted(cand.values(), key=lambda c: (-c.score, c.function))   # score desc, name for stable ties
    kept: list[Candidate] = []
    per_cat: dict[str, int] = {}
    for c in ranked:                                    # score-ordered, so per category we keep the top ones
        cat = _bucket(c)
        if per_cat.get(cat, 0) < MAX_PER_CATEGORY.get(cat, 0):
            per_cat[cat] = per_cat.get(cat, 0) + 1
            kept.append(c)
    return DetectionReport(
        candidates=kept, dropped=len(ranked) - len(kept),
        toxic_entrypoints=sorted(toxic_entrypoints, key=lambda t: -t[1])[:5],  # top 5 by nonlinear-ops %
    )


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
    facts: dict[str, FnFacts] = {}
    merged = _unit_declaring(ast, cut)                     # the CUT's compilation unit, built once
    if merged is not None:
        edges, roots, by_name, sizes = _ast_call_graph(merged, cut)
        if external_call_graph is not None:
            _add_external_edges(edges, by_name, external_call_graph)
        reachable = _bfs(edges, roots)                     # for cone (rank) + the ecg fallback gate
        cone = _cone_weights(edges, reachable, sizes)
        facts = _fn_facts(merged)                          # signature/expressible/mutating/nondet-ok, one walk
    if surviving_set:                                     # authoritative: exactly what reached SMT
        survivors_bare = {n.rpartition(".")[2] for n in surviving_set}
        hash_signals = [h for h in hash_signals if _survives(h.function, surviving_set, survivors_bare)]
    elif merged is not None and external_call_graph is not None:   # fallback: complete cross-contract edges
        hash_signals = [h for h in hash_signals if h.function in reachable]
    difficulty = fetch_difficulty(job_url, limit=None) if job_url else DifficultyReport()   # detector filters
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
              if "nonlinear" in c.signals and "hashing" not in c.signals
              and not any(s in _PRIMITIVE_CATEGORIES for s in c.signals)]
        prim_reach = {m: _descend_to_prims(edges, m, reachable or None) for m in nl}
        fanin: dict[str, int] = {}
        for prims in prim_reach.values():
            for p in prims:
                fanin[p] = fanin.get(p, 0) + 1
        for c in report.candidates:
            if "hashing" in c.signals or any(s in _PRIMITIVE_CATEGORIES for s in c.signals):  # walk UP to a caller
                c.boundaries = _caller_boundaries(edges, facts, c.function, bounds_reach)
            elif "nonlinear" in c.signals:                 # descend DOWN to the shared nonlinear primitive
                targets = sorted(prim_reach.get(c.function, {}).items(),
                                 key=lambda kv: (-fanin.get(kv[0], 0), kv[1], kv[0]))[:4]
                c.boundaries = []
                for p, d in targets:
                    f = facts.get(p) or FnFacts(p)
                    if not f.expressible:    # a shared primitive we can't express as a value is no target
                        continue
                    c.boundaries.append(Boundary(p, d, f.signature, f.mutating, direction="down",
                                                 shared=fanin.get(p, 1)))
            elif "branching" in c.signals:                 # path-count: NONDET the loop, gated on return + writes
                f = _by_qual_or_bare(facts, c.function)
                ok = f.nondet_ok if f else False           # value/void return => NONDET-able
                mutating = f.mutating if f else True       # unknown -> assume mutating (conservative)
                # Offer summarizable boundaries for EVERY branching candidate (value/void-return callers
                # that wrap the loop, ranked view/pure-first), the same way the nonlinear/hashing/toxic
                # signals do — a reference-returning OR state-mutating hotspot can often be replaced by a
                # cleanly-sound (view/pure, value/void) boundary. Only view/pure value/void is
                # unconditionally sound to `=> NONDET`.
                c.boundaries = _caller_boundaries(edges, facts, c.function, bounds_reach, nondet=True)
                if ok and not mutating:                    # value/void return AND view/pure -> unconditionally sound
                    c.candidate_summary = ("=> NONDET (view/pure, value/void return): sound over-approximation, "
                                           "deletes the loop/path subproblem")
                elif ok:                                   # value/void return but STATE-MUTATING: NONDET drops writes
                    c.candidate_summary = ("state-MUTATING: `=> NONDET` drops its writes, so it is sound ONLY if "
                                           "the property does not read the state it mutates; prefer a view/pure "
                                           "value/void boundary below if one is offered, else verify")
                else:                                      # reference return -> the prover rejects NONDET here
                    c.candidate_summary = ("returns a reference type (the prover rejects `=> NONDET` on it): "
                                           "NONDET a value/void caller/container that wraps its loop instead "
                                           "(see boundaries)")
        # Prover-toxic CUT entrypoints (the rules' own external subjects) can't be summarized themselves, but
        # their shallowest sound inner boundary can. Surface that boundary as a candidate — this is what a
        # human summarizes when the method under test times out (e.g. liquidationCall ->
        # _calculateLiquidationAmounts), which no leaf-primitive signal catches.
        present = {c.function for c in report.candidates}
        for ep, pct in report.toxic_entrypoints:
            boundary = _shallowest_view_boundary(edges, facts, ep)
            if boundary is None or boundary in present:
                continue
            present.add(boundary)
            report.candidates.append(Candidate(
                function=boundary, signals=("toxic-entrypoint",), score=float(pct),
                evidence=f"shallowest summarizable boundary of prover-toxic {ep} ({pct:.0f}% of nonlinear "
                         f"ops) — the method under test can't be summarized, but this internal view can, "
                         f"cutting its whole subtree",
            ))
        report.candidates.sort(key=lambda c: (-c.score, c.function))     # re-rank with the new candidates
    # attach each candidate's source location (file always; line when the source is readable)
    locations = _function_locations(ast, sources_root)
    for c in report.candidates:
        if c.function in locations:
            c.file, c.line = locations[c.function]
    # attach a signature + view/pure flag (the agent needs the param/return types to write the summary, and
    # `mutating` to know a value-summary would erase side effects) — the same AST map the boundaries use.
    # Exact qualified match, else bare name: the difficulty report attributes an inlined fn to its CALLING
    # contract (`HubInstanceHarness.calculatePremiumRay`), which resolves by the bare `calculatePremiumRay`.
    for c in report.candidates:
        f = _by_qual_or_bare(facts, c.function)
        if f:
            c.signature, c.mutating = f.signature, f.mutating
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
    """The name of the tightest (start, end, name) span containing `off` — the enclosing contract/function."""
    t = _min_span(off, spans, lambda entry: (entry[0], entry[1]))
    return t[2] if t else None


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
    contracts_by_fid = _contracts_by_fid(merged)
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
    consumers = _invert(edges)
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


def _is_reference_typename(tn: object, merged: dict) -> bool:
    """Whether a solc return `typeName` is a REFERENCE type — one the prover REFUSES `=> NONDET` on
    ("using a NONDET summary for reference types causes unsoundness"): dynamic/fixed arrays, dynamic
    `bytes`/`string`, structs, mappings, function types. Value types (elementary except bytes/string,
    enums, UDVTs, contracts) are NONDET-able. A function is NONDET-summarizable iff it is void OR every
    return type is a value type."""
    if not isinstance(tn, dict):
        return False
    nt = tn.get("nodeType")
    if nt == "ElementaryTypeName":
        return (tn.get("name") or "") in _NON_EXPRESSIBLE_ELEM     # bytes / string
    if nt == "ArrayTypeName":
        return True
    if nt == "UserDefinedTypeName":
        ref = tn.get("referencedDeclaration")
        decl = merged.get(str(ref)) if ref is not None else None
        return isinstance(decl, dict) and decl.get("nodeType") == "StructDefinition"
    if nt in ("Mapping", "FunctionTypeName"):
        return True
    return False


@dataclass(frozen=True)
class FnFacts:
    """The AST facts about a function the detector needs to place a summary. ``signature`` = readable
    ``fn(pt,..) -> rt,..``. ``expressible`` = has a return AND all param/return types are CVL-clean (can be
    a typed value summary; a void fn has no value to model). ``mutating`` = stateMutability not view/pure
    (a value summary erases its writes). ``external`` = public/external (an entry point / rule subject,
    never an internal boundary). ``nondet_ok`` = void OR every return type is a VALUE type (the prover
    rejects ``=> NONDET`` on a reference return). ``FnFacts(qual)`` — all-conservative defaults — stands in
    for an unknown function."""
    signature: str
    expressible: bool = False
    mutating: bool = True
    external: bool = False
    nondet_ok: bool = False


def _contracts_by_fid(merged: dict) -> dict[int, list]:
    """fileId -> [(start, end, contract_name)] for every ContractDefinition, so a function's enclosing
    contract resolves by source containment within its file."""
    out: dict[int, list] = {}
    for node in merged.values():
        if node.get("nodeType") == "ContractDefinition":
            sp = _span3(node)
            if sp:
                out.setdefault(sp[2], []).append((sp[0], sp[1], node.get("name") or ""))
    return out


def _iter_functions(merged: dict):
    """Yield ``(qual, node)`` for every FunctionDefinition in the merged unit — ``qual`` = "Contract.fn"
    (bare fn when host-less). The shared enumeration behind `_fn_facts` (the call-graph builder keeps its
    own loop because it also needs each node's id)."""
    contracts_by_fid = _contracts_by_fid(merged)
    for node in merged.values():
        if node.get("nodeType") != "FunctionDefinition":
            continue
        sp = _span3(node)
        if sp is None:
            continue
        cname = _enclosing(sp[0], contracts_by_fid.get(sp[2], [])) or ""
        fname = node.get("name") or ""
        yield (f"{cname}.{fname}" if cname else fname), node


def _fn_facts(merged: dict) -> dict[str, FnFacts]:
    """qual -> FnFacts for every function — ONE AST walk, replacing the former `_signatures` + `_nondet_ok`
    passes that each re-enumerated the same functions."""
    out: dict[str, FnFacts] = {}
    for qual, node in _iter_functions(merged):
        fname = qual.rpartition(".")[2]
        params = _param_infos(node, "parameters")
        rets = _param_infos(node, "returnParameters")
        sig = f"{fname}({', '.join(t for t, _ in params)})" + (
            f" -> {', '.join(t for t, _ in rets)}" if rets else "")
        out[qual] = FnFacts(
            signature=sig,
            expressible=bool(rets) and all(_expressible_typename(tn, merged) for _, tn in params + rets),
            mutating=(node.get("stateMutability") or "") not in ("view", "pure"),
            external=(node.get("visibility") or "") in ("external", "public"),
            nondet_ok=not any(_is_reference_typename(tn, merged) for _, tn in rets),
        )
    return out


def _by_qual_or_bare(mapping: dict, qual: str):
    """`mapping[qual]`, else the first entry whose bare name (after the last '.') matches — the difficulty
    report attributes an inlined fn to its CALLING contract (`HubInstanceHarness.calcRay`), which resolves
    by the bare `calcRay`. None if neither hits."""
    if qual in mapping:
        return mapping[qual]
    tail = qual.rpartition(".")[2]
    return next((v for k, v in mapping.items() if k.rpartition(".")[2] == tail), None)


def _invert(edges: dict[str, set[str]]) -> dict[str, set[str]]:
    """callee -> {callers}: the reverse adjacency of the call graph."""
    consumers: dict[str, set[str]] = {}
    for caller, callees in edges.items():
        for callee in callees:
            consumers.setdefault(callee, set()).add(caller)
    return consumers


def _bfs_depths(adj: dict[str, set[str]], start: str, max_depth: int) -> dict[str, int]:
    """{node -> min hop distance from `start`} over adjacency `adj`, excluding `start`, bounded by
    `max_depth`. The shared bounded-BFS behind the up/down call-graph walkers."""
    depths: dict[str, int] = {}
    frontier, depth = {start}, 0
    while frontier and depth < max_depth:
        depth += 1
        nxt: set[str] = set()
        for f in frontier:
            for c in adj.get(f, ()):
                if c not in depths and c != start:
                    depths[c] = depth
                    nxt.add(c)
        frontier = nxt
    return depths


def _caller_boundaries(edges: dict, facts: dict, leaf: str, reachable: set[str] | None,
                       max_hops: int = 4, limit: int = 4, nondet: bool = False) -> list[Boundary]:
    """Walk UP the call graph from `leaf` and return caller boundaries — places to summarize instead of
    the leaf — ranked view/pure-first then nearest. Restricted to `reachable` (host-less free functions
    matched by bare name, as in the gate) when given, so suggestions are real. When `nondet` (the branching
    case), keep internal callers that are `=> NONDET`-summarizable (value/void return) — the value/void
    CONTAINER that wraps the leaf's loop — instead of the default `expressible` filter."""
    bare = {n.rpartition(".")[2] for n in reachable} if reachable is not None else set()
    out: list[Boundary] = []
    for q, h in _bfs_depths(_invert(edges), leaf, max_hops).items():
        if reachable is not None and not _survives(q, reachable, bare):
            continue
        f: FnFacts = facts.get(q) or FnFacts(q)
        if f.external:                       # an entry point is the rule's subject, never a boundary
            continue
        if nondet:
            if not f.nondet_ok:              # branching: keep value/void-return (NONDET-able) containers
                continue
        elif not f.expressible:              # nonlinear/hashing: keep value-expressible boundaries
            continue
        out.append(Boundary(q, h, f.signature, f.mutating))
    # view/pure over state-changing, then nearest — the cleanest, safest boundary first
    out.sort(key=lambda b: (b.mutating, b.hops, b.function))
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


def _entrypoint_in_edges(edges: dict, entrypoint: str) -> str | None:
    """Locate a difficulty-reported entrypoint (`SpokeInstance.liquidationCall`) among the AST call-graph
    keys, which are keyed by the DECLARING contract (`Spoke.liquidationCall` when the CUT inherits it).
    Exact match first, else the unique bare-name match; None if absent or ambiguous."""
    if entrypoint in edges:
        return entrypoint
    bare = entrypoint.rpartition(".")[2]
    hits = [k for k in edges if k.rpartition(".")[2] == bare]
    return hits[0] if len(hits) == 1 else None


def _shallowest_view_boundary(edges: dict, facts: dict, entrypoint: str, max_depth: int = 6) -> str | None:
    """The SHALLOWEST sound summarization boundary inside a prover-toxic entrypoint's call subtree.

    Walks DOWN from `entrypoint` and returns the first internal function that is safe to replace with a
    value: `view`/`pure` (no state to erase), CVL-expressible return, internal (not itself a rule subject),
    not a bare nonlinear primitive (those are already curated), and an AGGREGATOR — its own subtree reaches
    a nonlinear primitive, so it dominates real cost rather than being a trivial getter. Shallowest = the
    highest such boundary = cuts the most subtree in one entry (e.g. liquidationCall ->
    _calculateLiquidationAmounts). Ties at a depth: the one aggregating the most primitives. None if the
    subtree has no such boundary."""
    start = _entrypoint_in_edges(edges, entrypoint)
    if start is None:
        return None
    seen = {start}
    frontier, depth = {start}, 0
    while frontier and depth < max_depth:
        depth += 1
        at_depth: list[tuple[str, int]] = []
        nxt: set[str] = set()
        for f in frontier:
            for callee in edges.get(f, ()):
                if callee in seen:
                    continue
                seen.add(callee)
                nxt.add(callee)
                f = facts.get(callee)
                if f is None:
                    continue
                if f.expressible and not f.mutating and not f.external and not _is_nonlinear_prim(callee):
                    # Ungated prim-descent: a toxic entrypoint is a difficulty hotspot (it reached SMT), so
                    # its whole subtree is live — the reachability BFS would wrongly exclude it when the
                    # entrypoint is inherited (declared on a parent, CUT is the child), which is the common case.
                    prims = _descend_to_prims(edges, callee, None)
                    if prims:                                # an aggregator of expensive math, not a getter
                        at_depth.append((callee, len(prims)))
        if at_depth:                                         # shallowest depth with a boundary wins
            at_depth.sort(key=lambda kv: (-kv[1], kv[0]))
            return at_depth[0][0]
        frontier = nxt
    return None


def _descend_to_prims(edges: dict, method: str, reachable: set[str] | None,
                      max_depth: int = 4) -> dict[str, int]:
    """BFS DOWN the call graph from `method`; return {nonlinear-primitive qual -> min call depth} for the
    primitives it (transitively) inlines. Restricted to `reachable` (free functions bare-name matched, as
    in the gate) so dead code is never suggested."""
    bare = {n.rpartition(".")[2] for n in reachable} if reachable is not None else set()
    return {q: d for q, d in _bfs_depths(edges, method, max_depth).items()
            if _is_nonlinear_prim(q) and (reachable is None or _survives(q, reachable, bare))}


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
