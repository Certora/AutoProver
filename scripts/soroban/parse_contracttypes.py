#!/usr/bin/env python3
"""
Parse all Rust source files under a Soroban contract repo and extract
every type annotated with #[contracttype], producing a JSON description
of each type's kind (struct / enum / tuple-struct) and its fields or variants.
 
Each field carries a "recursive" flag that is True when the field's type,
or the type of any transitive child field, is the same type that contains
the field (i.e. the type is directly or indirectly self-referential through
that field).
"""
 
import json
import re
import sys
from pathlib import Path
from util import *

# ---------------------------------------------------------------------------
# Tokeniser helpers
# ---------------------------------------------------------------------------
 
def strip_comments(src: str) -> str:
    """Remove // line comments and /* … */ block comments."""
    # block comments (non-greedy)
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
    # line comments
    src = re.sub(r'//[^\n]*', '', src)
    return src
 
 
def find_matching_brace(s: str, start: int) -> int:
    """Return the index of the closing '}' that matches the '{' at *start*."""
    depth = 0
    i = start
    while i < len(s):
        if s[i] == '{':
            depth += 1
        elif s[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1
 
 
def find_matching_paren(s: str, start: int) -> int:
    """Return the index of the closing ')' that matches the '(' at *start*."""
    depth = 0
    i = start
    while i < len(s):
        if s[i] == '(':
            depth += 1
        elif s[i] == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1
 
 
# ---------------------------------------------------------------------------
# Type-string cleanup
# ---------------------------------------------------------------------------
 
def clean_type(raw: str) -> str:
    raw = raw.strip().rstrip(',').strip()
    # collapse internal whitespace
    raw = re.sub(r'\s+', ' ', raw)
    return raw
 
 
# ---------------------------------------------------------------------------
# Struct / enum body parsers
# ---------------------------------------------------------------------------
 
def parse_struct_fields(body: str) -> list[dict]:
    """Parse named fields of a struct body (inside { … })."""
    fields = []
    # Each field optionally has attributes and pub keyword.
    # Pattern: optional pub/pub(…) then  name: Type,
    # We iterate line-by-line; a type may span multiple lines for generics.
    body = strip_comments(body)
 
    # Strip outer braces if present
    body = body.strip()
    if body.startswith('{'):
        body = body[1:]
    if body.endswith('}'):
        body = body[:-1]
 
    # Remove attribute lines (#[...])
    body = re.sub(r'#\s*\[.*?\]', '', body, flags=re.DOTALL)
 
    # Split on field separators: "name: Type," – we need to handle nested generics
    # Strategy: find "identifier: " pattern, then grab until next "identifier: " or end
    # We'll scan token by token tracking depth
    tokens = []
    i = 0
    body = body.strip()
    while i < len(body):
        # skip whitespace
        if body[i].isspace():
            i += 1
            continue
        # skip pub visibility
        if body[i:].startswith('pub'):
            rest = body[i+3:]
            if rest and rest[0] == '(':
                # pub(crate) etc.
                close = body.index(')', i+3)
                i = close + 1
            else:
                i += 3
            continue
        # match  field_name  :
        m = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:', body[i:])
        if m:
            name = m.group(1)
            i += m.end()
            # now collect the type until the next top-level comma or end
            type_chars = []
            depth_angle = 0
            depth_paren = 0
            depth_brace = 0
            while i < len(body):
                c = body[i]
                if c == '<':
                    depth_angle += 1
                elif c == '>':
                    depth_angle -= 1
                elif c == '(':
                    depth_paren += 1
                elif c == ')':
                    depth_paren -= 1
                elif c == '{':
                    depth_brace += 1
                elif c == '}':
                    depth_brace -= 1
                elif c == ',' and depth_angle == 0 and depth_paren == 0 and depth_brace == 0:
                    i += 1
                    break
                type_chars.append(c)
                i += 1
            field_type = clean_type(''.join(type_chars))
            if name and field_type:
                fields.append({"name": name, "type": field_type})
        else:
            i += 1
 
    return fields
 
 
def parse_tuple_struct_fields(inner: str) -> list[dict]:
    """Parse positional fields of a tuple struct body (inside ( … ))."""
    inner = strip_comments(inner).strip()
    if inner.startswith('('):
        inner = inner[1:]
    if inner.endswith(')'):
        inner = inner[:-1]
    # Remove attribute annotations
    inner = re.sub(r'#\s*\[.*?\]', '', inner, flags=re.DOTALL)
    # Remove pub
    parts = []
    # split by top-level commas
    depth = 0
    current = []
    for c in inner:
        if c in '<(':
            depth += 1
        elif c in '>)':
            depth -= 1
        elif c == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
            continue
        current.append(c)
    if current:
        parts.append(''.join(current).strip())
 
    fields = []
    for idx, part in enumerate(parts):
        part = re.sub(r'\bpub\b(\s*\([^)]*\))?', '', part).strip()
        if part:
            fields.append({"index": idx, "type": clean_type(part)})
    return fields
 
 
def parse_enum_variants(body: str) -> list[dict]:
    """Parse variants of an enum body (inside { … })."""
    body = strip_comments(body).strip()
    if body.startswith('{'):
        body = body[1:]
    if body.endswith('}'):
        body = body[:-1]
 
    variants = []
    i = 0
    body = body.strip()
    while i < len(body):
        # skip whitespace
        if body[i].isspace():
            i += 1
            continue
        # skip attribute lines
        if body[i] == '#':
            depth = 0
            while i < len(body):
                if body[i] == '[':
                    depth += 1
                elif body[i] == ']':
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            continue
        # match variant name
        m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)', body[i:])
        if not m:
            i += 1
            continue
        vname = m.group(1)
        i += m.end()
        # skip whitespace
        while i < len(body) and body[i].isspace():
            i += 1
 
        if i >= len(body):
            variants.append({"variant": vname, "kind": "unit"})
            break
 
        if body[i] == '{':
            # struct variant
            close = find_matching_brace(body, i)
            inner = body[i:close+1]
            fields = parse_struct_fields(inner)
            variants.append({"variant": vname, "kind": "struct", "fields": fields})
            i = close + 1
        elif body[i] == '(':
            # tuple variant
            close = find_matching_paren(body, i)
            inner = body[i:close+1]
            fields = parse_tuple_struct_fields(inner)
            variants.append({"variant": vname, "kind": "tuple", "fields": fields})
            i = close + 1
        elif body[i] == ',':
            variants.append({"variant": vname, "kind": "unit"})
            i += 1
        elif body[i] == '=':
            # discriminant: Unit = N,
            j = i + 1
            while j < len(body) and body[j] != ',':
                j += 1
            disc = body[i+1:j].strip()
            variants.append({"variant": vname, "kind": "unit", "discriminant": disc})
            i = j + 1
        else:
            variants.append({"variant": vname, "kind": "unit"})
 
    return variants
 
 
