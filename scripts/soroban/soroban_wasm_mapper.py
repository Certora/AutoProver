#!/usr/bin/env python3
"""
soroban_wasm_mapper.py
======================
Maps functions in a compiled Soroban WASM to their origin:
  - #[contract] struct name
  - #[contractimpl] or #[contracttrait] annotation
  - source file + line
  - public contract spec (from contractspecv0 section)
 
Works by combining three sources of truth embedded in the WASM:
  1. contractspecv0 custom section  – public interface (XDR-encoded)
  2. WASM export section            – exported function names
  3. DWARF debug sections           – trait/impl/source provenance
     (requires llvm-dwarfdump on PATH)
 
Usage:
  python3 soroban_wasm_mapper.py <file.wasm> [--json] [--verbose]
"""
 
import argparse
import json
import re
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 1.  WASM binary helpers
# ─────────────────────────────────────────────────────────────────────────────
 
def read_leb128(data: bytes, offset: int):
    v = shift = 0
    while True:
        b = data[offset]; offset += 1
        v |= (b & 0x7F) << shift; shift += 7
        if not (b & 0x80):
            break
    return v, offset
 
 
def wasm_sections(data: bytes):
    """Yield (section_id, section_bytes) for every section in the WASM."""
    offset = 8  # skip magic + version
    while offset < len(data):
        sid = data[offset]; offset += 1
        length, offset = read_leb128(data, offset)
        yield sid, data[offset:offset + length]
        offset += length
 
 
def get_custom_section(data: bytes, name_target: str) -> bytes | None:
    for sid, payload in wasm_sections(data):
        if sid != 0:
            continue
        name_len, noff = read_leb128(payload, 0)
        name = payload[noff:noff + name_len].decode("utf-8", errors="replace")
        if name == name_target:
            return payload[noff + name_len:]
    return None
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 2.  WASM export section
# ─────────────────────────────────────────────────────────────────────────────
 
def parse_exports(data: bytes) -> dict[int, str]:
    """Return {func_index: export_name} for function exports."""
    exports = {}
    for sid, payload in wasm_sections(data):
        if sid != 7:
            continue
        count, pos = read_leb128(payload, 0)
        for _ in range(count):
            nlen, pos = read_leb128(payload, pos)
            name = payload[pos:pos + nlen].decode("utf-8", errors="replace")
            pos += nlen
            kind = payload[pos]; pos += 1
            idx, pos = read_leb128(payload, pos)
            if kind == 0:   # function export
                exports[idx] = name
    return exports
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 3.  WASM name section  (maps function index → internal name)
# ─────────────────────────────────────────────────────────────────────────────
 
def parse_name_section(data: bytes) -> dict[int, str]:
    """Return {func_index: debug_name} from the 'name' custom section."""
    payload = get_custom_section(data, "name")
    if payload is None:
        return {}
    names: dict[int, str] = {}
    pos = 0
    while pos < len(payload):
        subsection_id = payload[pos]; pos += 1
        sub_len, pos = read_leb128(payload, pos)
        end = pos + sub_len
        if subsection_id == 1:   # function names subsection
            count, pos = read_leb128(payload, pos)
            for _ in range(count):
                idx, pos = read_leb128(payload, pos)
                nlen, pos = read_leb128(payload, pos)
                name = payload[pos:pos + nlen].decode("utf-8", errors="replace")
                pos += nlen
                names[idx] = name
        pos = end
    return names
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 4.  contractspecv0  (XDR-encoded public interface)
# ─────────────────────────────────────────────────────────────────────────────
 
