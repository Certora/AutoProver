#!/usr/bin/env python3
"""
Parse all Rust source files under a Soroban contract repo and extract
every type annotated with #[contracttype], producing a JSON description
of each type's kind (struct / enum / tuple-struct) and its fields or variants.

Each field carries a "recursive" flag that is True when the field's type,
or the type of any transitive child field, is the same type that contains
the field (i.e. the type is directly or indirectly self-referential through
that field).

Rust sources are parsed with tree-sitter-rust (see rust_ast.py):
    pip install tree-sitter tree-sitter-rust
"""

import json
import re
import sys
from pathlib import Path
from util import *
import rust_ast as ra

# Attributes that mark a Soroban contract type.
CONTRACT_TYPE_ATTRS = ('contracttype', 'contracterror', 'contractclient')

# ---------------------------------------------------------------------------
# Type-string cleanup
# ---------------------------------------------------------------------------

def clean_type(raw: str) -> str:
    raw = raw.strip().rstrip(',').strip()
    # collapse internal whitespace
    raw = re.sub(r'\s+', ' ', raw)
    return raw


# ---------------------------------------------------------------------------
# Type-alias collection
# ---------------------------------------------------------------------------

def parse_type_aliases_tree(root) -> dict[str, str]:
    """Collect non-parametric `type Alias = RhsType;` items (outside test
    modules and macro bodies) as {alias_name: rhs_type_string}.  Parametric
    aliases such as `type Foo<T> = Bar<T>` are skipped because their RHS
    depends on the type parameter and cannot be substituted without
    specialisation."""
    aliases: dict[str, str] = {}
    for node in ra.items_of_type(root, 'type_item'):
        if node.child_by_field_name('type_parameters') is not None:
            continue  # parametric alias – skip
        rhs = clean_type(ra.text(node.child_by_field_name('type')))
        if rhs:
            aliases[ra.name_of(node)] = rhs
    return aliases


def parse_type_aliases(src: str) -> dict[str, str]:
    """Scan *src* for non-parametric `type Alias = RhsType;` declarations."""
    return parse_type_aliases_tree(ra.parse(src).root_node)

# ---------------------------------------------------------------------------
# Struct / enum body parsers (tree-sitter nodes)
# ---------------------------------------------------------------------------

def parse_struct_fields(body) -> list[dict]:
    """Named fields of a `field_declaration_list` node ({ … })."""
    fields = []
    if body is None:
        return fields
    for fd in ra.named_children(body):
        if fd.type != 'field_declaration':
            continue
        name = ra.text(fd.child_by_field_name('name'))
        field_type = clean_type(ra.text(fd.child_by_field_name('type')))
        if name and field_type:
            fields.append({"name": name, "type": parse_type_str(field_type)})
    return fields


def parse_tuple_struct_fields(body) -> list[dict]:
    """Positional fields of an `ordered_field_declaration_list` node (( … ))."""
    fields = []
    if body is None:
        return fields
    for idx, ty in enumerate(body.children_by_field_name('type')):
        part = clean_type(ra.text(ty))
        if part:
            fields.append({"index": idx, "type": parse_type_str(part)})
    return fields


def parse_enum_variants(body) -> list[dict]:
    """Variants of an `enum_variant_list` node ({ … })."""
    variants = []
    if body is None:
        return variants
    for v in ra.named_children(body):
        if v.type != 'enum_variant':
            continue
        vname = ra.name_of(v)
        vbody = v.child_by_field_name('body')
        value = v.child_by_field_name('value')
        if vbody is None:
            entry = {"variant": vname, "kind": "unit"}
            if value is not None:
                entry["discriminant"] = ra.text(value).strip()
            variants.append(entry)
        elif vbody.type == 'field_declaration_list':
            variants.append({"variant": vname, "kind": "struct",
                             "fields": parse_struct_fields(vbody)})
        else:
            variants.append({"variant": vname, "kind": "tuple",
                             "fields": parse_tuple_struct_fields(vbody)})
    return variants


def _reparse_body(kind: str, body_text: str):
    """Parse the text of a struct/enum body that came out of a macro token
    tree, returning the tree-sitter body node (or None)."""
    code = f'{kind} __Macro {body_text}'
    if kind == 'struct' and body_text.lstrip().startswith('('):
        code += ';'
    root = ra.parse(code).root_node
    for item in root.named_children:
        if item.type in ('struct_item', 'enum_item'):
            return item.child_by_field_name('body')
    return None


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
 
 
def _idents_from_parsed_type(t: dict) -> list[str]:
    """Collect every non-builtin identifier reachable inside a parsed-type dict."""
    names = []
    base = t.get("type", "")
    if base and base not in _RUST_BUILTINS:
        names.append(base)
    for param in t.get("params", []):
        names.extend(_idents_from_parsed_type(param))
    return names


def _field_types(entry: dict) -> list[dict]:
    """Return all parsed-type dicts stored in the fields of *entry*."""
    types = []
    if entry["kind"] == "struct":
        for f in entry.get("fields", []):
            types.append(f["type"])
    else:  # enum
        for v in entry.get("variants", []):
            for f in v.get("fields", []):
                types.append(f["type"])
    return types
 
 
def is_recursive(type_val: dict, target: str,
                 type_map: dict, visited: set) -> bool:
    """
    Return True if *target* appears in *type_val* or in the type of any
    transitive child field reachable from *type_val*.
 
    *visited* prevents infinite loops when the contracttype graph has cycles.
    """
    for name in _idents_from_parsed_type(type_val):
        if name == target:
            return True
        if name in type_map and name not in visited:
            visited.add(name)
            for child_type in _field_types(type_map[name]):
                if is_recursive(child_type, target, type_map, visited):
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
# Macro-metavariable resolution
#
# Some #[contracttype] definitions live inside a macro_rules! body and use
# metavariable repetition syntax (e.g. `$(pub $ttl_field: u64),*`) instead of
# literal field names.  tree-sitter keeps macro bodies as token trees, so the
# helpers below walk those token trees to find such definitions, locate the
# enclosing macro's call-site invocation in the same source file, and
# materialise the concrete fields / variants from the invocation arguments.
# ---------------------------------------------------------------------------

def has_macro_metavars(text: str) -> bool:
    """Return True when *text* contains Rust macro metavariable syntax ('$')."""
    return '$' in text