# ---------------------------------------------------------------------------
# Recursive-field analysis
# ---------------------------------------------------------------------------
 
# Match every Rust identifier in a type string so we can extract candidate
# type names (e.g. "Vec<PathStep>" → ["Vec", "PathStep"]).
_IDENT_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
 
# Rust primitive / stdlib names that can never be a contracttype.
_RUST_BUILTINS = frozenset({
    "bool", "u8", "u16", "u32", "u64", "u128", "i8", "i16", "i32", "i64",
    "i128", "f32", "f64", "usize", "isize", "str", "String", "char",
    "Vec", "Option", "Result", "Box", "Rc", "Arc", "HashMap", "BTreeMap",
    "HashSet", "BTreeSet", "Bytes", "BytesN", "Map", "Set",
    "Address", "Symbol", "Val", "Env", "U256", "I256",
    "Duration", "Timepoint", "Error",
    # keywords that can appear after whitespace removal
    "pub", "crate", "super", "self", "Self", "mut", "ref",
})
 
 
def referenced_type_names(type_str: str) -> list[str]:
    """Return the unique identifiers in *type_str* that could be type names."""
    return [t for t in _IDENT_RE.findall(type_str) if t not in _RUST_BUILTINS]
 
 
def _field_type_strings(entry: dict) -> list[str]:
    """Return all field/variant-field type strings stored in *entry*."""
    types = []
    if entry["kind"] == "struct":
        for f in entry.get("fields", []):
            types.append(f["type"])
    else:  # enum
        for v in entry.get("variants", []):
            for f in v.get("fields", []):
                types.append(f["type"])
    return types
 
 
