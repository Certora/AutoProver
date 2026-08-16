"""Summarization-target DETECTOR — from ONE prover run, rank the functions worth summarizing and say
HOW (per-function over-approx via overapprox.py, or whole-contract symbolic model via driver.py).

Autosetup calls this after a slow/timeout run to decide what to summarize; the overapprox/model tooling
then does it. THREE signals, each catching a different cost class:

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
     UNRESOLVED (`[?]`) calls — resolving those is call-resolution's job (a separate autosetup tool).

The WHOLE-vs-PART classifier (`_classify_mode`): a PURE/VIEW output-only function -> per-function
over-approx (a Phi over its result). A stateful external contract the rules lean on (several of its
methods are hotspots) -> whole-contract symbolic model.

AST acquisition (`ensure_ast`): the raw solc AST (`.asts.json`) is all we need — its `FunctionDefinition`
nodes carry name + `stateMutability` + `visibility`, so we never touch autosetup's `all_methods.json`
(which only autosetup can produce). Pass an existing `ast_path` (autosetup already ran `--dump_asts`), or
pass a `conf` and we run `certoraRun --compilation_steps_only --dump_asts` ourselves (standalone mode).

Pure cores (`scan_ast`, `detect_from`) take already-fetched inputs so they are unit-testable offline;
`detect` is the thin orchestrator; `cli.main` is the standalone command. A separate tool from smtool (it
decides WHAT to summarize; smtool generates the summaries), it only REUSES AutoProver code —
`smtool.difficulty` for the difficulty signal and `certora_autosetup.utils.file_utils` for the AST.
"""
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from certora_autosetup.utils.file_utils import stream_ast_files

from smtool.difficulty import DifficultyReport, fetch_difficulty

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
class Candidate:
    """A function worth summarizing, with WHY (`signals`) and HOW (`mode`)."""
    function: str                 # "Contract.fn"
    contract: str
    signals: tuple[str, ...]      # subset of {"nonlinear", "hashing", "external"}
    mode: str                     # "over_approx" | "symbolic_model"
    score: float                  # rank key (higher = summarize first)
    evidence: str                 # human-readable justification


@dataclass
class DetectionReport:
    candidates: list[Candidate] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.candidates

    def format(self) -> str:
        if self.is_empty():
            return "no summarization candidates detected"
        out = ["summarization candidates (what to summarize first, and how):"]
        for c in self.candidates:
            out.append(f"  [{c.mode:14s}] {c.score:5.1f}  {c.function}  <{','.join(c.signals)}>")
            out.append(f"                          {c.evidence}")
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