def _macro_name(invocation) -> str:
    return ra.squash(ra.text(invocation.child_by_field_name('macro'))).split('::')[-1]


def find_macro_invocation_tt(root, macro_name: str):
    """
    Return the token tree of the first call-site ``macro_name! { ... }`` (or
    ``macro_name! ( ... )``) outside any ``macro_rules!`` body and test
    module; otherwise return ``None``.
    """
    for inv in ra.items_of_type(root, 'macro_invocation'):
        if _macro_name(inv) != macro_name:
            continue
        tt = next((c for c in inv.named_children if c.type == 'token_tree'), None)
        if tt is not None and ra.tt_delim(tt) in ('{', '('):
            return tt
    return None


def _invocation_entries(tt) -> list[list]:
    """Split an invocation token tree into its top-level comma-separated
    entries (lists of tokens).  Bracketed groups are single token-tree nodes,
    so only `<`/`>` are not tracked — matching how the entries are written
    (`Variant(A, B) => field`)."""
    return ra.split_tokens(ra.tt_inner(tt), ',')


def _arrow_target(entry: list):
    """For `… => ident`, return (index_of_arrow, ident) or (None, None)."""
    for i, tok in enumerate(entry):
        if tok.type == '=>' and i + 1 < len(entry) and entry[i + 1].type == 'identifier':
            return i, ra.text(entry[i + 1])
    return None, None


def _repetition_parts(src: bytes, rep):
    """Decompose a `$( … ) sep op` token_repetition into
    (inner_tokens, separator_text, operator)."""
    kids = [c for c in rep.children if not ra.is_comment(c)]
    if len(kids) < 4 or kids[0].type != '$' or kids[1].type != '(':
        return None
    op = kids[-1]
    close = kids[-2]
    if op.type not in ('*', '+', '?') or close.type != ')':
        return None
    sep = src[close.end_byte:op.start_byte].decode('utf-8', errors='replace').strip()
    return kids[2:-2], sep, op.type


def _metavar_fragment(macro_def, metavar: str):
    """Fragment specifier (`ident`, `tt`, …) of *metavar* in the macro's matchers."""
    stack = [macro_def]
    while stack:
        n = stack.pop()
        if n.type == 'token_binding_pattern' and ra.text(n.child_by_field_name('name')) == metavar:
            return ra.text(n.child_by_field_name('type'))
        stack.extend(reversed(n.named_children))
    return None


def expand_macro_struct_fields(body_tt, macro_def, root, src: bytes):
    """
    When a struct body inside a macro_rules! transcriber is a metavariable
    repetition like ``$(pub $ttl_field: u64),*``, resolve the concrete fields
    from the enclosing macro's call-site invocation.

    Handles the pattern ``$(pub? $FIELD_VAR: FieldType),*`` where $FIELD_VAR
    is bound to the identifier after ``=>`` in each invocation entry.

    Returns a list of field dicts on success, or ``None``.
    """
    if macro_def is None:
        return None
    inner = ra.tt_inner(body_tt)
    if not inner or inner[0].type != 'token_repetition':
        return None
    parts = _repetition_parts(src, inner[0])
    if parts is None:
        return None
    toks, sep, op = parts
    if sep != ',' or op != '*':
        return None
    i = 0
    if i < len(toks) and toks[i].type == 'pub':
        i += 1
    if not (i + 1 < len(toks) and toks[i].type == 'metavariable' and toks[i + 1].type == ':'):
        return None
    type_toks = toks[i + 2:]
    if not type_toks:
        return None
    field_type_str = ra.span_text(src, type_toks).strip()

    inv = find_macro_invocation_tt(root, ra.name_of(macro_def))
    if inv is None:
        return None

    unique: list[str] = []
    for entry in _invocation_entries(inv):
        _, name = _arrow_target(entry)
        if name and name not in unique:
            unique.append(name)
    if not unique:
        return None

    return [{"name": name, "type": parse_type_str(field_type_str)} for name in unique]


def expand_macro_enum_variants(body_tt, macro_def, root, src: bytes):
    """
    When an enum body inside a macro_rules! transcriber contains metavariable
    repetition (e.g. ``$($variant),*`` or ``$($ev)*``), resolve the concrete
    variants from the enclosing macro's call-site invocation.

    Each invocation entry must be either:
      ``Variant(F1, F2, ...) => ttl_field``  (tuple variant)
      ``Variant => ttl_field``               (unit variant)

    The ``=> ttl_field`` suffix is stripped before interpreting the variant.

    Returns a list of variant dicts on success, or ``None``.
    """
    if macro_def is None:
        return None
    inner = ra.tt_inner(body_tt)
    if not inner or inner[0].type != 'token_repetition':
        return None

    inv = find_macro_invocation_tt(root, ra.name_of(macro_def))
    if inv is None:
        return None

    # A repetition over a single bare metavariable, e.g. `$($variant),*`,
    # emits only the variant *name* of each invocation entry: the enum is a
    # fieldless "tag" enum (like `OperationKind` alongside `GovernanceAction`),
    # even if the invocation entries carry field lists for another type.
    # Only when that metavariable is declared `:ident` in the macro's matchers;
    # a `:tt` bundle such as `$($ev)*` carries whole variants (with fields).
    names_only = False
    if len(inner) == 1:
        parts = _repetition_parts(src, inner[0])
        if parts is not None:
            toks, sep, _op = parts
            if len(toks) == 1 and toks[0].type == 'metavariable' and sep in ('', ','):
                names_only = _metavar_fragment(macro_def, ra.text(toks[0])) == 'ident'

    variants: list[dict] = []
    for entry in _invocation_entries(inv):
        arrow, _ = _arrow_target(entry)
        if arrow is not None and arrow + 2 == len(entry):
            entry = entry[:arrow]
        if not entry or entry[0].type in ('$', 'metavariable', 'token_repetition'):
            continue
        if entry[0].type != 'identifier':
            continue
        vname = ra.text(entry[0])
        if names_only:
            variants.append({"variant": vname, "kind": "unit"})
            continue
        body = _reparse_body('enum', '{ ' + ra.span_text(src, entry) + ' }')
        parsed = parse_enum_variants(body)
        if parsed and parsed[0]["variant"] == vname:
            variants.append(parsed[0])
        else:
            variants.append({"variant": vname, "kind": "unit"})

    return variants if variants else None


