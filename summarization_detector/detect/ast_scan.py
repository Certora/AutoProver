"""Signal 2 — the static AST scan for dynamic-input hashing/encoding, plus per-function source-location
resolution (`_function_locations` / `_project_relative`)."""
import re
from collections.abc import Callable
from pathlib import Path

from certora_autosetup.solidity_ast import stream_raw_units

from .model import HashSignal, _min_span, _project_relative, _span


# ---------------------------------------------------------------- signal 2: AST hashing/encoding calls
# The TRIGGER is an actual hash builtin — a global `Identifier` callee. Yul (assembly) calls are
# `YulFunctionCall`, never Solidity `FunctionCall`, so storage-slot keccaks are excluded automatically.
_HASH_IDENT = {"keccak256", "sha256", "ripemd160", "ecrecover"}
# `abi.encode` / `abi.encodePacked` (MemberAccess on the `abi` object) are CONTEXT — what is being hashed
# (they decide the length class), never a trigger on their own. Deliberately EXCLUDES encodeWithSelector /
# encodeCall / encodeWithSignature: those build calldata for an external CALL, not a hash.
_ENCODERS = {"encode", "encodePacked"}




# ---------------------------------------------------------------- signal 2 core: the AST walk
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
                out[qual] = (_project_relative(absp, sources_root), _line_of(absp, fsp[0]))   # store relative
    return out


