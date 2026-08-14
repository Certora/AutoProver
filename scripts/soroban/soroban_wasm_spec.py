#!/usr/bin/env python3
"""
soroban_wasm_spec.py -- describe the exported functions of a Soroban contract .wasm

Reads the `contractspecv0` custom section (XDR-encoded SCSpecEntry stream) plus the
wasm export table, and writes a JSON description of every exported function: its
parameter names/types, return type, doc comment, and the user-defined types and
error enums it references.

Each function is attributed to the `#[contract]` type whose `#[contractimpl]`
block produced it, read out of the wasm `name` section. `#[contractimpl]`
generates, for every exported method, a hidden wrapper module named
`__<ContractType>__<fn_name>` containing an `invoke_raw` function; that module
path survives in the `name` section of any build that keeps symbols, and it
names both the contract type and the exported function. The trait, if the
method came from a trait impl, is recoverable from the implementing symbol.
So the output records, per function:

    contract.type        e.g. "StrategyVaultContract"     (definitive)
    contract.crate       e.g. "strategy_vault"
    contract.trait       e.g. "stellar_tokens::vault::FungibleVault", or null
    contract.impl_kind   "trait_impl" | "inherent"

  This needs symbols. A release build with `strip = "symbols"` has no `name`
  section, and `contractspecv0` records no impl association whatsoever -- so
  for a stripped wasm every `contract` block is null and a warning says so.
  Build with `strip = "none"` (or a debug build) to get the attribution.

Functions are *additionally* grouped by standard interface (SEP-41 fungible
token, non-fungible token, tokenized vault, RWA, pausable, ...), with anything
unrecognized collected under `custom`.

  IMPORTANT: unlike the contract attribution above, that grouping is *inferred*.
  Functions are matched against the catalog in INTERFACE_CATALOG below by name
  and type signature; matches are labelled `exact` or `name_only` so you can see
  how much weight a grouping carries. Extend the catalog in-place or merge
  additions with `--catalog extra.json`. When trait information is available
  from the symbols, the two groupings are cross-checked and disagreements are
  reported as warnings.

No third-party dependencies -- pure stdlib wasm + XDR parsing.

Usage:
    python3 soroban_wasm_spec.py contract.wasm                 # -> contract.spec.json
    python3 soroban_wasm_spec.py contract.wasm -o out.json
    python3 soroban_wasm_spec.py contract.wasm --all-entries   # include events + every UDT
    python3 soroban_wasm_spec.py contract.wasm --print         # also dump signatures to stdout
    python3 soroban_wasm_spec.py contract.wasm --catalog my_interfaces.json
    python3 soroban_wasm_spec.py contract.wasm --no-grouping   # skip interface inference
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# wasm container
# --------------------------------------------------------------------------- #

EXTERNAL_KIND = {0: "func", 1: "table", 2: "memory", 3: "global"}


class WasmError(Exception):
    pass


def _uleb128(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WasmError("truncated LEB128")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise WasmError("LEB128 too long")


def parse_wasm_sections(data: bytes) -> tuple[dict[int, list[bytes]], dict[str, bytes]]:
    """Return ({section_id: [payloads]}, {custom_section_name: payload})."""
    if data[:4] != b"\x00asm":
        raise WasmError("not a wasm module (bad magic)")
    version = struct.unpack_from("<I", data, 4)[0]
    if version != 1:
        raise WasmError(f"unsupported wasm version {version}")

    sections: dict[int, list[bytes]] = {}
    customs: dict[str, bytes] = {}
    pos = 8
    while pos < len(data):
        section_id = data[pos]
        pos += 1
        size, pos = _uleb128(data, pos)
        payload = data[pos : pos + size]
        pos += size
        sections.setdefault(section_id, []).append(payload)
        if section_id == 0:  # custom
            name_len, q = _uleb128(payload, 0)
            name = payload[q : q + name_len].decode("utf-8", "replace")
            customs[name] = payload[q + name_len :]
    return sections, customs


def parse_exports(payload: bytes) -> list[dict]:
    count, pos = _uleb128(payload, 0)
    exports = []
    for _ in range(count):
        name_len, pos = _uleb128(payload, pos)
        name = payload[pos : pos + name_len].decode("utf-8", "replace")
        pos += name_len
        kind = payload[pos]
        pos += 1
        index, pos = _uleb128(payload, pos)
        exports.append({"name": name, "kind": EXTERNAL_KIND.get(kind, str(kind)), "index": index})
    return exports


# --------------------------------------------------------------------------- #
# XDR reader
# --------------------------------------------------------------------------- #


class XdrReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def u32(self) -> int:
        if self.remaining() < 4:
            raise WasmError("truncated XDR (u32)")
        value = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i32(self) -> int:
        value = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def enum(self) -> int:
        return self.i32()

    def opaque_var(self) -> bytes:
        length = self.u32()
        if self.remaining() < length:
            raise WasmError("truncated XDR (opaque)")
        raw = self.data[self.pos : self.pos + length]
        self.pos += length + (-length % 4)  # 4-byte alignment padding
        return raw

    def string(self) -> str:
        return self.opaque_var().decode("utf-8", "replace")

    def array(self, read_one):
        return [read_one() for _ in range(self.u32())]


# --------------------------------------------------------------------------- #
# SCSpec XDR
# --------------------------------------------------------------------------- #

SC_SPEC_TYPE = {
    0: "Val",
    1: "bool",
    2: "void",
    3: "Error",
    4: "u32",
    5: "i32",
    6: "u64",
    7: "i64",
    8: "Timepoint",
    9: "Duration",
    10: "u128",
    11: "i128",
    12: "u256",
    13: "i256",
    14: "Bytes",
    16: "String",
    17: "Symbol",
    19: "Address",
    20: "MuxedAddress",
}

T_OPTION, T_RESULT, T_VEC, T_MAP, T_TUPLE, T_BYTES_N, T_UDT = (
    1000,
    1001,
    1002,
    1004,
    1005,
    1006,
    2000,
)

ENTRY_KIND = {
    0: "function",
    1: "udt_struct",
    2: "udt_union",
    3: "udt_enum",
    4: "udt_error_enum",
    5: "event",
}

EVENT_PARAM_LOCATION = {0: "data", 1: "topic_list"}
EVENT_DATA_FORMAT = {0: "single_value", 1: "vec", 2: "map"}


def read_type(r: XdrReader) -> dict:
    """Read an SCSpecTypeDef. Returns a nested dict with a rendered `display`."""
    kind = r.enum()

    if kind in SC_SPEC_TYPE:
        name = SC_SPEC_TYPE[kind]
        return {"type": name, "display": name}

    if kind == T_OPTION:
        inner = read_type(r)
        return {"type": "option", "value_type": inner, "display": f"Option<{inner['display']}>"}

    if kind == T_RESULT:
        ok = read_type(r)
        err = read_type(r)
        return {
            "type": "result",
            "ok_type": ok,
            "error_type": err,
            "display": f"Result<{ok['display']}, {err['display']}>",
        }

    if kind == T_VEC:
        inner = read_type(r)
        return {"type": "vec", "element_type": inner, "display": f"Vec<{inner['display']}>"}

    if kind == T_MAP:
        key = read_type(r)
        val = read_type(r)
        return {
            "type": "map",
            "key_type": key,
            "value_type": val,
            "display": f"Map<{key['display']}, {val['display']}>",
        }

    if kind == T_TUPLE:
        parts = r.array(lambda: read_type(r))
        return {
            "type": "tuple",
            "value_types": parts,
            "display": "(" + ", ".join(p["display"] for p in parts) + ")",
        }

    if kind == T_BYTES_N:
        n = r.u32()
        return {"type": "bytes_n", "n": n, "display": f"BytesN<{n}>"}

    if kind == T_UDT:
        name = r.string()
        return {"type": "udt", "name": name, "display": name}

    raise WasmError(f"unknown SCSpecType discriminant {kind}")


def read_entry(r: XdrReader) -> dict:
    """Read one SCSpecEntry."""
    kind = r.enum()
    kind_name = ENTRY_KIND.get(kind)
    if kind_name is None:
        raise WasmError(f"unknown SCSpecEntry kind {kind}")

    if kind_name == "function":
        doc = r.string()
        name = r.string()  # SCSymbol

        def one_input():
            return {"doc": r.string(), "name": r.string(), "type": read_type(r)}

        inputs = r.array(one_input)
        outputs = r.array(lambda: read_type(r))
        return {"kind": "function", "doc": doc, "name": name, "inputs": inputs, "outputs": outputs}

    if kind_name == "udt_struct":
        doc, lib, name = r.string(), r.string(), r.string()

        def one_field():
            return {"doc": r.string(), "name": r.string(), "type": read_type(r)}

        return {
            "kind": "udt_struct",
            "doc": doc,
            "lib": lib,
            "name": name,
            "fields": r.array(one_field),
        }

    if kind_name == "udt_union":
        doc, lib, name = r.string(), r.string(), r.string()

        def one_case():
            case_kind = r.enum()  # 0 = void, 1 = tuple
            if case_kind == 0:
                return {"kind": "void", "doc": r.string(), "name": r.string()}
            if case_kind == 1:
                return {
                    "kind": "tuple",
                    "doc": r.string(),
                    "name": r.string(),
                    "types": r.array(lambda: read_type(r)),
                }
            raise WasmError(f"unknown union case kind {case_kind}")

        return {
            "kind": "udt_union",
            "doc": doc,
            "lib": lib,
            "name": name,
            "cases": r.array(one_case),
        }

    if kind_name in ("udt_enum", "udt_error_enum"):
        doc, lib, name = r.string(), r.string(), r.string()

        def one_case():
            return {"doc": r.string(), "name": r.string(), "value": r.u32()}

        return {
            "kind": kind_name,
            "doc": doc,
            "lib": lib,
            "name": name,
            "cases": r.array(one_case),
        }

    # event (protocol 23+)
    doc, lib, name = r.string(), r.string(), r.string()
    prefix_topics = r.array(r.string)

    def one_param():
        return {
            "doc": r.string(),
            "name": r.string(),
            "type": read_type(r),
            "location": EVENT_PARAM_LOCATION.get(r.enum(), "unknown"),
        }

    params = r.array(one_param)
    data_format = EVENT_DATA_FORMAT.get(r.enum(), "unknown")
    return {
        "kind": "event",
        "doc": doc,
        "lib": lib,
        "name": name,
        "prefix_topics": prefix_topics,
        "params": params,
        "data_format": data_format,
    }


def parse_spec(blob: bytes) -> tuple[list[dict], int]:
    """Parse the whole SCSpecEntry stream. Returns (entries, unparsed_trailing_bytes)."""
    r = XdrReader(blob)
    entries = []
    while r.remaining() > 0:
        before = r.pos
        try:
            entries.append(read_entry(r))
        except Exception as exc:  # noqa: BLE001 - report and stop, keep what we have
            print(
                f"warning: stopped parsing spec at byte {before} ({exc})",
                file=sys.stderr,
            )
            r.pos = before
            break
    return entries, r.remaining()


# --------------------------------------------------------------------------- #
# metadata sections
# --------------------------------------------------------------------------- #


def parse_contract_meta(blob: bytes) -> dict:
    """contractmetav0 is a stream of SCMetaEntry (kind 0 = SC_META_V0 {key, val})."""
    r = XdrReader(blob)
    meta = {}
    while r.remaining() > 0:
        before = r.pos
        try:
            if r.enum() != 0:
                raise WasmError("unexpected SCMetaEntry kind")
            key = r.string()
            meta[key] = r.string()
        except Exception:  # noqa: BLE001
            r.pos = before
            break
    return meta


def parse_env_meta(blob: bytes) -> dict:
    """contractenvmetav0: SCEnvMetaEntry, kind 0 = interface version."""
    r = XdrReader(blob)
    out = {}
    try:
        if r.enum() == 0:
            protocol = r.u32()
            pre_release = r.u32()
            out["interface_version"] = {"protocol": protocol, "pre_release": pre_release}
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- #
# name section + Rust symbol paths
# --------------------------------------------------------------------------- #

NAME_SUBSECTION_FUNCTION = 1

# `#[contractimpl]` wraps each exported method in a hidden module. Empirically
# (soroban-sdk 23.x) that module is `__<ContractType>__<fn_name>` and holds an
# `invoke_raw` function. Rather than assume a fixed underscore count, we match
# the module ident against the set of names actually exported by the wasm, which
# is ground truth -- see split_wrapper_module().
WRAPPER_LEAF = "invoke_raw"


def parse_name_section(payload: bytes) -> dict[int, str]:
    """{function index: symbol} from the `name` custom section's subsection 1."""
    names: dict[int, str] = {}
    pos = 0
    while pos < len(payload):
        subsection_id = payload[pos]
        pos += 1
        size, pos = _uleb128(payload, pos)
        body = payload[pos : pos + size]
        pos += size
        if subsection_id != NAME_SUBSECTION_FUNCTION:
            continue
        count, q = _uleb128(body, 0)
        for _ in range(count):
            index, q = _uleb128(body, q)
            length, q = _uleb128(body, q)
            names[index] = body[q : q + length].decode("utf-8", "replace")
            q += length
    return names


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BASE62 = re.compile(r"[0-9a-zA-Z]*_")