def _is_attr_tokens(toks: list, i: int) -> bool:
    return (i + 1 < len(toks) and toks[i].type == '#'
            and toks[i + 1].type == 'token_tree' and ra.tt_delim(toks[i + 1]) == '[')


def _find_macro_contracttypes(toks: list, macro_def, out: list) -> None:
    """Find `#[contracttype] (pub)? struct|enum Name …` sequences in a macro
    token list (recursively), appending
    (start_byte, kind, name, body_token_tree_or_None, macro_def)."""
    i = 0
    while i < len(toks):
        tok = toks[i]
        if _is_attr_tokens(toks, i):
            attr_inner = ra.tt_inner(toks[i + 1])
            is_contract_attr = (
                attr_inner and attr_inner[0].type == 'identifier'
                and ra.text(attr_inner[0]) in CONTRACT_TYPE_ATTRS
                and (len(attr_inner) == 1 or (len(attr_inner) == 2
                                              and attr_inner[1].type == 'token_tree'
                                              and ra.tt_delim(attr_inner[1]) == '(')))
            if is_contract_attr:
                j = i + 2
                while _is_attr_tokens(toks, j):          # further attributes
                    j += 2
                if j < len(toks) and toks[j].type == 'pub':
                    j += 1
                    if j < len(toks) and toks[j].type == 'token_tree' and ra.tt_delim(toks[j]) == '(':
                        j += 1
                if j + 1 < len(toks) and toks[j].type in ('struct', 'enum') \
                        and toks[j + 1].type == 'identifier':
                    kind, name = toks[j].type, ra.text(toks[j + 1])
                    j += 2
                    if j < len(toks) and toks[j].type == '<':  # generics
                        depth = 0
                        while j < len(toks):
                            if toks[j].type == '<':
                                depth += 1
                            elif toks[j].type == '>':
                                depth -= 1
                                if depth == 0:
                                    j += 1
                                    break
                            j += 1
                    body = None
                    if j < len(toks) and toks[j].type == 'token_tree' and ra.tt_delim(toks[j]) in ('{', '('):
                        body = toks[j]
                    out.append((toks[i].start_byte, kind, name, body, macro_def))
                    i = j + 1
                    continue
        if tok.type in ('token_tree', 'token_repetition'):
            _find_macro_contracttypes(ra.tt_inner(tok) if tok.type == 'token_tree'
                                      else list(tok.children), macro_def, out)
        i += 1


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

def _struct_entry_fields(entry: dict, body) -> None:
    """Fill struct_kind / fields / macro_expanded (in the original key order)."""
    if body is not None and body.type == 'ordered_field_declaration_list':
        entry["struct_kind"] = "tuple"
        entry["fields"] = parse_tuple_struct_fields(body)
        entry["macro_expanded"] = False
    elif body is not None and body.type == 'field_declaration_list':
        entry["macro_expanded"] = False
        entry["struct_kind"] = "named"
        entry["fields"] = parse_struct_fields(body)
    else:
        entry["struct_kind"] = "unit"
        entry["fields"] = []
        entry["macro_expanded"] = False


def _macro_entry_body(entry: dict, kind: str, body_tt, macro_def, root, src: bytes) -> None:
    """Fill an entry for a contract type found inside a macro token tree."""
    if kind == "struct":
        if body_tt is None:
            _struct_entry_fields(entry, None)
            return
        body_text = ra.text(body_tt)
        if ra.tt_delim(body_tt) == '(':
            _struct_entry_fields(entry, _reparse_body('struct', body_text))
            return
        if has_macro_metavars(body_text):
            expanded = expand_macro_struct_fields(body_tt, macro_def, root, src)
            fields = expanded if expanded is not None else parse_struct_fields(_reparse_body('struct', body_text))
            entry["macro_expanded"] = expanded is not None
        else:
            fields = parse_struct_fields(_reparse_body('struct', body_text))
            entry["macro_expanded"] = False
        entry["struct_kind"] = "named"
        entry["fields"] = fields
    else:
        body_text = ra.text(body_tt) if body_tt is not None else '{}'
        if body_tt is not None and has_macro_metavars(body_text):
            expanded = expand_macro_enum_variants(body_tt, macro_def, root, src)
            variants = expanded if expanded is not None else parse_enum_variants(_reparse_body('enum', body_text))
            entry["macro_expanded"] = expanded is not None
        else:
            variants = parse_enum_variants(_reparse_body('enum', body_text))
            entry["macro_expanded"] = False
        entry["variants"] = variants


def extract_from_source(src: str, filepath: str, root=None) -> list[dict]:
    """Return all contracttype definitions found in *src* (or in the already
    parsed tree *root* of it).

    Items inside test-gated inline modules are ignored, as are the bodies of
    `cfg_if!` invocations.  Definitions inside other macro bodies (notably
    `macro_rules!` transcribers) are found by scanning their token trees.
    """
    if root is None:
        root = ra.parse(src).root_node
    src_b = root.text
    # Build a map of explicitly-imported names for this file so cross-crate
    # types referenced in fields can be annotated with their use path even
    # when those types aren't themselves contracttypes in the scanned set.
    file_uses = parse_file_uses_tree(root)

    found: list[tuple] = []   # (start_byte, kind, name, body, macro_def_or_None, from_macro)
    for node in ra.walk(root):
        if node.type in ('struct_item', 'enum_item') and ra.has_attr(node, *CONTRACT_TYPE_ATTRS):
            kind = 'struct' if node.type == 'struct_item' else 'enum'
            found.append((node.start_byte, kind, ra.name_of(node),
                          node.child_by_field_name('body'), None, False))
        elif node.type == 'macro_definition':
            hits: list = []
            for rule in node.named_children:
                if rule.type == 'macro_rule':
                    right = rule.child_by_field_name('right')
                    if right is not None:
                        _find_macro_contracttypes(ra.tt_inner(right), node, hits)
            found.extend(h + (True,) for h in hits)
        elif node.type == 'macro_invocation' and _macro_name(node) != 'cfg_if':
            hits = []
            for tt in node.named_children:
                if tt.type == 'token_tree':
                    _find_macro_contracttypes(ra.tt_inner(tt), None, hits)
            found.extend(h + (True,) for h in hits)
    found.sort(key=lambda f: f[0])

    results = []
    for _start, kind, name, body, macro_def, from_macro in found:
        entry: dict = {"name": name,
                       "kind": kind,
                       "file": filepath,
                       "use": src_path_to_module(filepath) + "::" + name,
                       "_file_uses": file_uses,
                    }
        if from_macro:
            _macro_entry_body(entry, kind, body, macro_def, root, src_b)
        elif kind == "struct":
            _struct_entry_fields(entry, body)
        else:
            entry["macro_expanded"] = False
            entry["variants"] = parse_enum_variants(body)
        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Crate discovery & privacy-aware "use" resolution
