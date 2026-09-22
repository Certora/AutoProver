
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


_TEST_ATTR_MOD_PAT = re.compile(
    r'#\[[^\]]*\btest\b[^\]]*\]\s*(?:pub\s+)?mod\s+\w+\s*\{'
)


_CFG_IF_PAT = re.compile(
    r'\bcfg_if\s*(?:::\s*cfg_if\s*)?\s*!\s*\{'
)


def _strip_brace_blocks(cleaned: str, pattern: re.Pattern) -> str:
    """Remove all macro/mod blocks matched by *pattern* (which must end just
    before the opening '{') from an already-comment-stripped source string."""
    pos = 0
    parts: list[str] = []
    while pos < len(cleaned):
        m = pattern.search(cleaned, pos)
        if not m:
            parts.append(cleaned[pos:])
            break
        parts.append(cleaned[pos:m.start()])
        brace_pos = m.end() - 1   # points to the opening '{'
        close = find_matching_brace(cleaned, brace_pos)
        if close == -1:
            parts.append(cleaned[m.start():])
            break
        pos = close + 1           # skip past the closing '}'
    return ''.join(parts)


def strip_test_blocks(cleaned: str) -> str:
    """Remove test-attributed mod blocks (e.g. #[cfg(test)] mod tests { … })
    from an already-comment-stripped source string so that test-only
    #[contracttype] definitions are not mistaken for real types."""
    return _strip_brace_blocks(cleaned, _TEST_ATTR_MOD_PAT)


def strip_cfg_if_blocks(cleaned: str) -> str:
    """Remove cfg_if::cfg_if! { … } blocks from an already-comment-stripped
    source string so that conditionally-compiled definitions are not picked up."""
    return _strip_brace_blocks(cleaned, _CFG_IF_PAT)


def strip_non_contract_blocks(content: str) -> str:
    """Full cleaning pipeline: strip comments, test mod blocks, and cfg_if blocks."""
    cleaned = strip_comments(content)
    cleaned = strip_test_blocks(cleaned)
    cleaned = strip_cfg_if_blocks(cleaned)
    return cleaned
 
 
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
# Type-alias collection
# ---------------------------------------------------------------------------

def parse_type_aliases(src: str) -> dict[str, str]:
    """Scan *src* for non-parametric `type Alias = RhsType;` declarations and
    return a mapping {alias_name: rhs_type_string}.  Parametric aliases such
    as `type Foo<T> = Bar<T>` are skipped because their RHS depends on the
    type parameter and cannot be substituted without specialisation."""
    src_s = strip_non_contract_blocks(src)
    aliases: dict[str, str] = {}
    for m in re.finditer(r'\btype\s+([A-Za-z_][A-Za-z0-9_]*)\s*', src_s):
        name = m.group(1)
        rest = src_s[m.end():]
        if rest.startswith('<'):
            continue  # parametric alias – skip
        if not rest.startswith('='):
            continue
        # Collect everything up to the terminating ';' at depth 0
        start = m.end() + 1  # character after '='
        depth_a = depth_p = depth_b = 0
        chars: list[str] = []
        i = start
        while i < len(src_s):
            c = src_s[i]
            if c == '<':
                depth_a += 1
            elif c == '>':
                depth_a -= 1
            elif c == '(':
                depth_p += 1
            elif c == ')':
                depth_p -= 1
            elif c == '{':
                depth_b += 1
            elif c == '}':
                depth_b -= 1
            elif c == ';' and depth_a == 0 and depth_p == 0 and depth_b == 0:
                break
            chars.append(c)
            i += 1
        rhs = clean_type(''.join(chars))
        if rhs:
            aliases[name] = rhs
    return aliases

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
        if body[i:].startswith('pub') and (len(body) <= i+3 or not (body[i+3].isalnum() or body[i+3] == '_')):
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
                fields.append({"name": name, "type": parse_type_str(field_type)})
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
            fields.append({"index": idx, "type": parse_type_str(clean_type(part))})
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
# literal field names.  The helpers below detect this case, find the enclosing
# macro definition and its call-site invocation in the same source file, and
# materialise the concrete fields / variants from the invocation arguments.
# ---------------------------------------------------------------------------

