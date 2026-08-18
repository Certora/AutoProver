"""Bridge to the compiled scene: load CUT method FACTS + the Solidity->CVL type mapper by REUSING
AutoProver's own parsers (no duplicated parsing logic). Feeds `Signature.from_scene` /
`FunctionSpec.from_scene` in ir.py.

Requires a compiled scene: a `.certora_internal` dir holding `all_methods.json` +
`all_user_defined_types.json` (a Certora build artifact). Offline recon agent-sims do NOT need this —
they hand-author facts via `FunctionSpec.of(...)`.

Reused, not reinvented:
  - `MethodParser`  -> loads `all_methods.json` (name, fullSignature, paramNames, returns,
                       stateMutability, visibility per method).
  - `TypeAnalyzer._resolve_type_from_string(s).cvl_name` -> the canonical Solidity-string -> CVL
                       spelling (qualifies dotted struct types like `IFoo.Bar`, handles arrays). Same mapper
                       autosetup uses to emit its summaries. Its output strings flow through
                       `cvlx.ty` into composer.cvl.schema (the EVMVerifier CVL-AST mirror).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from certora_autosetup.parsers.method_parser import MethodParser
from certora_autosetup.parsers.type_analyzer import TypeAnalyzer


def load_methods(certora_internal_path: str = ".certora_internal") -> MethodParser:
    """A `MethodParser` over the scene's `all_methods.json`."""
    return MethodParser(str(Path(certora_internal_path) / "all_methods.json"))


def type_resolver(certora_internal_path: str = ".certora_internal"):
    """Return `resolve(solidity_type_str) -> cvl_type_str`, backed by AutoProver's `TypeAnalyzer`."""
    ta = TypeAnalyzer(certora_internal_path)
    ta.parse_all()   # loads user-defined types + methods into the registry (required before resolving)
    return lambda s: ta._resolve_type_from_string(s).cvl_name


def method_dict(parser: MethodParser, contract: str, name: str) -> dict | None:
    """The `all_methods.json` entry for `contract.name` (first match), or None."""
    for m in parser.get_methods_by_contract(contract):
        if m["name"] == name:
            return m
    return None


def methods_from_build(build_json_path) -> list[dict]:
    """Extract method FACTS in the `all_methods.json` shape directly from certoraRun's
    `.certora_build.json`, REUSING certora_autosetup's `parse_type_descriptor` for the typeDesc -> CVL
    type-string flattening (primitives AND structs/arrays — no reinvention) — WITHOUT running autosetup.
    `certoraRun --compilation_steps_only` emits `.certora_build.json` (raw typeDescs); autosetup's
    `generate_all_methods_json` normally post-processes it, but that is a stateful method — here we do
    just the fields `Signature.from_scene` reads (name/contractName/fullSignature/paramNames/returns/
    stateMutability/visibility), deduped like autosetup's `_process_method_info`."""
    from certora_autosetup.utils.types import parse_type_descriptor, TypeParseMode
    Q = TypeParseMode.QUALIFIED
    data = json.loads(Path(build_json_path).read_text())
    out: list[dict] = []
    seen: set = set()
    for obj in data.values():
        if not (isinstance(obj, dict) and "contracts" in obj):
            continue
        for c in obj.get("contracts", []) or []:
            cname = c.get("name", "")
            for m in c.get("allMethods", []) or []:
                mc = m.get("contractName", cname)
                sig = [parse_type_descriptor(a.get("typeDesc", {}), Q, mc) for a in m.get("fullArgs", [])]
                rets = [parse_type_descriptor(r.get("typeDesc", {}), Q, mc) for r in m.get("returns", [])]
                key = (mc, m.get("name"), tuple(sig))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": m.get("name", ""), "contractName": mc, "fullSignature": sig,
                            "paramNames": list(m.get("paramNames", [])), "returns": rets,
                            "stateMutability": m.get("stateMutability", "nonpayable"),
                            "visibility": m.get("visibility", "external")})
    return out