#
# The "use" field above (from src_path_to_module + name) is only a guess
# based on where the file sits on disk -- it doesn't know whether every
# intermediate module in that path is actually declared `pub`, whether the
# owning crate itself is private (unpublished), or whether a *different*,
# public crate re-exports this exact type. This section adds a real,
# crate-aware resolution pass that corrects both problems:
#   1. It verifies the type's actual publicly-reachable module path by
#      walking each crate's module tree from its own lib.rs/main.rs,
#      following only `pub mod` declarations (mirroring how `cargo doc`
#      would see it).
#   2. If the owning crate is private (`publish = false`/`publish = []` in
#      its Cargo.toml) but some other, public crate re-exports this exact
#      item via an unrestricted `pub use`, the "use" path is redirected to
#      that public re-export instead (chasing through a re-export chain
#      across private crates if necessary).
#   3. If the owning crate is private and no public re-export exists
#      anywhere in the workspace, "use" is set to null and a "private_note"
#      field explains why.
# When a type's owning crate can't be determined, or the type isn't
# actually reachable via any `pub mod` chain in its own crate, this falls
# back to the original src_path_to_module-derived guess.
# ---------------------------------------------------------------------------
 
def _strip_toml_comments(text: str) -> str:
    """Remove '# ...' comments (good enough for Cargo.toml in practice)."""
    return re.sub(r'#[^\n]*', '', text)
 
 
def _section_text(text: str, header: str) -> str:
    """Raw lines under the exact top-level TOML header `[header]`, stopping
    at the next '[' line (any header, including sub-tables)."""
    lines = text.splitlines()
    out = []
    in_section = False
    header_re = re.compile(r'^\[' + re.escape(header) + r'\]\s*$')
    for line in lines:
        stripped = line.strip()
        if header_re.match(stripped):
            in_section = True
            continue
        if in_section and stripped.startswith('['):
            break
        if in_section:
            out.append(line)
    return '\n'.join(out)
 
 
def _split_dependency_entries(section_text: str) -> dict:
    """Split a dependencies-section body into {key: raw_value_text},
    handling inline tables that wrap across multiple lines."""
    entries: dict[str, str] = {}
    lines = section_text.splitlines()
    i = 0
    key_line_re = re.compile(r'^([A-Za-z0-9_.\-]+)\s*=\s*(.*)$')
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        m = key_line_re.match(line)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        collected = [rest]
        depth = rest.count('{') + rest.count('[') - rest.count('}') - rest.count(']')
        while depth > 0 and i < len(lines):
            nxt = lines[i]
            i += 1
            collected.append(nxt)
            depth += nxt.count('{') + nxt.count('[') - nxt.count('}') - nxt.count(']')
        entries[key] = ' '.join(collected).strip()
    return entries
 
 
def _parse_dependency_entry(raw: str) -> dict:
    is_workspace = bool(re.search(r'\bworkspace\s*=\s*true', raw))
    pkg_m = re.search(r'\bpackage\s*=\s*"([^"]+)"', raw)
    return {"package_override": pkg_m.group(1) if pkg_m else None, "is_workspace": is_workspace}
 
 
def parse_cargo_dependencies(text: str) -> dict:
    """{dep_key: {"package_override", "is_workspace"}} merged across
    [dependencies], [dev-dependencies], [build-dependencies]."""
    text = _strip_toml_comments(text)
    merged: dict[str, dict] = {}
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        body = _section_text(text, section)
        for key, raw in _split_dependency_entries(body).items():
            merged[key] = _parse_dependency_entry(raw)
    return merged
 
 
def build_dependency_alias_map(cargo_text: str, root_ws_deps: dict) -> dict:
    """{local_identifier: target_crate_name}, both underscored, resolving
    `package = "..."` renames and `{ workspace = true }` indirection
    through the root [workspace.dependencies] table."""
    deps = parse_cargo_dependencies(cargo_text)
    alias_map: dict[str, str] = {}
    for key, entry in deps.items():
        target = entry["package_override"]
        if target is None and entry["is_workspace"]:
            ws_entry = root_ws_deps.get(key)
            if ws_entry and ws_entry.get("package_override"):
                target = ws_entry["package_override"]
        if target is None:
            target = key
        alias_map[key.replace('-', '_')] = target.replace('-', '_')
    return alias_map
 
 
def is_private_package(cargo_text: str) -> bool:
    """True when [package] sets `publish = false` or `publish = []` --
    the standard Cargo convention for "never publish this crate"."""
    pkg = _section_text(_strip_toml_comments(cargo_text), "package")
    m = re.search(r'^\s*publish\s*=\s*(.+)$', pkg, re.MULTILINE)
    if not m:
        return False
    val = m.group(1).strip()
    return val == "false" or bool(re.match(r'^\[\s*\]$', val))
 
 
def parse_package_name(cargo_text: str) -> str | None:
    pkg = _section_text(_strip_toml_comments(cargo_text), "package")
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', pkg, re.MULTILINE)
    return m.group(1) if m else None
 
 
def resolve_module_file(parent_file: Path, mod_name: str) -> Path | None:
    """parent/mod_name.rs or parent/mod_name/mod.rs, following Rust's
    module-file resolution rules."""
    parent_dir = parent_file.parent
    c1 = parent_dir / f"{mod_name}.rs"
    if c1.exists():
        return c1
    c2 = parent_dir / mod_name / "mod.rs"
    if c2.exists():
        return c2
    return None
 
 