def has_macro_metavars(text: str) -> bool:
    """Return True when *text* contains Rust macro metavariable syntax ('$')."""
    return '$' in text


def find_containing_macro_rules(src: str, pos: int):
    """
    If *pos* falls inside a ``macro_rules! name { ... }`` body, return
    ``(macro_name, body_start, body_end)``; otherwise return ``None``.
    """
    for m in re.finditer(r'\bmacro_rules!\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{', src):
        macro_name = m.group(1)
        body_open = m.end() - 1
        body_close = find_matching_brace(src, body_open)
        if body_close == -1:
            continue
        if body_open < pos < body_close:
            return macro_name, body_open, body_close
    return None


def find_macro_invocation_body(src: str, macro_name: str):
    """
    Return the inner text of the first actual call-site
    ``macro_name! { ... }`` (or ``macro_name! ( ... )``) that occurs *outside*
    any ``macro_rules!`` body; otherwise return ``None``.
    """
    macro_bodies: list[tuple[int, int]] = []
    for m in re.finditer(r'\bmacro_rules!\s+[A-Za-z_][A-Za-z0-9_]*\s*\{', src):
        bo = m.end() - 1
        bc = find_matching_brace(src, bo)
        if bc != -1:
            macro_bodies.append((bo, bc))

    def in_macro_body(p: int) -> bool:
        return any(lo < p < hi for lo, hi in macro_bodies)

    pattern = re.compile(r'\b' + re.escape(macro_name) + r'!\s*([{(])')
    for m in pattern.finditer(src):
        if in_macro_body(m.start()):
            continue
        opener = m.group(1)
        start = m.end() - 1
        end = (find_matching_brace(src, start) if opener == '{'
               else find_matching_paren(src, start))
        if end == -1:
            continue
        return src[start + 1 : end]
    return None


def _split_invocation_entries(inv_body: str) -> list[str]:
    """Split a macro invocation body into top-level comma-separated entries.

    Only ``()`` and ``{}`` brackets are tracked for depth because ``<`` and
    ``>`` also appear as comparison / arrow operators (e.g. ``=>``) and would
    corrupt a depth counter.  Generic-type commas (e.g. ``Vec<A, B>``) are
    always inside a ``()`` field list, so they are handled correctly by the
    paren depth alone.
    """
    entries: list[str] = []
    depth_p = depth_b = 0
    cur: list[str] = []
    for c in inv_body:
        if c == '(':   depth_p += 1
        elif c == ')': depth_p -= 1
        elif c == '{': depth_b += 1
        elif c == '}': depth_b -= 1
        elif c == ',' and depth_p == 0 and depth_b == 0:
            entry = ''.join(cur).strip()
            if entry:
                entries.append(entry)
            cur = []
            continue
        cur.append(c)
    last = ''.join(cur).strip()
    if last:
        entries.append(last)
    return entries


def expand_macro_struct_fields(inner: str, struct_pos: int, src: str):
    """
    When *inner* (the raw struct body including outer braces) contains macro
    metavariable repetition like ``$(pub $ttl_field: u64),*``, resolve the
    concrete fields from the enclosing macro's call-site invocation.

    Handles the pattern ``$(pub? $FIELD_VAR: FieldType),*`` where $FIELD_VAR
    is bound to the identifier after ``=>`` in each invocation entry.

    Returns a list of field dicts on success, or ``None``.
    """
    body = inner.strip().lstrip('{').rstrip('}').strip()

    m = re.match(
        r'\$\(\s*(?:pub\s+)?\$([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'
        r'([A-Za-z_][A-Za-z0-9_<>,:&\[\] ]*?)\s*\)\s*,\s*\*',
        body
    )
    if not m:
        return None
    field_type_str = m.group(2).strip()

    ctx = find_containing_macro_rules(src, struct_pos)
    if ctx is None:
        return None
    macro_name = ctx[0]

    inv_body = find_macro_invocation_body(src, macro_name)
    if inv_body is None:
        return None

    entries = _split_invocation_entries(inv_body)
    field_names: list[str] = []
    for entry in entries:
        arrow_m = re.search(r'=>\s*([A-Za-z_][A-Za-z0-9_]*)', entry)
        if arrow_m:
            field_names.append(arrow_m.group(1))

    seen: set[str] = set()
    unique: list[str] = []
    for n in field_names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    if not unique:
        return None

    return [{"name": name, "type": parse_type_str(field_type_str)} for name in unique]


