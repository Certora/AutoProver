"""AST-based source extraction for the optional prefetch (smtool.agent.refine._reference_source).

Reads the solc `.asts.json` — a byproduct of the `--compilation_steps_only --dump_asts` build (the same
compile that yields `.certora_build.json`; smtool.scene passes the flag) — and returns, for each model
method, its Solidity body plus the TRANSITIVE bodies of the in-tree functions it calls. Precise where the
text slice is heuristic: bodies come from each `FunctionDefinition` node's `src` byte-range, and callees
from `FunctionCall.expression.referencedDeclaration` (an exact node id), so overloads / inheritance /
library `using-for` resolve correctly and the closure is complete, not one-hop-by-name-guess.

Schema (verified): `.asts.json` = dict[compilationUnit][file][nodeId] = node; a FunctionDefinition has
`nodeType`, `name`, `id`, `src="start:len:fileIdx"` (BYTE offsets), `kind`, `visibility`, `implemented`;
a FunctionCall's callee is `node["expression"]["referencedDeclaration"]` (int id; negative == builtin).

Memory: the file is many MB (GBs on big projects), so we NEVER json.load it — we stream one compilation
unit at a time (autosetup's `stream_ast_files`, ijson) and keep only a SLIM index (per function: name,
file, src, arity, visibility, implemented, callee-ids). Bodies are sliced from source on demand, not held.
Absent the file (or any hiccup), the caller falls back to the text extractor — nothing here is load-bearing
for correctness (the conformance proof stays the trust anchor); it only spends fewer agent turns.
"""
from pathlib import Path

from certora_autosetup.utils.file_utils import stream_ast_files



def find_asts(sources_root: str) -> Path | None:
    """The newest `.asts.json` under the scene's build dirs, or None (then the caller text-falls-back)."""
    cands = sorted(Path(sources_root, ".certora_internal").glob("*/.asts.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _arity(node: dict) -> int:
    return len((node.get("parameters") or {}).get("parameters") or [])


def _callee_ids(node: dict) -> list[int]:
    """Every `referencedDeclaration` id reached by a FunctionCall in `node`'s subtree (user fns only —
    negative ids are Solidity builtins like require/assert). Iterative to avoid deep recursion."""
    out: list[int] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get("nodeType") == "FunctionCall":
                rd = (n.get("expression") or {}).get("referencedDeclaration")
                if isinstance(rd, int) and rd >= 0:
                    out.append(rd)
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)
    return out


def _build_index(asts_path: Path) -> tuple[dict, dict]:
    """ONE streaming pass -> (by_id, by_name). by_id[id] = slim record; by_name[name] = [ids]. Peak
    memory is a single compilation unit (via stream_ast_files), not the whole file."""
    by_id: dict[int, dict] = {}
    by_name: dict[str, list] = {}
    for _rel, absmap in stream_ast_files(asts_path):
        if not isinstance(absmap, dict):
            continue
        for file, nodes in absmap.items():
            if not isinstance(nodes, dict):
                continue
            for node in nodes.values():
                if not (isinstance(node, dict) and node.get("nodeType") == "FunctionDefinition"
                        and isinstance(node.get("id"), int) and node.get("name")):
                    continue
                nid = node["id"]
                if nid in by_id:                      # same node can recur across compilation units
                    continue
                by_id[nid] = {
                    "name": node["name"], "file": file, "src": node.get("src"),
                    "arity": _arity(node), "vis": node.get("visibility"),
                    "impl": bool(node.get("implemented")), "callees": _callee_ids(node),
                }
                by_name.setdefault(node["name"], []).append(nid)
    return by_id, by_name


def _slice(sources_root: str, file: str, src: str | None) -> str | None:
    """The source text of a node's `src` byte-range, read from `file` (absolute, or under sources_root)."""
    try:
        start, length, _ = (int(x) for x in (src or "").split(":"))
    except ValueError:
        return None
    p = Path(file) if Path(file).is_absolute() else Path(sources_root, file)
    try:
        return p.read_bytes()[start:start + length].decode("utf-8", "replace")
    except OSError:
        return None


_INDEX_CACHE: dict[str, tuple] = {}    # asts_path -> (mtime, by_id, by_name); one streaming pass, reused


def _cached_index(asts_path: Path) -> tuple[dict, dict]:
    """`_build_index` is a full stream of the (many-MB) file — cache it per asts file+mtime so the
    on-demand get_function tool is cheap after the first call."""
    key, mt = str(asts_path), asts_path.stat().st_mtime
    hit = _INDEX_CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    by_id, by_name = _build_index(asts_path)
    _INDEX_CACHE[key] = (mt, by_id, by_name)
    return by_id, by_name


def function_source(sources_root: str, name: str, *, max_chars: int = 8_000, max_defs: int = 4) -> str | None:
    """The Solidity body(ies) of `name` + the in-tree functions each calls, sliced exactly from the AST.
    On-demand version of the prefetch: precise (overloads/inheritance resolve via node ids, not regex) and
    lazy (the agent pulls one function at a time). Returns None if there is no AST or no such function —
    the caller then falls back to grep_files/get_file. Prefers IMPLEMENTED defs; caps count + size."""
    asts_path = find_asts(sources_root)
    if asts_path is None:
        return None
    by_id, by_name = _cached_index(asts_path)
    ids = by_name.get(name)
    if not ids:
        return None
    recs = [by_id[i] for i in ids]
    recs = [r for r in recs if r["impl"]] or recs          # prefer real bodies over interface decls
    out, total = [], 0
    for rec in recs[:max_defs]:
        body = _slice(sources_root, rec["file"], rec["src"])
        if not body:
            continue
        callees = sorted({f"{by_id[c]['name']} ({by_id[c]['file']})" for c in rec["callees"] if c in by_id})
        sec = f"### {name}  ({rec['file']})\n{body}\n"
        if callees:
            sec += f"// calls (in-tree): {', '.join(callees[:20])}  [get_function any of these to expand]\n"
        out.append(sec)
        total += len(sec)
        if total > max_chars:
            out.append(f"### … more definitions of {name} omitted (budget) — grep_files to see them.\n")
            break
    if len(recs) > max_defs:
        out.append(f"### note: {len(recs)} definitions of `{name}` exist; showing {max_defs}.\n")
    return "\n".join(out) if out else None