def find_pub_use_statements(container) -> list:
    """Unrestricted `pub use ...;` statements that are direct children of
    *container* (a source_file or an inline module's declaration_list).
    `pub(crate) use` / `pub(super) use` / `pub(in ...) use` are excluded.
    Returns [{"path_prefix": str|None, "items": [{"name","alias"}]}], where an
    item "name" is relative to "path_prefix" and "*" marks a glob."""
    results = []
    for node in ra.named_children(container):
        if node.type != 'use_declaration':
            continue
        decl = ra.use_decl_of(node)
        if not decl.is_pub:
            continue
        prefix = decl.root_path
        items = []
        nested_globs = []
        for leaf in decl.leaves:
            if leaf.kind == 'glob':
                if leaf.path == prefix:
                    items.append({"name": "*", "alias": None})
                else:
                    nested_globs.append(leaf.path)
                continue
            path = leaf.path if leaf.kind == 'name' else _join_path(leaf.path, 'self')
            if prefix and path.startswith(prefix + '::'):
                rel = path[len(prefix) + 2:]
            else:
                rel = path
            items.append({"name": rel, "alias": leaf.alias})
        if items:
            results.append({"path_prefix": prefix or None, "items": items})
        for g in nested_globs:
            results.append({"path_prefix": g or None, "items": [{"name": "*", "alias": None}]})
    return results


def _join_path(prefix: str, rest: str) -> str:
    return f"{prefix}::{rest}" if prefix else rest


def parse_file_uses_tree(root) -> dict[str, str]:
    """All `use` declarations under *root* as {local_name: full_qualified_path}.

    Example: `use k2_shared::{Asset, AssetConfig};`
      → {"Asset": "k2_shared::Asset", "AssetConfig": "k2_shared::AssetConfig"}

    Both plain `use` and `pub use` count for our annotation purposes; glob
    imports are ignored.
    """
    result: dict[str, str] = {}
    for decl in ra.use_decls(root):
        for leaf in decl.leaves:
            if leaf.kind == 'glob':
                continue
            if leaf.kind == 'self':
                if leaf.path:
                    result[leaf.alias or leaf.path.split('::')[-1]] = leaf.path
                continue
            result[leaf.local_name] = leaf.path
    return result


def parse_file_uses(src: str) -> dict[str, str]:
    """Parse all `use` statements from *src* → {local_name: full_qualified_path}."""
    return parse_file_uses_tree(ra.parse(src).root_node)


_ITEM_KEYWORDS = ('struct', 'enum', 'union', 'trait', 'type', 'fn', 'const', 'static')


def _macro_token_trees(node) -> list:
    """Token trees of a macro definition's transcribers or an invocation's arguments."""
    if node.type == 'macro_definition':
        return [r.child_by_field_name('right') for r in node.named_children
                if r.type == 'macro_rule' and r.child_by_field_name('right') is not None]
    return [c for c in node.named_children if c.type == 'token_tree']


def _pub_item_names_in_tokens(toks: list) -> list[str]:
    """Names in `pub struct|enum|trait|type|fn|const|static Name` token
    sequences of a macro body (recursively)."""
    names = []
    for i, tok in enumerate(toks):
        if tok.type == 'pub' and i + 2 < len(toks) and toks[i + 1].type in _ITEM_KEYWORDS \
                and toks[i + 2].type == 'identifier':
            names.append(ra.text(toks[i + 2]))
        elif tok.type == 'token_tree':
            names.extend(_pub_item_names_in_tokens(ra.tt_inner(tok)))
        elif tok.type == 'token_repetition':
            names.extend(_pub_item_names_in_tokens(list(tok.children)))
    return names


# Item kinds recorded as publicly reachable names of a module.
_PUBLIC_ITEM_TYPES = ('struct_item', 'enum_item', 'union_item', 'trait_item', 'type_item',
                      'function_item', 'const_item', 'static_item')