def expand_macro_enum_variants(inner: str, enum_pos: int, src: str):
    """
    When *inner* (the raw enum body including outer braces) contains macro
    metavariable repetition (e.g. ``$($variant),*`` or ``$($ev)*``), resolve
    the concrete variants from the enclosing macro's call-site invocation.

    Each invocation entry must be either:
      ``Variant(F1, F2, ...) => ttl_field``  (tuple variant)
      ``Variant => ttl_field``               (unit variant)

    The ``=> ttl_field`` suffix is stripped before interpreting the variant.

    Returns a list of variant dicts on success, or ``None``.
    """
    body = inner.strip().lstrip('{').rstrip('}').strip()
    if not re.match(r'\$\(', body):
        return None

    ctx = find_containing_macro_rules(src, enum_pos)
    if ctx is None:
        return None
    macro_name = ctx[0]

    inv_body = find_macro_invocation_body(src, macro_name)
    if inv_body is None:
        return None

    entries = _split_invocation_entries(inv_body)
    variants: list[dict] = []
    for entry in entries:
        entry = entry.strip()
        entry = re.sub(r'\s*=>\s*[A-Za-z_][A-Za-z0-9_]*\s*$', '', entry).strip()
        if not entry or entry.startswith('$'):
            continue
        vname_m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)', entry)
        if not vname_m:
            continue
        vname = vname_m.group(1)
        rest = entry[vname_m.end():].strip()
        if rest.startswith('('):
            close = find_matching_paren(rest, 0)
            fields_str = rest[1 : close] if close != -1 else rest[1:]
            fields = parse_tuple_struct_fields('(' + fields_str + ')')
            variants.append({"variant": vname, "kind": "tuple", "fields": fields})
        elif rest.startswith('{'):
            close = find_matching_brace(rest, 0)
            inner_v = rest[: close + 1] if close != -1 else rest
            fields = parse_struct_fields(inner_v)
            variants.append({"variant": vname, "kind": "struct", "fields": fields})
        else:
            variants.append({"variant": vname, "kind": "unit"})

    return variants if variants else None


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------
 