def canonical_arg_types(build_json_path, contract: str, fn: str) -> list[str] | None:
    """The CANONICAL (underlying) arg types of `contract.fn` from `.certora_build.json`, REUSING
    autosetup's `parse_type_descriptor` in CANONICAL mode — which recurses a UDVT into its `underlying`
    (so a uint256-backed UDVT array -> `uint256[]`, a bytes31-backed UDVT -> `bytes31`). Lets the
    deterministic-memo summary (detsummary) resolve an array element's base type + cast WITHOUT grepping
    source. None if not found."""
    from certora_autosetup.utils.types import parse_type_descriptor, TypeParseMode
    C = TypeParseMode.CANONICAL
    data = json.loads(Path(build_json_path).read_text())
    for obj in data.values():
        if not (isinstance(obj, dict) and "contracts" in obj):
            continue
        for c in obj.get("contracts", []) or []:
            for m in c.get("allMethods", []) or []:
                mc = m.get("contractName", c.get("name", ""))
                if m.get("name") == fn and mc == contract:
                    return [parse_type_descriptor(a.get("typeDesc", {}), C, mc) for a in m.get("fullArgs", [])]
    return None


def _newest_build_json(sources_root) -> Path | None:
    cands = sorted(Path(sources_root, ".certora_internal").glob("*/.certora_build.json"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def ensure_all_methods_json(sources_root, setup_conf, scene_path=None,
                            certora_run_path: str = "certoraRun") -> str:
    """Return a `.certora_internal` dir containing `all_methods.json` for a scene-sourced input.

    Preference: an EXISTING all_methods.json (in the integrated flow autosetup already produced it) ->
    else DERIVE one from certoraRun's `.certora_build.json` via `methods_from_build` (reuse, not
    autosetup), compiling once with `--compilation_steps_only` if no build is present yet. The derived
    fullSignature/returns are already CVL type strings, so `Signature.from_scene` reads them with an
    identity resolver (SceneInput falls back to identity when all_user_defined_types.json is absent).
    Fails loud if it cannot produce the file."""
    scene_dir = Path(scene_path) if scene_path else Path(sources_root, ".certora_internal")
    if (scene_dir / "all_methods.json").exists():
        return str(scene_dir)
    build = _newest_build_json(sources_root)
    if build is None:
        subprocess.run([certora_run_path, str(setup_conf), "--compilation_steps_only"],
                       cwd=str(sources_root), check=True)
        build = _newest_build_json(sources_root)
    if build is None:
        raise RuntimeError(
            "cannot derive all_methods.json: no .certora_build.json after --compilation_steps_only. "
            "Pass --scene pointing at an autosetup-produced .certora_internal (with all_methods.json).")
    out = Path(sources_root, ".certora_internal", "all_methods.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(methods_from_build(build), indent=2))
    return str(out.parent)


def mutability_resolver(certora_internal_path: str = ".certora_internal"):
    """Return `resolve(name) -> stateMutability | None` from the scene, for cross-checking NONDET
    targets (mutations.add_nondet). CONSERVATIVE: if any contract's method of that name is
    state-changing, report state-changing (fail toward unsound-if-NONDET'd). None if the name is
    absent from the scene."""
    parser = load_methods(certora_internal_path)

    def resolve(name: str):
        muts = [m.get("stateMutability") for m in parser.get_methods_by_name(name)]
        if not muts:
            return None
        for strong in ("payable", "nonpayable"):
            if strong in muts:
                return strong
        return muts[0]

    return resolve


def signature_from_scene(certora_internal_path, contract, name):
    """Native `Signature` for `contract.name` from the compiled scene's `all_methods.json`, or None if
    absent. Reuses AutoProver's `MethodParser` + `TypeAnalyzer` via `Signature.from_scene` — no string
    parsing. (Fallback to `methods_from_build` when only the raw build json is present is a TODO.)"""
    from .ir import Signature
    m = method_dict(load_methods(certora_internal_path), contract, name)
    return Signature.from_scene(m, type_resolver(certora_internal_path)) if m else None