def scan_crate_public_items(crate_root: Path, dependency_aliases: dict) -> tuple:
    """
    Walk *crate_root*'s publicly visible module tree from lib.rs/main.rs,
    following only `pub mod` chains, and return:
      (pub_items, foreign_reexports, foreign_globs)
    where pub_items is {name: qualified_module_path} for every publicly
    reachable struct/enum/trait/type/fn/const/static and pub-use re-export
    (shortest path kept when reachable more than one way), and
    foreign_reexports/foreign_globs record `pub use` statements that
    re-export something from ANOTHER workspace crate (resolved via
    *dependency_aliases*), for cross-crate public-reexport detection.
    Only plain `pub` items count (not `pub(crate)` etc.), items are attributed
    to the module that actually contains them (inline `pub mod x { … }`
    blocks included), and test-gated modules are skipped.
    """
    entry = crate_root / "src" / "lib.rs"
    if not entry.exists():
        entry = crate_root / "src" / "main.rs"
    if not entry.exists():
        return {}, [], []

    pub_items: dict[str, str] = {}
    foreign_reexports: list = []
    foreign_globs: list = []
    visited: set = set()

    def record(name: str, module_path: list) -> None:
        qpath = "::".join(module_path)
        existing = pub_items.get(name)
        if existing is None or len(qpath) < len(existing):
            pub_items[name] = qpath

    def resolve_glob_target(rs_file: Path, module_path: list, prefix: str):
        parts = [p for p in prefix.split('::') if p]
        if not parts:
            return None, None
        cur_file = rs_file
        if parts[0] == 'crate':
            cur_file = entry
            parts = parts[1:]
        elif parts[0] == 'self':
            parts = parts[1:]
        for part in parts:
            resolved = resolve_module_file(cur_file, part)
            if not resolved:
                return None, None
            cur_file = resolved
        return cur_file, module_path

    def scan_pub_uses(container, rs_file: Path, module_path: list) -> None:
        for stmt in find_pub_use_statements(container):
            prefix = stmt["path_prefix"]
            for it in stmt["items"]:
                if it["name"] == "*":
                    if not prefix:
                        continue
                    target_file, target_path = resolve_glob_target(rs_file, module_path, prefix)
                    if target_file:
                        scan_file(target_file, target_path, is_public_context=True)
                        continue
                    parts = [p for p in prefix.split('::') if p]
                    if parts and parts[0] not in ('crate', 'self', 'super'):
                        src_crate = dependency_aliases.get(parts[0])
                        if src_crate:
                            foreign_globs.append({
                                "source_crate": src_crate,
                                "source_qpath_prefix": "::".join(parts[1:]),
                                "dest_qpath": "::".join(module_path),
                            })
                    continue

                full_parts = [p for p in (
                    (prefix.split('::') if prefix else []) + it["name"].split('::')
                ) if p]
                if not full_parts:
                    continue
                first_seg = full_parts[0]
                final_name = it["alias"] if it["alias"] else full_parts[-1]
                if final_name in ("self", "crate", "super"):
                    continue
                record(final_name, module_path)

                if first_seg not in ("crate", "self", "super") and not resolve_module_file(rs_file, first_seg):
                    src_crate = dependency_aliases.get(first_seg)
                    if src_crate:
                        foreign_reexports.append({
                            "source_crate": src_crate,
                            "source_qpath": "::".join(full_parts[1:-1]),
                            "source_name": full_parts[-1],
                            "dest_qpath": "::".join(module_path),
                            "dest_name": final_name,
                        })

    def scan_items(container, rs_file: Path, module_path: list, is_public_context: bool) -> None:
        for node in ra.named_children(container):
            if node.type == 'mod_item':
                if not ra.is_pub(node) or ra.is_test_item(node):
                    continue
                mod_name = ra.name_of(node)
                if is_public_context:
                    record(mod_name, module_path)
                body = node.child_by_field_name('body')
                if body is None:
                    mod_file = resolve_module_file(rs_file, mod_name)
                    if mod_file:
                        scan_file(mod_file, module_path + [mod_name], is_public_context=is_public_context)
                else:
                    scan_items(body, rs_file, module_path + [mod_name], is_public_context)
            elif node.type in _PUBLIC_ITEM_TYPES:
                if is_public_context and ra.is_pub(node):
                    record(ra.name_of(node), module_path)
            elif node.type in ('macro_definition', 'macro_invocation') and is_public_context:
                # Items generated by a macro in this module (e.g. a
                # `macro_rules!` that emits `pub struct TtlConfig { … }`):
                # macros cannot be expanded here, so record `pub <item> Name`
                # token sequences found in their bodies.
                if node.type == 'macro_invocation' and _macro_name(node) == 'cfg_if':
                    continue
                for tt in _macro_token_trees(node):
                    for name in _pub_item_names_in_tokens(ra.tt_inner(tt)):
                        record(name, module_path)
        if is_public_context:
            scan_pub_uses(container, rs_file, module_path)

    def scan_file(rs_file: Path, module_path: list, is_public_context: bool) -> None:
        rs_file = rs_file.resolve()
        key = (rs_file, tuple(module_path))
        if key in visited:
            return
        visited.add(key)

        try:
            root = ra.parse_file(rs_file).root_node
        except OSError:
            return
        scan_items(root, rs_file, module_path, is_public_context)

    scan_file(entry, [], is_public_context=True)
    return pub_items, foreign_reexports, foreign_globs


def qualified_path(crate_name: str, qpath: str, name: str) -> str:
    """
    A bare, dot/colon-qualified path to *name* -- matching this script's
    existing "use" field convention (e.g. "crate::foo::bar::TypeName", as
    produced by src_path_to_module), just with the *actual* crate name in
    place of the literal self-referential "crate" segment. NOT a full Rust
    `use ...;` statement (no "use " prefix or trailing ";").
    """
    return f"{crate_name}::{qpath}::{name}" if qpath else f"{crate_name}::{name}"
 
 
def resolve_use(name: str, home: tuple, private_set: set, reexports_by_source: dict):
    """
    Given an item's home (crate_name, qualified_module_path), decide what
    to report about importing *name*. Returns:
      ("use", "crate_name::path::Name")  -- public crate, or a private
                                             crate re-exported publicly
                                             (chasing multi-hop chains via
                                             breadth-first search across
                                             every re-export candidate)
      ("private", "explanation text")    -- private crate, no public
                                             re-export found anywhere
    """
    crate_name, qpath = home
    if crate_name not in private_set:
        return ("use", qualified_path(crate_name, qpath, name))
 
    start = (crate_name, qpath, name)
    visited = {start}
    queue = [start]
    while queue:
        key = queue.pop(0)
        for dest_crate, dest_qpath, dest_name in reexports_by_source.get(key, []):
            if dest_crate not in private_set:
                return ("use", qualified_path(dest_crate, dest_qpath, dest_name))
            dest_key = (dest_crate, dest_qpath, dest_name)
            if dest_key not in visited:
                visited.add(dest_key)
                queue.append(dest_key)
 
    return ("private", f"'{crate_name}' is a private (unpublished) crate and this item has no public re-export")
 
 
def discover_crates(root: Path) -> dict:
    """
    Find every Cargo.toml under *root* (skipping target/), and return
    {crate_root_dir (resolved Path): {
        "crate_name", "is_private", "pub_items", "src_dir"
    }}, plus the merged lists of foreign_reexports/foreign_globs (each
    tagged with "dest_crate") collected across all of them.
    """
    root_ws_deps: dict = {}
    root_cargo = root / "Cargo.toml"
    if root_cargo.exists():
        root_text = _strip_toml_comments(root_cargo.read_text(encoding="utf-8", errors="replace"))
        ws_dep_body = _section_text(root_text, "workspace.dependencies")
        root_ws_deps = {
            key: _parse_dependency_entry(raw)
            for key, raw in _split_dependency_entries(ws_dep_body).items()
        }
 
    crates: dict = {}
    all_foreign_reexports: list = []
    all_foreign_globs: list = []
 
    for cargo_toml in sorted(root.rglob("Cargo.toml")):
        if "target" in cargo_toml.relative_to(root).parts:
            continue
        try:
            text = cargo_toml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = parse_package_name(text)
        if not name:
            continue  # virtual workspace-only manifest
        crate_name = name.replace('-', '_')
        crate_root = cargo_toml.parent.resolve()
        is_private = is_private_package(text)
        dependency_aliases = build_dependency_alias_map(text, root_ws_deps)
 
        pub_items, foreign_reexports, foreign_globs = scan_crate_public_items(crate_root, dependency_aliases)
        for fr in foreign_reexports:
            fr["dest_crate"] = crate_name
        for fg in foreign_globs:
            fg["dest_crate"] = crate_name
 
        crates[crate_root] = {
            "crate_name": crate_name,
            "is_private": is_private,
            "pub_items": pub_items,
            "src_dir": (crate_root / "src").resolve(),
        }
        all_foreign_reexports.extend(foreign_reexports)
        all_foreign_globs.extend(foreign_globs)
 
    return crates, all_foreign_reexports, all_foreign_globs
 
 