def demangle_path(symbol: str) -> list[str]:
    """Path identifiers from a Rust mangled symbol, outermost first.

    This is a path *extractor*, not a full demangler: it pulls out the
    length-prefixed identifiers and skips the encoding scaffolding. That is all
    the contract attribution needs, and it avoids implementing the whole v0
    grammar (compression backrefs, generic arguments, const values).

    Handles v0 (`_R...`) and legacy (`_ZN...`) mangling. Unmangled symbols come
    back as a single component.
    """
    if symbol.startswith("_ZN"):
        # Legacy: plain <len><ident> runs, terminated by 'E'. The trailing
        # 17h<16 hex digits> hash component is dropped.
        out: list[str] = []
        i = 3
        while i < len(symbol) and symbol[i].isdigit():
            j = i
            while j < len(symbol) and symbol[j].isdigit():
                j += 1
            length = int(symbol[i:j])
            out.append(symbol[j : j + length])
            i = j + length
        if out and re.fullmatch(r"h[0-9a-f]{16}", out[-1]):
            out.pop()
        return out

    if not symbol.startswith("_R"):
        return [symbol]

    body = symbol[2:]
    out = []
    i = 0
    while i < len(body):
        char = body[i]
        if char.isdigit():
            j = i
            while j < len(body) and body[j].isdigit():
                j += 1
            length = int(body[i:j])
            # v0 inserts a '_' separator before an identifier that would
            # otherwise be ambiguous (one starting with '_' or a digit).
            start = j + 1 if j < len(body) and body[j] == "_" else j
            candidate = body[start : start + length]
            if len(candidate) == length and _IDENT.match(candidate):
                out.append(candidate)
                i = start + length
                continue
            i += 1
            continue
        if char in "BsS":
            # Backref / disambiguator: <tag><base62>_
            match = _BASE62.match(body, i + 1)
            i = match.end() if match else i + 1
            continue
        i += 1
    return out