def _dynamic_input(sites: list) -> bool:
    """Whether a function's hashing consumes unbounded-length data. `sites` are (pattern, arg_types) for
    every hash/encode call in the function. An `abi.encode*` with any dynamic arg is the source of a
    dynamic hash; a bare `keccak256(x)` counts only when no `abi.encode*` feeds it (otherwise its
    `bytes memory` arg is just the encode result, whose length class the encode site already decided)."""
    has_encode = any(p.startswith("abi.") for p, _ in sites)
    for pattern, args in sites:
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
    sites: dict[str, list] = {}       # fnkey -> [(pattern, arg_types)...] for the dynamic/fixed decision
    seen: set[str] = set()
    for _rel, pdata in stream_ast_files(Path(ast_path)):
        if not isinstance(pdata, dict):
            continue
        for absp, nodes in pdata.items():
            if absp in seen or not isinstance(nodes, dict):
                continue
            seen.add(absp)
            contracts: list = []          # ((start,end), name)
            funcs: list = []              # ((start,end), name, mutability, visibility)
            hits: list = []               # (offset, kind, pattern, arg_types)
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
                    funcs.append((sp, node.get("name") or "", node.get("stateMutability") or "",
                                  node.get("visibility") or ""))
                elif nt == "FunctionCall":
                    kp = _classify_call(node)
                    if kp:
                        hits.append((sp[0], kp[0], kp[1], _arg_types(node)))
            # AST source keys are project-relative (e.g. "lib/solady/src/tokens/ERC20.sol"), so test for a
            # dependency-root path COMPONENT rather than a "/lib/" substring (which misses a leading "lib/").
            parts = set(Path(absp).parts)
            is_dep = bool(parts & {"lib", "node_modules", ".certora_internal"})
            has_hash: dict[str, bool] = {}    # fnkey -> does it actually call a hash builtin (the trigger)
            per_fn: dict[str, tuple] = {}     # fnkey -> (contract, name, mut, vis)
            for off, kind, pat, argtypes in hits:
                f = _tightest(off, funcs)
                if f is None:                         # a call at file scope — no owning fn
                    continue
                (_fsp, fname, mut, vis) = f
                c = _tightest(f[0][0], contracts)
                cname = c[1] if c else ""
                key = f"{cname}.{fname}" if cname else fname
                per_fn[key] = (cname, fname, mut, vis)
                sites.setdefault(key, []).append((pat, argtypes))
                has_hash[key] = has_hash.get(key, False) or (kind == "hash")
            for key, (cname, fname, mut, vis) in per_fn.items():
                if not has_hash.get(key):             # encoder-only (calldata/serialization) — not hashing
                    continue
                pats = tuple(sorted({p for p, _ in sites[key]}))
                rec = out.get(key)
                if rec is None:
                    out[key] = HashSignal(function=key, contract=cname, name=fname, mutability=mut,
                                          visibility=vis, patterns=pats, file=absp, is_dependency=is_dep)
                else:
                    rec.patterns = tuple(sorted({*rec.patterns, *pats}))
    for key, rec in out.items():
        rec.dynamic_input = _dynamic_input(sites.get(key, []))
    return sorted(out.values(), key=lambda h: h.function)