def find_owning_crate(rs_file: Path, crates: dict):
    """Return the crate dict owning *rs_file* (the crate whose src/ dir is
    an ancestor of it), or None if it isn't part of any discovered crate."""
    rs_file = rs_file.resolve()
    best = None
    for info in crates.values():
        src_dir = info["src_dir"]
        if src_dir in rs_file.parents or src_dir == rs_file.parent:
            if best is None or len(src_dir.parts) > len(best["src_dir"].parts):
                best = info
    return best
 
 
def resolve_entry_uses(all_types: list, root: Path) -> None:
    """
    Mutate every entry in *all_types* in place: replace the naive "use"
    (originally src_path_to_module(filepath) + "::" + name, computed with
    *filepath* relative to the whole repo root) with a corrected one.
 
    That original computation had two problems this fixes:
      1. src_path_to_module expects a path relative to the *crate* root
         (starting with "src/..."), but was being called with a path
         relative to the whole *repo* root -- which only happens to be the
         same thing for a single, standalone crate. For any multi-crate
         workspace it silently produced a wrong module path. This is now
         recomputed relative to the type's actual owning crate.
      2. It always used the literal self-referential "crate" segment,
         which never reveals which crate a type lives in -- so there was
         no way to know if that crate is private, or to redirect through
         a public re-export. Both are now resolved for real, verified by
         walking each crate's actual `pub mod` tree from its lib.rs/main.rs
         (see scan_crate_public_items) rather than assumed from the file
         path, and cross-referencing every workspace crate's `pub use`
         statements to redirect a private crate's item through wherever
         it's re-exported publicly (see resolve_use).
 
    Every entry gets a "private_note" field (null unless its owning crate
    is private with no public re-export found anywhere in the workspace,
    in which case "use" is also set to null). Entries whose owning crate
    can't be determined at all keep their original repo-root-relative
    "use" guess as an absolute last resort.
    """
    crates, foreign_reexports, foreign_globs = discover_crates(root)
 
    private_set = {info["crate_name"] for info in crates.values() if info["is_private"]}
    crates_by_name = {info["crate_name"]: info for info in crates.values()}
 
    reexports_by_source: dict = {}
 
    def add_reexport(key, dest):
        candidates = reexports_by_source.setdefault(key, [])
        if dest not in candidates:
            candidates.append(dest)
 
    for fr in foreign_reexports:
        key = (fr["source_crate"], fr["source_qpath"], fr["source_name"])
        add_reexport(key, (fr["dest_crate"], fr["dest_qpath"], fr["dest_name"]))
 
    for fg in foreign_globs:
        src_crate = crates_by_name.get(fg["source_crate"])
        if not src_crate:
            continue
        for name, qpath in src_crate["pub_items"].items():
            if qpath == fg["source_qpath_prefix"]:
                key = (fg["source_crate"], qpath, name)
                add_reexport(key, (fg["dest_crate"], fg["dest_qpath"], name))
 
    for entry in all_types:
        entry["private_note"] = None
        rs_file = (root / entry["file"]).resolve()
        owner = find_owning_crate(rs_file, crates)
        if owner is None:
            entry["public"] = None  # crate context unknown; can't determine
            continue  # keep the original best-effort "use" guess
 
        if entry["name"] in owner["pub_items"]:
            qpath = owner["pub_items"][entry["name"]]
        else:
            # Not verified reachable via any `pub mod` chain (e.g. it's
            # inside a #[cfg(test)] module) -- still recompute a
            # crate-correct path instead of the original repo-root-relative
            # guess, which is wrong for anything but a single-crate repo.
            crate_root = owner["src_dir"].parent
            try:
                crate_relative = str(rs_file.relative_to(crate_root))
            except ValueError:
                continue  # shouldn't happen; keep the original guess
            module_path = src_path_to_module(crate_relative)  # "crate::a::b" or "crate"
            qpath = "" if module_path == "crate" else module_path[len("crate::"):]
 
        in_pub_items = entry["name"] in owner["pub_items"]
        kind, payload = resolve_use(entry["name"], (owner["crate_name"], qpath), private_set, reexports_by_source)
        entry["owning_crate"] = owner["crate_name"]
        crate_prefix = owner["crate_name"] + "::"
        if kind == "use":
            # Rewrite the owning crate name to "crate" so the path is valid
            # from within the crate that defines the type.  Types that are
            # re-exported from a *different* (public) crate keep their full
            # qualified path because they don't live here.
            if payload.startswith(crate_prefix):
                entry["use"] = "crate::" + payload[len(crate_prefix):]
                # Public iff the type is reachable via the crate's pub mod tree.
                # A type in a public crate that lives in a private module is
                # not actually importable by external consumers.
                entry["public"] = in_pub_items
            else:
                entry["use"] = payload  # re-exported from another (public) crate
                entry["public"] = True
            entry["private_note"] = None
        else:
            # Private crate with no public re-export. Still record the best-effort
            # crate-relative path so that within-crate and within-workspace consumers
            # (e.g. Soroban contracts that never publish to crates.io) can still be
            # annotated with a use path.  private_note signals that the path isn't
            # publicly reachable outside the workspace.
            best = qualified_path(owner["crate_name"], qpath, entry["name"])
            entry["use"] = "crate::" + best[len(crate_prefix):] if best.startswith(crate_prefix) else best
            entry["private_note"] = payload
            entry["public"] = False
 
 
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
 