def split_wrapper_module(module_ident: str, export_names: set[str]) -> tuple[str, str] | None:
    """`__StrategyVaultContract__deposit` -> ("StrategyVaultContract", "deposit").

    The contract type and the function name are both arbitrary identifiers that
    may themselves contain underscores, so the boundary is found by matching the
    tail against the names the wasm actually exports (longest match wins, which
    resolves `__constructor` vs `_constructor`).
    """
    core = module_ident.lstrip("_")
    candidates = [name for name in export_names if core.endswith(name)]
    if not candidates:
        return None
    fn_name = max(candidates, key=len)
    contract = core[: len(core) - len(fn_name)].rstrip("_")
    if not contract:
        return None
    return contract, fn_name


def attribute_contracts(
    func_names: dict[int, str], export_names: set[str]
) -> tuple[dict[str, dict], dict[str, int]]:
    """Map each exported function to the contract type that implements it.

    Returns (attribution by function name, diagnostics).
    """
    paths = {index: demangle_path(sym) for index, sym in func_names.items()}

    # -- pass 1: the #[contractimpl] wrapper modules -------------------- #
    attribution: dict[str, dict] = {}
    for index, path in paths.items():
        if len(path) < 2 or path[-1] != WRAPPER_LEAF:
            continue
        split = split_wrapper_module(path[-2], export_names)
        if split is None:
            continue
        contract, fn_name = split
        attribution[fn_name] = {
            "type": contract,
            "crate": path[0] if path else None,
            "module": "::".join(path[1:-2]) or None,
            "trait": None,
            "impl_kind": None,
            "wrapper_symbol": func_names[index],
            "wrapper_func_index": index,
            "impl_symbol": None,
            "source": "name_section",
        }

    # -- pass 2: locate the implementing method for each ---------------- #
    # A trait impl mangles as <contract type> <trait path> <method>, an inherent
    # method as <contract type> <method>. Since the contract type is already
    # known from pass 1, whatever sits strictly between it and the method name
    # is the trait path -- no CamelCase guessing needed.
    for fn_name, record in attribution.items():
        contract = record["type"]
        best: tuple[int, list[str]] | None = None
        for index, path in paths.items():
            if not path or path[-1] != fn_name or contract not in path:
                continue
            if path[-2].startswith("__"):  # the wrapper module itself
                continue
            position = path.index(contract)
            if position + 1 > len(path) - 1:
                continue
            # Prefer the shortest path (the direct impl, not a closure inside it).
            if best is None or len(path) < len(best[1]):
                best = (index, path)
        if best is None:
            continue
        index, path = best
        position = path.index(contract)
        trait_path = path[position + 1 : -1]
        record["impl_symbol"] = func_names[index]
        record["trait"] = "::".join(trait_path) or None
        record["impl_kind"] = "trait_impl" if trait_path else "inherent"

    diagnostics = {
        "named_functions": len(func_names),
        "mangled_functions": sum(1 for s in func_names.values() if s.startswith(("_R", "_ZN"))),
        "wrappers_found": len(attribution),
        "impls_resolved": sum(1 for r in attribution.values() if r["impl_symbol"]),
    }
    return attribution, diagnostics