def is_recursive(type_str: str, target: str,
                 type_map: dict, visited: set) -> bool:
    """
    Return True if *target* appears in *type_str* or in the type of any
    transitive child field reachable from *type_str*.
 
    *visited* prevents infinite loops when the contracttype graph has cycles.
    """
    for name in referenced_type_names(type_str):
        if name == target:
            return True
        if name in type_map and name not in visited:
            visited.add(name)
            for child_type_str in _field_type_strings(type_map[name]):
                if is_recursive(child_type_str, target, type_map, visited):
                    return True
    return False
 
 
def annotate_recursive(all_types: list[dict]) -> None:
    """
    Mutate every field dict in *all_types* in-place, adding
    ``"recursive": true/false``.
    """
    # Build name → entry lookup (last definition wins for dedup safety)
    type_map = {entry["name"]: entry for entry in all_types}
 
    for entry in all_types:
        containing = entry["name"]
        if entry["kind"] == "struct":
            for field in entry.get("fields", []):
                field["recursive"] = is_recursive(
                    field["type"], containing, type_map, {containing}
                )
        else:  # enum
            for variant in entry.get("variants", []):
                for field in variant.get("fields", []):
                    field["recursive"] = is_recursive(
                        field["type"], containing, type_map, {containing}
                    )
 
 
# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------
 
CONTRACTTYPE_RE = re.compile(
    r'#\s*\[\s*contracttype\s*(?:\([^)]*\))?\s*\]'
)
 
 
def extract_from_source(src: str, filepath: str) -> list[dict]:
    """Return all contracttype definitions found in *src*."""
    src_stripped = strip_comments(src)
    results = []
 
    for m in CONTRACTTYPE_RE.finditer(src_stripped):
        attr_end = m.end()
        # skip whitespace after the attribute
        rest = src_stripped[attr_end:]
        rest_stripped = rest.lstrip()
        lead_ws = len(rest) - len(rest_stripped)
        pos = attr_end + lead_ws
 
        # remove any intermediate derives/other attributes between contracttype and the type def
        while pos < len(src_stripped) and src_stripped[pos] == '#':
            bracket_open = src_stripped.index('[', pos)
            bracket_close = src_stripped.index(']', bracket_open)
            pos = bracket_close + 1
            pos += len(src_stripped[pos:]) - len(src_stripped[pos:].lstrip())
 
        # expect: (pub)? struct|enum  Name
        head_match = re.match(
            r'(?:pub\s*(?:\([^)]*\)\s*)?)?'   # optional visibility
            r'(struct|enum)\s+'               # kind
            r'([A-Za-z_][A-Za-z0-9_]*)'       # name
            r'(?:\s*<[^{(;]*>)?'              # optional generics
            r'\s*',
            src_stripped[pos:]
        )
        if not head_match:
            continue
 
        kind = head_match.group(1)
        name = head_match.group(2)
        body_start = pos + head_match.end()
 
        entry: dict = {"name": name,
                       "kind": kind,
                       "file": filepath,
                       "use": src_path_to_module(filepath) + "::" + name
                    }
 
        if kind == "struct":
            if body_start < len(src_stripped) and src_stripped[body_start] == '(':
                # tuple struct
                close = find_matching_paren(src_stripped, body_start)
                inner = src_stripped[body_start:close+1]
                fields = parse_tuple_struct_fields(inner)
                entry["struct_kind"] = "tuple"
                entry["fields"] = fields
            elif body_start < len(src_stripped) and src_stripped[body_start] == '{':
                close = find_matching_brace(src_stripped, body_start)
                inner = src_stripped[body_start:close+1]
                fields = parse_struct_fields(inner)
                entry["struct_kind"] = "named"
                entry["fields"] = fields
            else:
                entry["struct_kind"] = "unit"
                entry["fields"] = []
        elif kind == "enum":
            close = find_matching_brace(src_stripped, body_start)
            inner = src_stripped[body_start:close+1]
            variants = parse_enum_variants(inner)
            entry["variants"] = variants
 
        results.append(entry)
 
    return results
 
 
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
 
def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    all_types = []
    for rs_file in sorted(root.rglob("*.rs")):
        # skip test files to avoid duplicates from mock contracts
        src = rs_file.read_text(encoding="utf-8", errors="replace")
        rel = str(rs_file.relative_to(root))
        found = extract_from_source(src, rel)
        all_types.extend(found)
        
    annotate_recursive(all_types)
    
    print(json.dumps(all_types, indent=2))

    
main()