def annotate_type_dict_uses(all_types: list[dict]) -> None:
    """
    Walk every parsed-type dict stored in field "type" values and add a "use"
    key to any node whose type name matches a known contracttype that has a
    non-null "use" path.  The path is crate-relative ("crate::...") for types
    in the same crate as the containing entry, and fully qualified
    ("other_crate::...") for types from a different crate — consistent with
    the logic in annotate_field_uses.

    Must be called after resolve_entry_uses (needs "use" and "owning_crate").
    """
    info_by_name: dict[str, tuple[str | None, str | None, bool | None]] = {
        e["name"]: (e.get("use"), e.get("owning_crate"), e.get("public"))
        for e in all_types
    }

    def stamp(t: dict, entry_crate: str | None, file_uses: dict) -> None:
        name = t.get("type", "")
        if name in info_by_name:
            use_path, ident_crate, pub = info_by_name[name]
            if use_path:
                if ident_crate and entry_crate and ident_crate != entry_crate:
                    if use_path.startswith("crate::"):
                        use_path = ident_crate + use_path[5:]
                t["use"] = use_path
            if pub is not None:
                t["public"] = pub
        elif name and name not in _RUST_BUILTINS and name in file_uses:
            # Not a known contracttype in the scanned set, but the file
            # explicitly imports it — use that import path directly.
            t["use"] = file_uses[name]
            t["public"] = True  # must be publicly accessible to be importable
        for param in t.get("params", []):
            stamp(param, entry_crate, file_uses)

    def collect_uses(t: dict) -> list[str]:
        """Gather every "use" value stamped anywhere in a type dict tree."""
        uses = []
        if "use" in t:
            uses.append(t["use"])
        for param in t.get("params", []):
            uses.extend(collect_uses(param))
        return uses

    def any_private(t: dict) -> bool:
        """Return True if any node in the type dict tree has public=False."""
        if t.get("public") is False:
            return True
        return any(any_private(p) for p in t.get("params", []))

    def stamp_field(f: dict, entry_crate: str | None, file_uses: dict) -> None:
        stamp(f["type"], entry_crate, file_uses)
        uses = sorted(set(collect_uses(f["type"])))
        if uses:
            f["uses"] = uses
        if any_private(f["type"]):
            f["public"] = False

    for entry in all_types:
        entry_crate = entry.get("owning_crate")
        file_uses = entry.get("_file_uses", {})
        if entry["kind"] == "struct":
            for f in entry.get("fields", []):
                stamp_field(f, entry_crate, file_uses)
        else:  # enum
            for v in entry.get("variants", []):
                for f in v.get("fields", []):
                    stamp_field(f, entry_crate, file_uses)
                if any(f.get("public") is False for f in v.get("fields", [])):
                    v["public"] = False


def annotate_field_uses(all_types: list[dict]) -> None:
    """
    Add a "field_type_uses" list to every entry.

    Each element is a non-null "use" path for a contracttype referenced in at
    least one of the entry's field types.  Paths are resolved relative to the
    crate that *contains the entry*:
      - A field type from the same crate uses "crate::..." (already in that
        form from resolve_entry_uses).
      - A field type from a different crate uses the fully-qualified
        "other_crate::..." form (by substituting its crate name back in).
    The list is sorted and deduplicated.  Types with a null "use" (private
    crate, no public re-export) are omitted.

    Must be called *after* resolve_entry_uses so that every entry's "use"
    and "owning_crate" fields are set.
    """
    # name -> (crate-relative-use-path, owning_crate_name)
    info_by_name: dict[str, tuple[str | None, str | None]] = {
        e["name"]: (e.get("use"), e.get("owning_crate"))
        for e in all_types
    }

    def field_idents(entry: dict) -> list[str]:
        idents: list[str] = []
        if entry["kind"] == "struct":
            for f in entry.get("fields", []):
                idents.extend(_idents_from_parsed_type(f["type"]))
        else:  # enum
            for v in entry.get("variants", []):
                for f in v.get("fields", []):
                    idents.extend(_idents_from_parsed_type(f["type"]))
        return idents

    for entry in all_types:
        entry_crate = entry.get("owning_crate")
        file_uses = entry.get("_file_uses", {})
        uses: set[str] = set()
        for ident in field_idents(entry):
            if ident in info_by_name:
                use_path, ident_crate = info_by_name[ident]
                if not use_path:
                    continue
                if ident_crate and entry_crate and ident_crate != entry_crate:
                    # Field type lives in a different crate: convert its
                    # "crate::..." path to the fully-qualified form.
                    if use_path.startswith("crate::"):
                        use_path = ident_crate + use_path[5:]  # len("crate") == 5
                    # If it doesn't start with "crate::" it is already fully
                    # qualified (re-exported through yet another public crate).
                uses.add(use_path)
            elif ident and ident not in _RUST_BUILTINS and ident in file_uses:
                # Not a known contracttype but explicitly imported — use the
                # import path from the file's use statements.
                uses.add(file_uses[ident])
        entry["field_type_uses"] = sorted(uses)



def annotate_resolved_aliases(all_types: list[dict], alias_map: dict[str, str]) -> None:
    """Walk every field / variant-field type dict; when the base type name is a
    known alias, record ``"alias_of"`` and replace ``type``/``params`` with the
    resolved RHS.  Operates recursively on generic parameters so that e.g.
    ``Vec<Point>`` becomes ``Vec<BytesN<64>>``."""

    def resolve(t: dict) -> None:
        name = t.get("type", "")
        if name in alias_map:
            rhs = alias_map[name]
            resolved = parse_type_str(rhs)
            t["alias_of"] = name
            t["type"] = resolved["type"]
            t["params"] = resolved.get("params", [])
        # Recurse into params (possibly just resolved)
        for param in t.get("params", []):
            resolve(param)

    for entry in all_types:
        if entry.get("kind") == "struct":
            for f in entry.get("fields", []):
                resolve(f["type"])
        else:  # enum
            for v in entry.get("variants", []):
                for f in v.get("fields", []):
                    resolve(f["type"])


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    all_types = []
    workspace_type_aliases: dict[str, str] = {}
    for rs_file in sorted(root.rglob("*.rs")):
        rel_parts = rs_file.relative_to(root).parts
        if "target" in rel_parts:
            continue
        src = rs_file.read_text(encoding="utf-8", errors="replace")
        rel = str(rs_file.relative_to(root))
        tree_root = ra.parse(src).root_node
        found = extract_from_source(src, rel, root=tree_root)
        all_types.extend(found)
        # Collect type aliases from every file for later resolution
        workspace_type_aliases.update(parse_type_aliases_tree(tree_root))

    annotate_resolved_aliases(all_types, workspace_type_aliases)
    annotate_recursive(all_types)
    resolve_entry_uses(all_types, root)
    annotate_field_uses(all_types)
    annotate_type_dict_uses(all_types)

    # Strip internal _file_uses before printing
    for entry in all_types:
        entry.pop("_file_uses", None)

    print(json.dumps(all_types, indent=2))


main()