def group_by_contract(
    attribution: dict[str, dict], all_function_names: list[str]
) -> dict[str, dict]:
    """Group functions by contract type, then by the trait they came from."""
    contracts: dict[str, dict] = {}
    for fn_name in sorted(all_function_names):
        record = attribution.get(fn_name)
        if record is None:
            continue
        entry = contracts.setdefault(
            record["type"],
            {
                "crate": record["crate"],
                "module": record["module"],
                "functions": [],
                "by_trait": {},
                "inherent": [],
            },
        )
        entry["functions"].append(fn_name)
        if record["impl_kind"] == "trait_impl":
            entry["by_trait"].setdefault(record["trait"], []).append(fn_name)
        elif record["impl_kind"] == "inherent":
            entry["inherent"].append(fn_name)
        else:
            entry["by_trait"].setdefault("<unresolved>", []).append(fn_name)
    return contracts


# --------------------------------------------------------------------------- #
# interface catalog (contract types)
# --------------------------------------------------------------------------- #
#
# Each interface lists its functions as {name: [accepted signatures]}, where a
# signature is [[param types...], return type]. A function may have several
# accepted signatures because implementations differ (e.g. a fungible
# `transfer` whose `to` is an `Address` in some builds and a `MuxedAddress` in
# others). Type strings are the `display` forms produced by read_type().
#
# `unique` names are ones distinctive enough that a single match is evidence
# the interface is present; everything else needs corroboration (see
# MIN_CORROBORATED_MATCHES). This is what keeps a lone `name()` from being
# reported as a whole NFT implementation.
#
# `error_enums` ties the interface to the error namespaces in the binary, which
# is real evidence rather than inference -- an interface whose error enum is
# present is much more likely to be genuinely implemented.

INTERFACE_CATALOG: dict[str, dict] = {
    "constructor": {
        "description": "Contract constructor, invoked once at deploy time.",
        "functions": {"__constructor": []},
        "unique": ["__constructor"],
        "error_enums": [],
    },
    "sep41_fungible_token": {
        "description": "SEP-41 fungible token interface, plus the common "
        "total_supply/mint/burn extensions.",
        "functions": {
            "name": [[[], "String"]],
            "symbol": [[[], "String"]],
            "decimals": [[[], "u32"]],
            "total_supply": [[[], "i128"]],
            "balance": [[["Address"], "i128"]],
            "allowance": [[["Address", "Address"], "i128"]],
            "approve": [[["Address", "Address", "i128", "u32"], "()"]],
            "transfer": [
                [["Address", "Address", "i128"], "()"],
                [["Address", "MuxedAddress", "i128"], "()"],
            ],
            "transfer_from": [[["Address", "Address", "Address", "i128"], "()"]],
            "burn": [[["Address", "i128"], "()"]],
            "burn_from": [[["Address", "Address", "i128"], "()"]],
            "mint": [[["Address", "i128"], "()"], [["Address", "i128"], "i128"]],
        },
        "unique": ["decimals", "allowance", "burn_from"],
        "error_enums": ["FungibleTokenError"],
    },
    "non_fungible_token": {
        "description": "Non-fungible token interface. Distinguished from the "
        "fungible one by u32 token ids where the fungible interface has i128 "
        "amounts.",
        "functions": {
            "name": [[[], "String"]],
            "symbol": [[[], "String"]],
            "token_uri": [[["u32"], "String"]],
            "owner_of": [[["u32"], "Address"]],
            "balance": [[["Address"], "u32"]],
            "transfer": [[["Address", "Address", "u32"], "()"]],
            "transfer_from": [[["Address", "Address", "Address", "u32"], "()"]],
            "approve": [[["Address", "Address", "u32", "u32"], "()"]],
            "approve_for_all": [[["Address", "Address", "u32"], "()"]],
            "get_approved": [[["u32"], "Option<Address>"]],
            "is_approved_for_all": [[["Address", "Address"], "bool"]],
            "mint": [[["Address", "u32"], "()"]],
            "burn": [[["Address", "u32"], "()"]],
        },
        "unique": [
            "owner_of",
            "token_uri",
            "approve_for_all",
            "get_approved",
            "is_approved_for_all",
        ],
        "error_enums": ["NonFungibleTokenError"],
    },
    "vault": {
        "description": "Tokenized vault (ERC-4626-shaped): shares minted "
        "against a deposited asset, with preview/max quoting helpers.",
        "functions": {
            "query_asset": [[[], "Address"]],
            "total_assets": [[[], "i128"]],
            "convert_to_shares": [[["i128"], "i128"]],
            "convert_to_assets": [[["i128"], "i128"]],
            "max_deposit": [[["Address"], "i128"]],
            "preview_deposit": [[["i128"], "i128"]],
            "deposit": [
                [["i128", "Address", "Address", "Address"], "i128"],
                [["i128", "Address", "Address"], "i128"],
            ],
            "max_mint": [[["Address"], "i128"]],
            "preview_mint": [[["i128"], "i128"]],
            "mint": [
                [["i128", "Address", "Address", "Address"], "i128"],
                [["i128", "Address", "Address"], "i128"],
            ],
            "max_withdraw": [[["Address"], "i128"]],
            "preview_withdraw": [[["i128"], "i128"]],
            "withdraw": [
                [["i128", "Address", "Address", "Address"], "i128"],
                [["i128", "Address", "Address"], "i128"],
            ],
            "max_redeem": [[["Address"], "i128"]],
            "preview_redeem": [[["i128"], "i128"]],
            "redeem": [
                [["i128", "Address", "Address", "Address"], "i128"],
                [["i128", "Address", "Address"], "i128"],
            ],
        },
        "unique": [
            "query_asset",
            "total_assets",
            "convert_to_shares",
            "convert_to_assets",
            "preview_deposit",
            "preview_mint",
            "preview_withdraw",
            "preview_redeem",
            "max_deposit",
            "max_mint",
            "max_withdraw",
            "max_redeem",
        ],
        "error_enums": ["VaultTokenError"],
    },
    "pausable": {
        "description": "Emergency pause switch.",
        "functions": {
            "paused": [[[], "bool"]],
            "pause": [[["Address"], "()"], [[], "()"]],
            "unpause": [[["Address"], "()"], [[], "()"]],
        },
        "unique": ["paused", "pause", "unpause"],
        "error_enums": ["PausableError"],
    },
    "upgradeable": {
        "description": "Wasm upgrade / migration hooks.",
        "functions": {
            "upgrade": [[["BytesN<32>", "Address"], "()"], [["BytesN<32>"], "()"]],
            "migrate": [],
            "complete_migration": [],
            "rollback": [],
        },
        "unique": ["upgrade", "complete_migration"],
        "error_enums": ["UpgradeableError"],
    },
    "access_control": {
        "description": "Role-based access control / ownership.",
        "functions": {
            "has_role": [[["Address", "Symbol"], "Option<u32>"]],
            "grant_role": [[["Address", "Address", "Symbol"], "()"]],
            "revoke_role": [[["Address", "Address", "Symbol"], "()"]],
            "renounce_role": [[["Address", "Symbol"], "()"]],
            "get_role_member": [[["Symbol", "u32"], "Address"]],
            "get_role_member_count": [[["Symbol"], "u32"]],
            "get_admin": [[[], "Option<Address>"]],
            "transfer_admin_role": [[["Address", "Address", "u32"], "()"]],
            "accept_admin_transfer": [[["Address"], "()"]],
            "owner": [[[], "Option<Address>"]],
            "transfer_ownership": [[["Address", "Address", "u32"], "()"]],
        },
        "unique": [
            "has_role",
            "grant_role",
            "revoke_role",
            "renounce_role",
            "transfer_admin_role",
            "accept_admin_transfer",
            "transfer_ownership",
        ],
        "error_enums": [],
    },
    "rwa": {
        "description": "Real-world-asset token: freezing, compliance modules, "
        "identity verification, recovery.",
        "functions": {
            "set_address_frozen": [[["Address", "bool", "Address"], "()"]],
            "is_frozen": [[["Address"], "bool"]],
            "freeze_partial_tokens": [[["Address", "i128", "Address"], "()"]],
            "unfreeze_partial_tokens": [[["Address", "i128", "Address"], "()"]],
            "get_frozen_tokens": [[["Address"], "i128"]],
            "recover_address": [[["Address", "Address", "Address"], "()"]],
            "compliance": [[[], "Address"]],
            "set_compliance": [[["Address"], "()"]],
            "identity_verifier": [[[], "Address"]],
            "set_identity_verifier": [[["Address"], "()"]],
            "onchain_id": [[[], "Address"]],
            "set_onchain_id": [[["Address"], "()"]],
            "claim_topics_and_issuers": [[[], "Address"]],
            "set_claim_topics_and_issuers": [[["Address"], "()"]],
            "version": [[[], "String"]],
        },
        "unique": [
            "set_address_frozen",
            "freeze_partial_tokens",
            "unfreeze_partial_tokens",
            "get_frozen_tokens",
            "recover_address",
            "set_identity_verifier",
            "set_onchain_id",
            "set_claim_topics_and_issuers",
        ],
        "error_enums": ["RWAError", "ComplianceError", "IRSError"],
    },
    "merkle_distributor": {
        "description": "Merkle-proof airdrop / claim distributor.",
        "functions": {
            "set_root": [[["BytesN<32>"], "()"]],
            "root": [[[], "BytesN<32>"]],
            "is_claimed": [[["u32"], "bool"]],
            "set_claimed": [[["u32"], "()"]],
        },
        "unique": ["set_root", "is_claimed", "set_claimed"],
        "error_enums": ["MerkleDistributorError"],
    },
    "allowlist_blocklist": {
        "description": "Per-user allow/block gating.",
        "functions": {
            "allowed": [[["Address"], "bool"]],
            "allow_user": [[["Address", "Address"], "()"]],
            "disallow_user": [[["Address", "Address"], "()"]],
            "blocked": [[["Address"], "bool"]],
            "block_user": [[["Address", "Address"], "()"]],
            "unblock_user": [[["Address", "Address"], "()"]],
        },
        "unique": [
            "allow_user",
            "disallow_user",
            "block_user",
            "unblock_user",
        ],
        "error_enums": [],
    },
}

# An interface backed only by non-`unique` name matches needs at least this
# many of them before it is reported. Prevents `name()`/`symbol()` alone from
# conjuring an interface that is not there.
MIN_CORROBORATED_MATCHES = 2


def signature_of(fn: dict) -> tuple[list[str], str]:
    """(param type displays, return type display) for a spec function entry."""
    params = [i["type"]["display"] for i in fn["inputs"]]
    ret = fn["outputs"][0]["display"] if fn["outputs"] else "()"
    if ret == "void":
        ret = "()"
    return params, ret