SPEC_TYPES = {
    0: "Val", 1: "bool", 2: "void", 3: "error",
    4: "u32", 5: "i32", 6: "u64", 7: "i64",
    8: "timepoint", 9: "duration", 10: "u128", 11: "i128",
    12: "u256", 13: "i256", 14: "bytes", 16: "string",
    17: "symbol", 19: "Address",
    1000: "Option", 1001: "Result", 1002: "Vec",
    1004: "Map", 1005: "Tuple", 1006: "BytesN",
    2000: "UDT",
}
 
 
class SpecParser:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.pos = 0
 
    def u32(self):
        v = struct.unpack(">I", self.raw[self.pos:self.pos + 4])[0]
        self.pos += 4
        return v
 
    def string(self):
        length = self.u32()
        s = self.raw[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        self.pos += (4 - length % 4) % 4   # XDR 4-byte alignment padding
        return s
 
    def type_def(self) -> str:
        kind = self.u32()
        name = SPEC_TYPES.get(kind, f"?{kind}")
        if kind == 1000:    # Option<T>
            return f"Option<{self.type_def()}>"
        if kind == 1001:    # Result<T, E>
            ok = self.type_def(); err = self.type_def()
            return f"Result<{ok},{err}>"
        if kind == 1002:    # Vec<T>
            return f"Vec<{self.type_def()}>"
        if kind == 1004:    # Map<K,V>
            k = self.type_def(); v = self.type_def()
            return f"Map<{k},{v}>"
        if kind == 1005:    # Tuple
            count = self.u32()
            items = [self.type_def() for _ in range(count)]
            return f"({', '.join(items)})"
        if kind == 1006:    # BytesN
            n = self.u32(); return f"BytesN<{n}>"
        if kind == 2000:    # UDT – named type
            return self.string()
        return name
 
    def parse_entries(self) -> list[dict]:
        entries = []
        while self.pos < len(self.raw):
            kind = self.u32()
            if kind == 0:       # FunctionV0
                doc    = self.string()
                name   = self.string()
                n_inp  = self.u32()
                inputs = []
                for _ in range(n_inp):
                    _doc  = self.string()
                    iname = self.string()
                    itype = self.type_def()
                    inputs.append({"name": iname, "type": itype})
                n_out   = self.u32()
                outputs = [self.type_def() for _ in range(n_out)]
                entries.append({
                    "kind": "function",
                    "name": name,
                    "doc":  doc.strip(),
                    "inputs":  inputs,
                    "outputs": outputs,
                })
            elif kind == 1:     # UDTStructV0
                doc  = self.string()
                lib  = self.string()
                name = self.string()
                n    = self.u32()
                fields = []
                for _ in range(n):
                    fdoc  = self.string()
                    fname = self.string()
                    ftype = self.type_def()
                    fields.append({"name": fname, "type": ftype})
                entries.append({"kind": "struct", "name": name, "lib": lib,
                                "doc": doc.strip(), "fields": fields})
            elif kind == 2:     # UDTUnionV0
                doc  = self.string()
                lib  = self.string()
                name = self.string()
                n    = self.u32()
                cases = []
                for _ in range(n):
                    case_kind = self.u32()
                    cdoc  = self.string()
                    cname = self.string()
                    if case_kind == 1:      # TupleCase
                        nt = self.u32()
                        types = [self.type_def() for _ in range(nt)]
                        cases.append({"name": cname, "types": types})
                    else:                   # VoidCase
                        cases.append({"name": cname})
                entries.append({"kind": "union", "name": name, "lib": lib,
                                "doc": doc.strip(), "cases": cases})
            elif kind == 3:     # UDTEnumV0
                doc  = self.string()
                lib  = self.string()
                name = self.string()
                n    = self.u32()
                cases = []
                for _ in range(n):
                    cdoc   = self.string()
                    cname  = self.string()
                    cvalue = self.u32()
                    cases.append({"name": cname, "value": cvalue})
                entries.append({"kind": "enum", "name": name, "lib": lib,
                                "doc": doc.strip(), "cases": cases})
            elif kind == 4:     # UDTErrorEnumV0
                doc  = self.string()
                lib  = self.string()
                name = self.string()
                n    = self.u32()
                cases = []
                for _ in range(n):
                    cdoc   = self.string()
                    cname  = self.string()
                    cvalue = self.u32()
                    cases.append({"name": cname, "value": cvalue})
                entries.append({"kind": "error_enum", "name": name, "lib": lib,
                                "doc": doc.strip(), "cases": cases})
            else:
                # Unknown entry kind – stop; we may have trailing data
                break
        return entries
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 5.  DWARF parsing via llvm-dwarfdump
# ─────────────────────────────────────────────────────────────────────────────
 
# Patterns in __GovernorContract__<fn>__spec namespace names
_SPEC_NS_RE = re.compile(r"^__([A-Za-z0-9_]+)__([a-z0-9_]+)__spec$")
 
# DW_AT_name for trait-impl subprograms:  fn_name<crate::path::Contract>
_TRAIT_FN_RE = re.compile(r"^([a-z_][a-z0-9_]*)<([^>]+)>$")
 
# Last component of a :: path
def _last(path: str) -> str:
    return path.rsplit("::", 1)[-1]
 
 
def parse_dwarf(wasm_path: str) -> dict:
    """
    Run llvm-dwarfdump and extract per-function provenance:
 
      contractimpl_fns  : {fn_name: {contract, source_file, source_line}}
      contracttrait_fns : {fn_name: {contract, trait, full_trait, source_file, source_line}}
      struct_method_fns : {fn_name: {contract, source_file, source_line}}
                          (inherent impl / constructor)
 
    Strategy
    ────────
    1.  Spec-namespace pass  – scan for `__<Contract>__<fn>__spec` namespace
        names that the #[contractimpl] macro emits.  Gives us {fn → contract}
        + source location.
 
    2.  Subprogram pass  – collect every DW_TAG_subprogram in a single linear
        scan, recording:
          • display name (DW_AT_name)
          • linkage name
          • decl_file / decl_line
          • the nearest enclosing namespace path (for trait impl)
          • the nearest enclosing structure_type name (for inherent methods)
 
        Functions whose display name matches  `fn_name<...StructName>`  come
        from trait-impl blocks; the enclosing namespace gives the trait name.
        Functions whose linkage name encodes a struct context (like constructor)
        are captured from the structure_type name.
    """
    try:
        result = subprocess.run(
            ["llvm-dwarfdump", wasm_path, "--debug-info"],
            capture_output=True, text=True, timeout=120,
        )
        lines = result.stdout.splitlines()
    except FileNotFoundError:
        print("WARNING: llvm-dwarfdump not found – skipping DWARF analysis.",
              file=sys.stderr)
        return {}
 
    contractimpl_fns:  dict[str, dict] = {}
    contracttrait_fns: dict[str, dict] = {}
    struct_method_fns: dict[str, dict] = {}
 
    # ── Pass 1: spec namespaces ───────────────────────────────────────────────
    in_spec   = False
    spec_fn   = None
    spec_cont = None
    for line in lines:
        stripped = line.lstrip()
        # Any new DW_TAG (other than attributes/NULL) resets spec context
        if re.match(r'DW_TAG_', stripped):
            in_spec = False; spec_fn = None; spec_cont = None
 
        m = re.search(r'DW_AT_name\s+\("([^"]+)"\)', stripped)
        if m:
            sm = _SPEC_NS_RE.match(m.group(1))
            if sm:
                in_spec = True
                spec_cont = sm.group(1)
                spec_fn   = sm.group(2)
                contractimpl_fns.setdefault(spec_fn, {
                    "contract":    spec_cont,
                    "annotation":  "#[contractimpl]",
                    "source_file": None,
                    "source_line": None,
                })
 
        if in_spec and spec_fn:
            m = re.search(r'DW_AT_decl_file\s+\("([^"]+)"\)', stripped)
            if m and contractimpl_fns[spec_fn]["source_file"] is None:
                contractimpl_fns[spec_fn]["source_file"] = m.group(1)
            m = re.search(r'DW_AT_decl_line\s+\((\d+)\)', stripped)
            if m and contractimpl_fns[spec_fn]["source_line"] is None:
                contractimpl_fns[spec_fn]["source_line"] = int(m.group(1))
 
    # ── Pass 2: subprograms – linear scan with lightweight context ────────────
    # We track:
    #   ns_path       – stack of (indent, name) for DW_TAG_namespace
    #   struct_ctx    – (indent, name) of most-recent DW_TAG_structure_type
    #   current_sp    – attributes of the open DW_TAG_subprogram
    #
    # INDENT NOTE
    # -----------
    # llvm-dwarfdump prefixes every DIE header line with its DWARF offset,
    # e.g. "0x000001de:       DW_TAG_namespace".  The nesting depth is the
    # space count AFTER the "0x....: " prefix, NOT the leading spaces of the
    # whole line (which would be 0 for all DIE headers).
    # Attribute lines ("                DW_AT_name ...") have no prefix and
    # are indented with plain leading spaces; they don't need indent tracking
    # because they always belong to the most-recently-opened DIE.
 
    _DIE_PREFIX_RE = re.compile(r'^0x[0-9a-f]+:\s*')
 
    def die_indent(line: str) -> int | None:
        """Return nesting indent if line is a DIE header (0x...: ...), else None."""
        m = _DIE_PREFIX_RE.match(line)
        if m:
            rest = line[m.end():]
            return len(line[m.end() - (m.end() - len(line[:m.end()].rstrip())):]) - \
                   len(line[m.end() - (m.end() - len(line[:m.end()].rstrip())):].lstrip())
        return None
 
    # Simpler: just count spaces between "0x...: " end and the first non-space
    def die_indent(line: str) -> int | None:  # noqa: F811
        if not line.startswith("0x"):
            return None
        colon = line.find(":")
        if colon < 0:
            return None
        rest = line[colon + 1:]
        return len(rest) - len(rest.lstrip())
 
    ns_path:   list[tuple[int, str]] = []
    struct_ctx: tuple[int, str] | None = None
    current_sp: dict | None = None
 
    def pop_to_indent(indent: int):
        nonlocal struct_ctx
        while ns_path and ns_path[-1][0] >= indent:
            ns_path.pop()
        if struct_ctx and struct_ctx[0] >= indent:
            struct_ctx = None
 
    def current_ns() -> str:
        return "::".join(name for _, name in ns_path if name != "__PENDING__")
 
    for line in lines:
        stripped = line.lstrip()
        indent   = die_indent(line)   # None for attribute lines
 
        if indent is not None and "DW_TAG_namespace" in stripped:
            pop_to_indent(indent)
            ns_path.append((indent, "__PENDING__"))
            if current_sp is not None:
                _flush_sp(current_sp, current_ns(), struct_ctx,
                          contracttrait_fns, struct_method_fns)
                current_sp = None
            continue
 
        if indent is not None and "DW_TAG_structure_type" in stripped:
            pop_to_indent(indent)
            struct_ctx = (indent, "__PENDING__")
            if current_sp is not None:
                _flush_sp(current_sp, current_ns(), struct_ctx,
                          contracttrait_fns, struct_method_fns)
                current_sp = None
            continue
 
        if indent is not None and "DW_TAG_subprogram" in stripped:
            if current_sp is not None:
                _flush_sp(current_sp, current_ns(), struct_ctx,
                          contracttrait_fns, struct_method_fns)
            current_sp = {"_indent": indent}
            continue
 
        # Attribute lines (indent is None) ────────────────────────────────────
 
        # DW_AT_name
        m = re.search(r'DW_AT_name\s+\("([^"]+)"\)', stripped)
        if m:
            val = m.group(1)
            # Fill pending namespace or structure_type name
            if ns_path and ns_path[-1][1] == "__PENDING__":
                ns_path[-1] = (ns_path[-1][0], val)
            elif struct_ctx and struct_ctx[1] == "__PENDING__":
                struct_ctx = (struct_ctx[0], val)
            elif current_sp is not None and "display_name" not in current_sp:
                current_sp["display_name"] = val
            continue
 
        # DW_AT_linkage_name
        m = re.search(r'DW_AT_linkage_name\s+\("([^"]+)"\)', stripped)
        if m and current_sp is not None:
            current_sp.setdefault("linkage", m.group(1))
            continue
 
        # DW_AT_decl_file
        m = re.search(r'DW_AT_decl_file\s+\("([^"]+)"\)', stripped)
        if m and current_sp is not None:
            current_sp.setdefault("source_file", m.group(1))
            continue
 
        # DW_AT_decl_line
        m = re.search(r'DW_AT_decl_line\s+\((\d+)\)', stripped)
        if m and current_sp is not None:
            current_sp.setdefault("source_line", int(m.group(1)))
            continue
 
        # NULL closes the current DIE  (it IS a DIE header, so indent is not None)
        if indent is not None and "NULL" in line.split(":", 1)[-1]:
            if current_sp is not None:
                _flush_sp(current_sp, current_ns(), struct_ctx,
                          contracttrait_fns, struct_method_fns)
                current_sp = None
            pop_to_indent(indent)
 
    if current_sp:
        _flush_sp(current_sp, current_ns(), struct_ctx,
                  contracttrait_fns, struct_method_fns)
 
    return {
        "contractimpl":   contractimpl_fns,
        "contracttrait":  contracttrait_fns,
        "struct_methods": struct_method_fns,
    }
 
 
def _flush_sp(sp: dict, ns: str, struct_ctx,
              contracttrait_fns: dict, struct_method_fns: dict):
    """Process a completed DW_TAG_subprogram entry."""
    display = sp.get("display_name", "")
    tm = _TRAIT_FN_RE.match(display)
 
    if tm:
        fn_name   = tm.group(1)
        impl_path = tm.group(2)          # e.g. fungible_governor_contract::governor::GovernorContract
        contract  = _last(impl_path)
        trait     = _last(ns) if ns else None
 
        # Only record functions on a plausible contract struct
        if contract and trait and not contract[0].islower():
            contracttrait_fns.setdefault(fn_name, {
                "contract":    contract,
                "trait":       trait,
                "full_trait":  ns,
                "annotation":  "#[contractimpl] of #[contracttrait]",
                "source_file": sp.get("source_file"),
                "source_line": sp.get("source_line"),
            })
 
    elif display and struct_ctx and struct_ctx[1] not in ("__PENDING__", None):
        # Inherent method inside a structure_type – e.g. constructor
        struct_name = struct_ctx[1]
        fn_name     = display
        # Allow __constructor and similar; skip purely internal single-underscore names
        if not (fn_name.startswith("_") and not fn_name.startswith("__")):
            struct_method_fns.setdefault(fn_name, {
                "contract":    struct_name,
                "annotation":  "#[contractimpl]",
                "source_file": sp.get("source_file"),
                "source_line": sp.get("source_line"),
            })
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 6.  contractmetav0 / contractenvmetav0
# ─────────────────────────────────────────────────────────────────────────────
 
def parse_contract_meta(data: bytes) -> dict:
    meta = {}
    payload = get_custom_section(data, "contractmetav0")
    if payload:
        pos = 0
        try:
            count = struct.unpack(">I", payload[pos:pos+4])[0]; pos += 4
            for _ in range(count):
                klen = struct.unpack(">I", payload[pos:pos+4])[0]; pos += 4
                k    = payload[pos:pos+klen].decode(); pos += klen
                pos += (4 - klen % 4) % 4
                vlen = struct.unpack(">I", payload[pos:pos+4])[0]; pos += 4
                v    = payload[pos:pos+vlen].decode(); pos += vlen
                pos += (4 - vlen % 4) % 4
                meta[k] = v
        except Exception:
            pass
 
    # fallback: interpret as concatenated string pairs (SCMetaKindText entries)
    if not meta:
        raw = get_custom_section(data, "contractmetav0") or b""
        # Each SCMetaEntry = u32 kind=0 (Text) + u32 key_len + key + pad + u32 val_len + val + pad
        pos = 0
        try:
            while pos < len(raw):
                kind = struct.unpack(">I", raw[pos:pos+4])[0]; pos += 4
                if kind != 0:
                    break
                klen = struct.unpack(">I", raw[pos:pos+4])[0]; pos += 4
                k    = raw[pos:pos+klen].decode(); pos += klen
                pos += (4 - klen % 4) % 4
                vlen = struct.unpack(">I", raw[pos:pos+4])[0]; pos += 4
                v    = raw[pos:pos+vlen].decode(); pos += vlen
                pos += (4 - vlen % 4) % 4
                meta[k] = v
        except Exception:
            pass
    return meta
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 7.  Main: combine everything
# ─────────────────────────────────────────────────────────────────────────────
 
def analyze(wasm_path: str, verbose: bool = False) -> dict:
    data = Path(wasm_path).read_bytes()
 
    # Contract metadata
    contract_meta = parse_contract_meta(data)
 
    # Public spec
    spec_raw  = get_custom_section(data, "contractspecv0") or b""
    spec_entries = SpecParser(spec_raw).parse_entries() if spec_raw else []
    spec_fns  = {e["name"]: e for e in spec_entries if e["kind"] == "function"}
    spec_udts = [e for e in spec_entries if e["kind"] != "function"]
 
    # Exports
    exports = parse_exports(data)          # {idx: name}
 
    # Internal function names
    int_names = parse_name_section(data)   # {idx: mangled_name}
 
    # DWARF
    dwarf  = parse_dwarf(wasm_path)
    ci_fns = dwarf.get("contractimpl",   {})   # from #[contractimpl] spec namespaces
    ct_fns = dwarf.get("contracttrait",  {})   # from trait-impl subprograms
    sm_fns = dwarf.get("struct_methods", {})   # inherent methods (constructor etc.)
 
    # ── Build per-function records ───────────────────────────────────────────
    all_fns: dict[str, dict] = {}
 
    for fn_name in sorted(set(list(spec_fns) + list(exports.values()))):
        if fn_name in ("memory", "_"):
            continue
        rec = {
            "name":       fn_name,
            "exported":   fn_name in exports.values(),
            "spec":       spec_fns.get(fn_name),
            "contract":   None,
            "annotation": None,
            "trait":      None,
            "full_trait": None,
            "source_file": None,
            "source_line": None,
        }
 
        if fn_name in ci_fns:
            d = ci_fns[fn_name]
            # Check if it's also a trait-impl (both annotations apply)
            ct = ct_fns.get(fn_name, {})
            rec.update({
                "contract":    d["contract"],
                "annotation":  "#[contractimpl] of #[contracttrait]" if ct else "#[contractimpl]",
                "trait":       ct.get("trait"),
                "full_trait":  ct.get("full_trait"),
                "source_file": d.get("source_file") or ct.get("source_file"),
                "source_line": d.get("source_line") or ct.get("source_line"),
            })
        elif fn_name in ct_fns:
            d = ct_fns[fn_name]
            rec.update({
                "contract":    d["contract"],
                "annotation":  "#[contractimpl] of #[contracttrait]",
                "trait":       d.get("trait"),
                "full_trait":  d.get("full_trait"),
                "source_file": d.get("source_file"),
                "source_line": d.get("source_line"),
            })
        elif fn_name in sm_fns:
            # Only use struct-method info when the function is actually
            # exported or in the public spec (avoids picking up client stubs)
            d = sm_fns[fn_name]
            if d["contract"] not in ("Val", "Some") and \
               not d["contract"].endswith("Client"):
                rec.update({
                    "contract":    d["contract"],
                    "annotation":  "#[contractimpl]",
                    "source_file": d.get("source_file"),
                    "source_line": d.get("source_line"),
                })
 
        all_fns[fn_name] = rec
 
    return {
        "wasm_path":      wasm_path,
        "contract_meta":  contract_meta,
        "spec_udts":      spec_udts,
        "functions":      all_fns,
    }
 
 
# ─────────────────────────────────────────────────────────────────────────────
# 8.  Pretty-print output
# ─────────────────────────────────────────────────────────────────────────────
 
def _rel_path(path: str | None) -> str:
    if not path:
        return "?"
    # Strip everything up to and including the repo root patterns
    for marker in ("src/", "packages/", "examples/"):
        idx = path.find(marker)
        if idx != -1:
            return "…/" + path[idx:]
    return path.split("/")[-1]
 
 
def pretty_print(result: dict, verbose: bool = False):
    meta = result["contract_meta"]
    fns  = result["functions"]
 
    print("=" * 70)
    print(f"  Soroban WASM contract function map")
    print(f"  File: {result['wasm_path']}")
    if meta:
        for k, v in meta.items():
            print(f"  {k}: {v}")
    print("=" * 70)
 
    # Group by (contract, annotation, trait)
    groups: dict[tuple, list] = defaultdict(list)
    for fn in fns.values():
        key = (
            fn.get("contract") or "(unknown contract)",
            fn.get("annotation") or "(unknown annotation)",
            fn.get("trait"),
            fn.get("full_trait"),
        )
        groups[key].append(fn)
 
    for (contract, annotation, trait, full_trait), fn_list in sorted(groups.items()):
        print()
        label = f"#[contract] struct {contract}"
        print(f"  ┌─ {label}")
        if trait:
            print(f"  │  implements trait {trait}")
            if full_trait and verbose:
                print(f"  │    (full path: {full_trait})")
        print(f"  │  annotation: {annotation}")
        print(f"  │")
 
        # Sort: constructor first, then alphabetical
        fn_list = sorted(fn_list, key=lambda f: (f["name"] != "__constructor", f["name"]))
        for fn in fn_list:
            spec = fn.get("spec") or {}
            inputs = spec.get("inputs", [])
            outputs = spec.get("outputs", [])
            sig_in  = ", ".join(f'{i["name"]}: {i["type"]}' for i in inputs)
            sig_out = " -> " + ", ".join(outputs) if outputs else ""
            src = _rel_path(fn.get("source_file"))
            line = fn.get("source_line")
            loc  = f"{src}:{line}" if line else src
 
            print(f"  │  fn {fn['name']}({sig_in}){sig_out}")
            if verbose and spec.get("doc"):
                print(f"  │      // {spec['doc'][:80]}")
            print(f"  │      @ {loc}")
        print(f"  └─")
 
    # UDTs from spec
    udts = result.get("spec_udts", [])
    if udts:
        print()
        print("  ── Contract types (from contractspecv0) ─────────────────────")
        for udt in udts:
            kind = udt["kind"]
            print(f"  {kind}  {udt['name']}", end="")
            if udt.get("lib"):
                print(f"  [lib: {udt['lib']}]", end="")
            print()
            if verbose and udt.get("doc"):
                print(f"    // {udt['doc'][:80]}")
            if kind == "struct":
                for f in udt.get("fields", []):
                    print(f"    {f['name']}: {f['type']}")
            elif kind in ("enum", "error_enum"):
                for c in udt.get("cases", []):
                    print(f"    {c['name']} = {c['value']}")
            elif kind == "union":
                for c in udt.get("cases", []):
                    types = c.get("types", [])
                    print(f"    {c['name']}" + (f"({', '.join(types)})" if types else ""))
 
    print()
 
 
# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
 
def main():
    parser = argparse.ArgumentParser(
        description="Map Soroban WASM functions to their contract/trait origin."
    )
    parser.add_argument("wasm", help="Path to the .wasm file")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of the human-readable report")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Include doc-comments and full trait paths")
    args = parser.parse_args()
 
    result = analyze(args.wasm, verbose=args.verbose)
 
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        pretty_print(result, verbose=args.verbose)
 
 
if __name__ == "__main__":
    main()