CONTRACTTYPE_RE = re.compile(
    r'#\s*\[\s*contract(type|error|client)\s*(?:\([^)]*\))?\s*\]'
)
 
 
def extract_from_source(src: str, filepath: str) -> list[dict]:
    """Return all contracttype definitions found in *src*."""
    src_stripped = strip_non_contract_blocks(src)
    # Build a map of explicitly-imported names for this file so cross-crate
    # types referenced in fields can be annotated with their use path even
    # when those types aren't themselves contracttypes in the scanned set.
    file_uses = parse_file_uses(src)
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
                       "use": src_path_to_module(filepath) + "::" + name,
                       "_file_uses": file_uses,
                    }
 
        if kind == "struct":
            if body_start < len(src_stripped) and src_stripped[body_start] == '(':
                # tuple struct
                close = find_matching_paren(src_stripped, body_start)
                inner = src_stripped[body_start:close+1]
                fields = parse_tuple_struct_fields(inner)
                entry["struct_kind"] = "tuple"
                entry["fields"] = fields
                entry["macro_expanded"] = False
            elif body_start < len(src_stripped) and src_stripped[body_start] == '{':
                close = find_matching_brace(src_stripped, body_start)
                inner = src_stripped[body_start:close+1]
                if has_macro_metavars(inner):
                    expanded = expand_macro_struct_fields(inner, body_start, src_stripped)
                    fields = expanded if expanded is not None else parse_struct_fields(inner)
                    entry["macro_expanded"] = expanded is not None
                else:
                    fields = parse_struct_fields(inner)
                    entry["macro_expanded"] = False
                entry["struct_kind"] = "named"
                entry["fields"] = fields
            else:
                entry["struct_kind"] = "unit"
                entry["fields"] = []
                entry["macro_expanded"] = False
        elif kind == "enum":
            close = find_matching_brace(src_stripped, body_start)
            inner = src_stripped[body_start:close+1]
            if has_macro_metavars(inner):
                expanded = expand_macro_enum_variants(inner, body_start, src_stripped)
                variants = expanded if expanded is not None else parse_enum_variants(inner)
                entry["macro_expanded"] = expanded is not None
            else:
                variants = parse_enum_variants(inner)
                entry["macro_expanded"] = False
            entry["variants"] = variants
 
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
 
 
def extract_use_items(group_inner: str) -> list:
    """Split a `pub use` brace-group's inner text by top-level commas,
    handling 'as' aliases and nested sub-groups."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in group_inner:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        elif ch == ',' and depth == 0:
            p = ''.join(cur).strip()
            if p:
                parts.append(p)
            cur = []
            continue
        cur.append(ch)
    p = ''.join(cur).strip()
    if p:
        parts.append(p)
 
    items = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '{' in part:
            prefix, _, inner = part.partition('{')
            prefix = prefix.rstrip(': \t')
            inner = inner.rstrip('}').strip()
            for sub in extract_use_items(inner):
                name = f"{prefix}::{sub['name']}" if prefix else sub['name']
                items.append({"name": name, "alias": sub["alias"]})
            continue
        m = re.match(r'^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$', part)
        if m:
            items.append({"name": m.group(1).strip(), "alias": m.group(2)})
        else:
            items.append({"name": part, "alias": None})
    return items
 
 
def find_pub_use_statements_in_source(src: str) -> list:
    """Unrestricted `pub use ...;` statements (excludes `pub(crate) use` /
    `pub(super) use` / `pub(in ...) use`), multi-line/alias/glob aware.
    Returns [{"path_prefix": str|None, "items": [{"name","alias"}]}]."""
    results = []
    for m in re.finditer(r'\bpub[ \t]+use[ \t]+', src):
        start = m.end()
        depth = 0
        end = None
        for i in range(start, len(src)):
            c = src[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == ';' and depth == 0:
                end = i
                break
        if end is None:
            continue
        body = src[start:end].strip()
 
        if '{' in body:
            prefix, _, rest = body.partition('{')
            prefix = prefix.rstrip(': \t')
            inner = rest.rstrip()
            if inner.endswith('}'):
                inner = inner[:-1]
            items = extract_use_items(inner)
            path_prefix = prefix or None
        elif body.endswith('*'):
            path_prefix = body[:-1].rstrip(': \t') or None
            items = [{"name": "*", "alias": None}]
        else:
            alias = None
            path = body
            alias_m = re.match(r'^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$', body)
            if alias_m:
                path, alias = alias_m.group(1).strip(), alias_m.group(2)
            segments = path.split('::')
            last = segments[-1].strip()
            path_prefix = '::'.join(segments[:-1]).strip() or None
            items = [{"name": last, "alias": alias}]
 
        results.append({"path_prefix": path_prefix, "items": items})
    return results
 
 
def _expand_use_into(body: str, prefix: str, out: dict) -> None:
    """Recursively expand a `use` tree segment into *out* ({local_name: full_path}).

    *body* is everything after the leading path prefix (which is in *prefix*).
    Examples (prefix="k2_shared"):
      "Asset"                 → out["Asset"] = "k2_shared::Asset"
      "{Asset, AssetConfig}"  → two entries
      "types::{Foo, Bar}"     → out["Foo"] = "k2_shared::types::Foo" etc.
      "Foo as F"              → out["F"] = "k2_shared::Foo"
      "self"                  → out[last_seg_of_prefix] = prefix
    """
    body = body.strip()
    if not body:
        return

    # Find the first brace group (ignoring angle brackets)
    brace_pos = -1
    angle_depth = 0
    for i, c in enumerate(body):
        if c == '<':
            angle_depth += 1
        elif c == '>':
            angle_depth -= 1
        elif c == '{' and angle_depth == 0:
            brace_pos = i
            break

    if brace_pos == -1:
        # No brace: simple leaf or "a::b::C" path
        # Handle `as` alias
        as_m = re.match(r'^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$', body)
        if as_m:
            path_part, alias = as_m.group(1).strip(), as_m.group(2)
            full = f"{prefix}::{path_part}" if prefix else path_part
            out[alias] = full
        elif body == 'self':
            if prefix:
                out[prefix.split('::')[-1]] = prefix
        elif body != '*':  # ignore globs
            full = f"{prefix}::{body}" if prefix else body
            out[body.split('::')[-1]] = full
        return

    # "path_prefix::{...}" — recurse into each comma-separated item
    path_part = body[:brace_pos].rstrip(': \t')
    new_prefix = f"{prefix}::{path_part}" if path_part and prefix else (path_part or prefix)

    # Find matching close brace
    close = -1
    depth = 0
    for i in range(brace_pos, len(body)):
        if body[i] == '{':
            depth += 1
        elif body[i] == '}':
            depth -= 1
            if depth == 0:
                close = i
                break
    inner = body[brace_pos + 1: close] if close >= 0 else body[brace_pos + 1:]

    # Split inner by top-level commas
    parts: list[str] = []
    d = 0
    cur: list[str] = []
    for c in inner:
        if c == '{':
            d += 1
        elif c == '}':
            d -= 1
        elif c == ',' and d == 0:
            parts.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if cur:
        parts.append(''.join(cur).strip())

    for part in parts:
        part = part.strip()
        if part:
            _expand_use_into(part, new_prefix, out)


def parse_file_uses(src: str) -> dict[str, str]:
    """Parse all `use` statements from *src* (comments already stripped or not),
    returning {local_name: full_qualified_path}.

    Example: `use k2_shared::{Asset, AssetConfig};`
      → {"Asset": "k2_shared::Asset", "AssetConfig": "k2_shared::AssetConfig"}

    Only non-restricted `use` is captured (i.e. NOT `pub(crate) use` / `use(self)` etc.
    but plain `use` and `pub use` both count for our annotation purposes).
    """
    result: dict[str, str] = {}
    src_s = strip_comments(src)
    for m in re.finditer(r'\buse\s+', src_s):
        start = m.end()
        depth = 0
        end = None
        for i in range(start, len(src_s)):
            c = src_s[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == ';' and depth == 0:
                end = i
                break
        if end is None:
            continue
        body = src_s[start:end].strip()
        # Split off any leading "pub" that got pulled in (e.g. if `pub use`)
        # — the regex anchor on \buse\s+ already positions us at the path start,
        # but `re.finditer` on `\buse\s+` may fire on the `use` inside `pub use`
        # so the body starts directly with the path.
        _expand_use_into(body, "", result)
    return result


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
    (Trimmed from the equivalent scanner in soroban_use_decls.py -- this
    version only needs plain item declarations, not Soroban contract
    attributes.)
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
 
    def scan_file(rs_file: Path, module_path: list, is_public_context: bool) -> None:
        rs_file = rs_file.resolve()
        key = (rs_file, tuple(module_path))
        if key in visited:
            return
        visited.add(key)
 
        try:
            src = strip_comments(rs_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return
 
        lines = src.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
 
            m = re.match(r'pub\s+mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;', line)
            if m:
                mod_name = m.group(1)
                if is_public_context:
                    record(mod_name, module_path)
                mod_file = resolve_module_file(rs_file, mod_name)
                if mod_file:
                    scan_file(mod_file, module_path + [mod_name], is_public_context=is_public_context)
 
            m = re.match(r'pub\s+mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{', line)
            if m and is_public_context:
                record(m.group(1), module_path)
 
            if is_public_context:
                m = re.match(
                    r'pub\s*(?:\([^)]*\)\s*)?'
                    r'(struct|enum|trait|type|fn|const|static)\s+'
                    r'([A-Za-z_][A-Za-z0-9_]*)',
                    line
                )
                if m:
                    record(m.group(2), module_path)
 
        if is_public_context:
            for stmt in find_pub_use_statements_in_source(src):
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
        found = extract_from_source(src, rel)
        all_types.extend(found)
        # Collect type aliases from every file for later resolution
        workspace_type_aliases.update(parse_type_aliases(src))

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