def load_catalog(extra_path: Path | None) -> dict[str, dict]:
    """The built-in catalog, optionally merged with a user-supplied JSON file.

    User entries are merged per interface: `functions` are unioned (the user's
    signatures win for a colliding name), and `unique`/`error_enums` are
    unioned. A brand-new interface name is added wholesale.
    """
    catalog = {name: json.loads(json.dumps(spec)) for name, spec in INTERFACE_CATALOG.items()}
    if extra_path is None:
        return catalog

    extra = json.loads(extra_path.read_text())
    if not isinstance(extra, dict):
        raise CatalogError("catalog file must be a JSON object of interface -> spec")

    for name, spec in extra.items():
        if name not in catalog:
            catalog[name] = {
                "description": spec.get("description", ""),
                "functions": spec.get("functions", {}),
                "unique": spec.get("unique", []),
                "error_enums": spec.get("error_enums", []),
            }
            continue
        target = catalog[name]
        target["functions"].update(spec.get("functions", {}))
        target["unique"] = sorted(set(target["unique"]) | set(spec.get("unique", [])))
        target["error_enums"] = sorted(
            set(target["error_enums"]) | set(spec.get("error_enums", []))
        )
        if spec.get("description"):
            target["description"] = spec["description"]
    return catalog


class CatalogError(Exception):
    pass


def classify_functions(
    functions: list[dict], present_error_enums: set[str], catalog: dict[str, dict]
) -> tuple[dict[str, dict], list[dict]]:
    """Assign each function to an interface.

    Returns (interfaces, per-function assignment records). Two passes: collect
    every candidate match, then resolve each function to a single primary
    interface, preferring exact signature matches and, on a tie, the interface
    with the most corroborating evidence overall.
    """
    # -- pass 1: candidate matches ------------------------------------- #
    # candidates[fn_name] = [(interface, "exact"|"name_only"), ...]
    candidates: dict[str, list[tuple[str, str]]] = {}
    for fn in functions:
        params, ret = signature_of(fn)
        for iface_name, iface in catalog.items():
            accepted = iface["functions"].get(fn["name"])
            if accepted is None:
                continue
            if not accepted:
                # Catalogued by name only, no signature asserted.
                quality = "exact"
            else:
                quality = (
                    "exact"
                    if any(
                        list(sig_params) == params and sig_ret == ret
                        for sig_params, sig_ret in accepted
                    )
                    else "name_only"
                )
            candidates.setdefault(fn["name"], []).append((iface_name, quality))

    # -- interface-level evidence -------------------------------------- #
    # Score = exact matches, weighted up for `unique` names, plus a bonus when
    # the interface's error enum is actually present in the binary.
    def evidence(iface_name: str) -> tuple[int, int, int]:
        iface = catalog[iface_name]
        unique = set(iface["unique"])
        exact = sum(
            1
            for fn_name, matches in candidates.items()
            for name, quality in matches
            if name == iface_name and quality == "exact"
        )
        exact_unique = sum(
            1
            for fn_name, matches in candidates.items()
            for name, quality in matches
            if name == iface_name and quality == "exact" and fn_name in unique
        )
        enum_hit = int(bool(set(iface["error_enums"]) & present_error_enums))
        return exact_unique, exact, enum_hit

    # -- pass 2: resolve to one primary interface per function ---------- #
    assignments: list[dict] = []
    for fn in functions:
        matches = candidates.get(fn["name"], [])
        exact = [name for name, quality in matches if quality == "exact"]
        pool = exact or [name for name, _ in matches]

        if not pool:
            assignments.append(
                {"name": fn["name"], "interface": "custom", "match": "none", "also_matches": []}
            )
            continue

        # Rank by evidence, then by catalog order for determinism.
        order = list(catalog)
        primary = max(pool, key=lambda n: (evidence(n), -order.index(n)))
        assignments.append(
            {
                "name": fn["name"],
                "interface": primary,
                "match": "exact" if primary in exact else "name_only",
                "also_matches": sorted(n for n in pool if n != primary),
            }
        )

    # -- build interface groups ---------------------------------------- #
    claimed: dict[str, list[dict]] = {}
    for record in assignments:
        claimed.setdefault(record["interface"], []).append(record)

    interfaces: dict[str, dict] = {}
    for iface_name, iface in catalog.items():
        members = claimed.get(iface_name, [])
        if not members:
            continue
        unique = set(iface["unique"])
        exact_names = {m["name"] for m in members if m["match"] == "exact"}
        exact_unique = exact_names & unique
        enums_present = sorted(set(iface["error_enums"]) & present_error_enums)

        # Confidence: is this interface really here, or did one generic name
        # happen to collide?
        if exact_unique or enums_present:
            confidence = "high"
        elif len(exact_names) >= MIN_CORROBORATED_MATCHES:
            confidence = "medium"
        else:
            confidence = "low"

        declared = set(iface["functions"])
        found = {m["name"] for m in members}
        interfaces[iface_name] = {
            "description": iface["description"],
            "confidence": confidence,
            "evidence": {
                "exact_matches": sorted(exact_names),
                "distinctive_matches": sorted(exact_unique),
                "signature_mismatches": sorted(
                    m["name"] for m in members if m["match"] == "name_only"
                ),
                "error_enums_present": enums_present,
            },
            "coverage": {
                "implemented": len(found & declared),
                "declared": len(declared),
                "missing": sorted(declared - found),
                "complete": not (declared - found),
            },
            "functions": sorted(found),
        }

    custom = claimed.get("custom", [])
    if custom:
        interfaces["custom"] = {
            "description": "Not matched by any catalogued interface.",
            "confidence": "n/a",
            "evidence": {},
            "coverage": {},
            "functions": sorted(m["name"] for m in custom),
        }

    return interfaces, assignments


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def collect_udt_names(type_node: dict, into: set[str]) -> None:
    if type_node.get("type") == "udt":
        into.add(type_node["name"])
    for key in ("value_type", "ok_type", "error_type", "element_type", "key_type"):
        if key in type_node:
            collect_udt_names(type_node[key], into)
    for child in type_node.get("value_types", []):
        collect_udt_names(child, into)