# ---------------------------------------------------------------- AST acquisition (optional-arg design)
def ensure_ast(ast_path: str | Path | None = None, *, conf: str | Path | None = None,
               solc_dir: str | Path | None = None) -> Path:
    """Return a path to a solc AST dump. If `ast_path` is given (autosetup already ran `--dump_asts`, or a
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
    subprocess.run(["certoraRun", conf.name, "--compilation_steps_only", "--dump_asts"],
                   cwd=work, env=env_path, check=True, capture_output=True, text=True)
    dumps = sorted((work / ".certora_internal").rglob("*.asts.json"), key=lambda p: p.stat().st_mtime)
    if not dumps:
        raise FileNotFoundError(f"certoraRun produced no .asts.json under {work}/.certora_internal")
    return dumps[-1]


# ---------------------------------------------------------------- fusion + classifier
def _contract_of(procid: str) -> str:
    """Contract/library prefix of a difficulty procId ('SomeLib.fn' -> 'SomeLib'; bare -> '')."""
    return procid.split(".", 1)[0] if "." in procid else ""


def _classify_mode(contract: str, signals: tuple[str, ...], external_multi: bool) -> str:
    """WHOLE (symbolic_model) vs PART (over_approx). Whole-contract when the cost is a stateful external
    dependency the rules lean on — heuristic: several methods of the same external contract are hotspots
    (`external_multi`). Otherwise a single output-only function -> per-function over-approx."""
    if "external" in signals and external_multi:
        return "symbolic_model"
    return "over_approx"


def detect_from(hash_signals: list[HashSignal], difficulty: DifficultyReport, *, cut: str,
                include_dependencies: bool = False) -> DetectionReport:
    """Fuse the three signals into a ranked candidate list. `difficulty` supplies signals 1 (nonlinear)
    and 3 (its hotspots whose contract != `cut` are resolved-expensive externals); `hash_signals` supply
    signal 2. `cut` is the verified contract (its own methods are NOT "external"). Dependency-tree
    functions (lib/) are dropped unless `include_dependencies`."""
    ext_counts: dict[str, int] = {}
    for h in difficulty.hotspots:
        c = _contract_of(h.function)
        if c and c != cut:
            ext_counts[c] = ext_counts.get(c, 0) + 1

    cand: dict[str, Candidate] = {}

    def _bump(key: str, contract: str, sig: str, score: float, evidence: str):
        c = cand.get(key)
        if c is None:
            cand[key] = Candidate(function=key, contract=contract, signals=(sig,), mode="over_approx",
                                  score=score, evidence=evidence)
        else:
            if sig not in c.signals:
                c.signals = (*c.signals, sig)
            c.score += score
            c.evidence += " | " + evidence

    # signal 1 (nonlinear) + 3 (external): the difficulty hotspots
    for h in difficulty.hotspots:
        contract = _contract_of(h.function)
        _bump(h.function, contract, "nonlinear", float(h.pct),
              f"{h.pct}% of nonlinear ops" + (f" @{h.location}" if h.location else ""))
        if contract and contract != cut:
            _bump(h.function, contract, "external", 10.0,
                  f"resolved external in {contract} (not the CUT)")

    # signal 2 (hashing/encoding): the AST scan. Dynamic-input hashing (unbounded bytes/string/array) is
    # the costly kind; a fixed-size digest (typical EIP-712) is bounded — score it far lower so the noise
    # of signature digests sinks below the real candidates rather than being dropped outright.
    for h in hash_signals:
        if h.is_dependency and not include_dependencies:
            continue
        cls = "dynamic-input" if h.dynamic_input else "fixed-size"
        _bump(h.function, h.contract, "hashing", 20.0 if h.dynamic_input else 4.0,
              f"{'/'.join(h.patterns)} [{cls}] ({h.mutability or 'n/a'} {h.visibility})")

    # classify mode per candidate
    for c in cand.values():
        external_multi = c.contract in ext_counts and ext_counts[c.contract] >= 2
        c.mode = _classify_mode(c.contract, c.signals, external_multi)

    ranked = sorted(cand.values(), key=lambda c: c.score, reverse=True)
    return DetectionReport(candidates=ranked)


def detect(job_url: str | None = None, *, ast_path: str | Path | None = None,
           conf: str | Path | None = None, cut: str, solc_dir: str | Path | None = None,
           include_dependencies: bool = False,
           external_call_graph: str | Path | None = None) -> DetectionReport:
    """Orchestrate the detector. `job_url` (optional) supplies the difficulty report (signals 1+3); with
    none, only the static signal-2 (hashing) runs. The AST is resolved by `ensure_ast` (`ast_path` if
    given, else generated from `conf`). `cut` is the verified contract name. When `external_call_graph`
    (the prover's `externalCallGraph.json`) is given, signal-2 candidates are pruned to those REACHABLE
    from the CUT (see `reachable_from_main`)."""
    ast = ensure_ast(ast_path, conf=conf, solc_dir=solc_dir)
    hash_signals = scan_ast(ast)
    if external_call_graph is not None:
        reachable = reachable_from_main(ast, cut, external_call_graph)
        hash_signals = [h for h in hash_signals if h.function in reachable]
    difficulty = fetch_difficulty(job_url) if job_url else DifficultyReport()
    return detect_from(hash_signals, difficulty, cut=cut, include_dependencies=include_dependencies)


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
    for _rel, pdata in stream_ast_files(Path(ast_path)):
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
    return edges, roots, by_name


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


def reachable_from_main(ast_path: str | Path, cut: str, external_call_graph: str | Path | None = None) -> set[str]:
    """The set of `Contract.fn` reachable from the CUT's external/public entry points, over the combined
    call graph (AST internal edges + prover external/dispatch edges). Returns all functions if the CUT's
    compilation unit can't be found (fail-open: never prune on incomplete data)."""
    merged = _unit_declaring(ast_path, cut)
    if merged is None:
        return set()                                        # caller treats empty as "don't prune" if desired
    edges, roots, by_name = _ast_call_graph(merged, cut)
    if external_call_graph is not None:
        _add_external_edges(edges, by_name, external_call_graph)
    seen = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for nxt in edges.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen
