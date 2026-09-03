"""The AST call graph and everything built on it: reachability-from-main (the signal-2 gate),
cone-of-influence weights, per-function facts (`FnFacts`), CVL-expressibility checks, and the caller/callee
summarization boundaries."""
import json
from dataclasses import dataclass
from pathlib import Path

from certora_autosetup.solidity_ast import stream_raw_units

from .model import Boundary, _min_span, _span3


# ---------------------------------------------------------------- reachability-from-main (signal-2 gate)
# Signal 2 sweeps EVERY function in the scene; most are noise (an EIP-712 digest in a module the CUT never
# reaches). We prune to functions reachable from the CUT's external/public entry points over the real call
# graph. Edges come from two authoritative sources, never guessed: DIRECT internal/library calls from the
# solc AST (`referencedDeclaration`), and CROSS-CONTRACT calls from the prover's `externalCallGraph.json`
# (linking + dispatch — a static AST can't resolve which impl an interface call hits). Reachability
# OVER-approximates (dispatch matched by selector across the scene), which is the safe direction for a
# prune: we keep a maybe-reachable candidate rather than drop a real one.
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
    matched by bare name, as in the gate) when given, so suggestions are real. When `nondet` (every current
    boundary offer — branching, primitive, hashing), keep only callers that are `=> NONDET`-summarizable
    (value/void return) — the CONTAINER that wraps the leaf — since a boundary is a place to NONDET instead
    of the leaf. `nondet=False` is a looser `expressible`-only fallback."""
    bare = {n.rpartition(".")[2] for n in reachable} if reachable is not None else set()
    out: list[Boundary] = []
    for q, h in _bfs_depths(_invert(edges), leaf, max_hops).items():
        if reachable is not None and not _survives(q, reachable, bare):
            continue
        f: FnFacts = facts.get(q) or FnFacts(q)
        if f.external:                       # an entry point is the rule's subject, never a boundary
            continue
        if nondet:
            if not f.nondet_ok:              # keep only value/void-return (NONDET-able) containers
                continue
        elif not f.expressible:              # looser fallback: any value-expressible boundary
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


def _survives(function: str, surviving_set: set[str], survivors_bare: set[str]) -> bool:
    """Whether `function` (a scan_ast candidate) reaches SMT per the prover's surviving set. A host-less
    free (file-level) function has no contract host in the AST (e.g. `computeBaseHash`), but the prover
    attributes it to an arbitrary host in the surviving set (e.g. `Ownable.computeBaseHash`) — so it's
    matched by bare name. A hosted candidate matches on its exact qualified name, so a genuinely-
    unreachable `CTHelpers.getConditionId` is NOT resurrected by a same-named method on another contract
    (`survivors_bare` is `{name after last '.'}` over the surviving set)."""
    return function in surviving_set or ("." not in function and function in survivors_bare)