def signature(fn: dict) -> str:
    params = ", ".join(f"{i['name']}: {i['type']['display']}" for i in fn["inputs"])
    ret = fn["outputs"][0]["display"] if fn["outputs"] else "()"
    return f"{fn['name']}({params}) -> {ret}"


def describe(
    wasm_path: Path,
    all_entries: bool = False,
    catalog: dict[str, dict] | None = None,
) -> dict:
    data = wasm_path.read_bytes()
    sections, customs = parse_wasm_sections(data)

    exports = parse_exports(sections[7][0]) if 7 in sections else []
    func_exports = [e for e in exports if e["kind"] == "func"]

    spec_blob = customs.get("contractspecv0")
    if spec_blob is None:
        raise WasmError(
            "no `contractspecv0` custom section -- not a Soroban contract, "
            "or built with the spec stripped"
        )
    entries, trailing = parse_spec(spec_blob)

    functions = [e for e in entries if e["kind"] == "function"]
    by_name = {e["name"]: e for e in entries if e["kind"] != "function"}

    # index -> [export names] so we can flag aliased implementations
    names_by_index: dict[int, list[str]] = {}
    for e in func_exports:
        names_by_index.setdefault(e["index"], []).append(e["name"])
    index_by_name = {e["name"]: e["index"] for e in func_exports}

    # -- attribute functions to their #[contract] type ------------------ #
    export_names = {e["name"] for e in func_exports}
    func_names = parse_name_section(customs["name"]) if "name" in customs else {}
    if func_names:
        attribution, symbol_diagnostics = attribute_contracts(func_names, export_names)
    else:
        attribution, symbol_diagnostics = {}, {}

    # -- group by interface (inferred) ---------------------------------- #
    present_error_enums = {
        e["name"] for e in entries if e["kind"] == "udt_error_enum"
    }
    if catalog is None:
        interfaces, assignments = {}, []
    else:
        interfaces, assignments = classify_functions(functions, present_error_enums, catalog)
    interface_of = {a["name"]: a for a in assignments}

    referenced: set[str] = set()
    out_functions = []
    for fn in sorted(functions, key=lambda f: f["name"]):
        for node in [i["type"] for i in fn["inputs"]] + fn["outputs"]:
            collect_udt_names(node, referenced)
        idx = index_by_name.get(fn["name"])
        aliases = [n for n in names_by_index.get(idx, []) if n != fn["name"]] if idx is not None else []
        out_functions.append(
            {
                "name": fn["name"],
                "signature": signature(fn),
                "contract": attribution.get(fn["name"]),
                "contract_type": interface_of.get(fn["name"], {}).get("interface"),
                "contract_type_match": interface_of.get(fn["name"], {}).get("match"),
                "also_matches": interface_of.get(fn["name"], {}).get("also_matches") or None,
                "doc": fn["doc"] or None,
                "exported": fn["name"] in index_by_name,
                "func_index": idx,
                "aliases": aliases or None,
                "parameters": [
                    {
                        "name": i["name"],
                        "type": i["type"]["display"],
                        "type_detail": i["type"],
                        "doc": i["doc"] or None,
                    }
                    for i in fn["inputs"]
                ],
                "returns": (
                    {
                        "type": fn["outputs"][0]["display"],
                        "type_detail": fn["outputs"][0],
                    }
                    if fn["outputs"]
                    else {"type": "()", "type_detail": {"type": "void", "display": "void"}}
                ),
            }
        )

    # Pull in UDTs reachable from the exported signatures (transitively).
    if all_entries:
        included = dict(by_name)
    else:
        included = {}
        queue = list(referenced)
        while queue:
            name = queue.pop()
            if name in included or name not in by_name:
                continue
            entry = by_name[name]
            included[name] = entry
            nested: set[str] = set()
            for field in entry.get("fields", []):
                collect_udt_names(field["type"], nested)
            for case in entry.get("cases", []):
                for node in case.get("types", []):
                    collect_udt_names(node, nested)
            queue.extend(nested)

    spec_names = {f["name"] for f in functions}
    result = {
        "file": wasm_path.name,
        "bytes": len(data),
        "env_meta": parse_env_meta(customs.get("contractenvmetav0", b"")),
        "contract_meta": parse_contract_meta(customs.get("contractmetav0", b"")),
        "counts": {
            "wasm_exports": len(exports),
            "exported_functions": len(func_exports),
            "spec_functions": len(functions),
            "spec_entries": len(entries),
        },
        # Read from the wasm `name` section -- definitive when symbols survive.
        "contracts": group_by_contract(attribution, [f["name"] for f in functions]),
        "symbol_info": {
            "name_section_present": bool(func_names),
            **symbol_diagnostics,
        },
        # Inferred, not read from the binary -- see the module docstring.
        "contract_types": interfaces,
        "functions": out_functions,
        "types": sorted(included.values(), key=lambda e: (e["kind"], e["name"])),
        "error_enums": [
            {
                "name": e["name"],
                "cases": {c["name"]: c["value"] for c in e["cases"]},
            }
            for e in entries
            if e["kind"] == "udt_error_enum"
        ],
        "wasm_exports": exports,
        "warnings": [],
    }

    if all_entries:
        result["events"] = [e for e in entries if e["kind"] == "event"]

    undocumented = sorted(e["name"] for e in func_exports if e["name"] not in spec_names)
    if undocumented:
        result["warnings"].append(
            f"exported but absent from the contract spec: {', '.join(undocumented)}"
        )
    missing = sorted(n for n in spec_names if n not in index_by_name)
    if missing:
        result["warnings"].append(
            f"in the spec but not exported from the wasm: {', '.join(missing)}"
        )
    aliased = {
        idx: names for idx, names in names_by_index.items() if len(names) > 1
    }
    for idx, names in sorted(aliased.items()):
        result["warnings"].append(
            f"func index {idx} is exported under multiple names: {', '.join(sorted(names))}"
        )
    if trailing:
        result["warnings"].append(f"{trailing} trailing bytes in contractspecv0 were not parsed")

    # -- contract-attribution warnings ---------------------------------- #
    if not func_names:
        result["warnings"].append(
            "no `name` custom section: contract attribution unavailable. This "
            "wasm was built with symbols stripped, and contractspecv0 records "
            "no impl association -- rebuild with `strip = \"none\"` (or a debug "
            "build) to recover the #[contract] type per function"
        )
    else:
        unattributed = sorted(export_names - set(attribution))
        if unattributed:
            result["warnings"].append(
                "exported but no #[contractimpl] wrapper found in the symbols: "
                + ", ".join(unattributed)
            )
        unresolved = sorted(n for n, r in attribution.items() if not r["impl_symbol"])
        if unresolved:
            result["warnings"].append(
                "contract type known but implementing method not located for: "
                + ", ".join(unresolved)
            )
        # Cross-check the hard grouping against the inferred one.
        for fn_name, record in sorted(attribution.items()):
            trait = record.get("trait")
            inferred = interface_of.get(fn_name, {}).get("interface")
            if not trait or not inferred or inferred == "custom":
                continue
            trait_leaf = trait.split("::")[-1].lower()
            stem = inferred.split("_")[-1]
            if stem and stem not in trait_leaf and trait_leaf not in inferred:
                result["warnings"].append(
                    f"`{fn_name}` is inferred as `{inferred}` but its symbol says "
                    f"it comes from `{trait}`; trust the symbol"
                )

    # Surface groupings that rest on a name collision rather than real evidence,
    # and functions that plausibly belong to more than one interface.
    for iface_name, iface in interfaces.items():
        if iface.get("confidence") == "low":
            result["warnings"].append(
                f"contract type `{iface_name}` inferred from a single generic "
                f"name match ({', '.join(iface['functions'])}); it may not "
                "actually be implemented"
            )
    contested = sorted(
        f"{a['name']} ({a['interface']} vs {', '.join(a['also_matches'])})"
        for a in assignments
        if a.get("also_matches")
    )
    if contested:
        result["warnings"].append(
            "functions matching more than one contract type: " + "; ".join(contested)
        )
    mismatched = sorted(
        a["name"] for a in assignments if a.get("match") == "name_only"
    )
    if mismatched:
        result["warnings"].append(
            "grouped by name but with a signature the catalog does not list: "
            + ", ".join(mismatched)
        )

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wasm", type=Path, help="path to the contract .wasm")
    ap.add_argument("-o", "--output", type=Path, help="output JSON path (default: <wasm>.spec.json)")
    ap.add_argument(
        "--all-entries",
        action="store_true",
        help="include every UDT and event in the spec, not just types reachable from exports",
    )
    ap.add_argument("--print", dest="do_print", action="store_true", help="print signatures to stdout")
    ap.add_argument(
        "--catalog",
        type=Path,
        help="JSON file of extra/overriding interface definitions to merge into "
        "the built-in catalog",
    )
    ap.add_argument(
        "--no-grouping",
        action="store_true",
        help="skip contract-type inference (emit an empty `contract_types`)",
    )
    ap.add_argument(
        "--dump-catalog",
        action="store_true",
        help="print the built-in interface catalog as JSON and exit",
    )
    args = ap.parse_args()

    if args.dump_catalog:
        print(json.dumps(INTERFACE_CATALOG, indent=2))
        return 0

    if not args.wasm.is_file():
        print(f"error: no such file: {args.wasm}", file=sys.stderr)
        return 1

    try:
        catalog = None if args.no_grouping else load_catalog(args.catalog)
        result = describe(args.wasm, all_entries=args.all_entries, catalog=catalog)
    except (WasmError, CatalogError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read catalog {args.catalog}: {exc}", file=sys.stderr)
        return 1

    out_path = args.output or args.wasm.with_suffix(".spec.json")
    out_path.write_text(json.dumps(result, indent=2) + "\n")

    if args.do_print:
        by_name = {fn["name"]: fn for fn in result["functions"]}
        if result["contracts"]:
            for contract, entry in sorted(result["contracts"].items()):
                where = "::".join(x for x in (entry["crate"], entry["module"]) if x)
                print(f"\n#[contract] {contract}   ({where})")
                for trait, members in sorted(entry["by_trait"].items()):
                    print(f"  impl {trait} for {contract}")
                    for name in members:
                        print(f"      {by_name[name]['signature']}")
                if entry["inherent"]:
                    print(f"  impl {contract}")
                    for name in entry["inherent"]:
                        print(f"      {by_name[name]['signature']}")
            print("\n--- inferred interface grouping (heuristic) ---")

        grouped: dict[str, list[dict]] = {}
        for fn in result["functions"]:
            grouped.setdefault(fn["contract_type"] or "ungrouped", []).append(fn)
        # Catalog order, with custom/ungrouped last.
        order = list(result["contract_types"]) or list(grouped)
        for name in order:
            if name not in grouped:
                continue
            iface = result["contract_types"].get(name, {})
            header = name
            if iface.get("confidence") and iface["confidence"] != "n/a":
                coverage = iface.get("coverage", {})
                header += (
                    f"  [confidence: {iface['confidence']}"
                    f", {coverage.get('implemented')}/{coverage.get('declared')}"
                    f"{' complete' if coverage.get('complete') else ''}]"
                )
            print(f"\n== {header} ==")
            for fn in grouped[name]:
                flag = " (signature mismatch)" if fn["contract_type_match"] == "name_only" else ""
                print(f"  {fn['signature']}{flag}")
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)

    contracts = result["contracts"]
    attributed = sum(len(c["functions"]) for c in contracts.values())
    print(
        f"{out_path}: {result['counts']['spec_functions']} functions; "
        f"{attributed} attributed to {len(contracts)} #[contract] type(s) "
        f"{sorted(contracts) if contracts else '(no symbols)'}; "
        f"{len(result['contract_types'])} inferred interfaces, "
        f"{len(result['types'])} types, {len(result['error_enums'])} error enums",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
