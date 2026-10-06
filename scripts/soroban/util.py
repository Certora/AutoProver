import copy
from functools import lru_cache
from pathlib import Path

from rust_ast import parse, text, named_children

def src_path_to_module(src_file: str) -> str:
    """
    Convert a file path under src/ to a Rust module path relative to `crate`.

    Examples
        src/contract.rs          -> crate::contract
        src/foo/bar.rs           -> crate::foo::bar
        src/foo/mod.rs           -> crate::foo
        src/lib.rs               -> crate            (the crate root itself)
    """
    rel = Path(src_file)

    parts = list(rel.parts)
    parts = parts[1:]

    # Drop the file extension from the last component
    last = parts[-1]
    stem = Path(last).stem          # e.g. "contract" from "contract.rs"

    if stem in ("lib", "main"):
        # The crate root – no extra segment
        parts = parts[:-1]
    elif stem == "mod":
        # mod.rs represents the parent module
        parts = parts[:-1]
    else:
        parts = parts[:-1] + [stem]

    if not parts:
        return "crate"
    return "crate::" + "::".join(parts)



def split_by_comma(s: str) -> list[str]:
    """Split on commas, respecting < > ( ) [ ] { } nesting.

    `{ }` matters for nested use trees such as
    `a::{b::{c, D}, E}` — without it the inner group is split apart and
    names like `D` are lost."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in '<([{':
            depth += 1
        elif ch in '>)]}':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append(''.join(cur).strip())
    return [p for p in parts if p]

def strip_soroban_sdk(s: str) -> str:
    if s.startswith("soroban_sdk::"):
        return s[13:]
    else:
        return s


# ─── Type strings ─────────────────────────────────────────────────────────────

_TYPE_PREFIX = b'type __T = '


def _type_dict(node) -> dict:
    """Structured dict for one tree-sitter type node (see parse_type_str)."""
    t = node.type
    if t == 'generic_type':
        base = text(node.child_by_field_name('type')).strip()
        args = node.child_by_field_name('type_arguments')
        result: dict = {'type': strip_soroban_sdk(base)}
        params = [_type_dict(a) for a in named_children(args)] if args is not None else []
        if params:
            result['params'] = params
        return result
    if t in ('tuple_type', 'unit_type'):
        params = [_type_dict(c) for c in named_children(node)]
        return {'type': 'tuple', 'params': params} if params else {'type': 'tuple'}
    if t in ('reference_type', 'abstract_type', 'dynamic_type'):
        field = 'trait' if t in ('abstract_type', 'dynamic_type') else 'type'
        inner = node.child_by_field_name(field)
        if inner is not None and inner.type == 'generic_type':
            # `&Vec<u32>`       → {'type': '&Vec', 'params': [...]}
            # `impl Into<i128>` → {'type': 'impl Into', 'params': [...]}
            prefix = node.text[:inner.start_byte - node.start_byte].decode()
            d = _type_dict(inner)
            base = text(inner.child_by_field_name('type')).strip()
            d['type'] = prefix + base
            return d
        return {'type': text(node).strip()}
    if t == 'bracketed_type':          # `<T as Trait>` in a qualified path
        return {'type': text(node).strip()}
    # Leaf types: identifiers, primitives, paths, arrays, slices, `impl Trait`,
    # `dyn Trait`, fn pointers, literals/lifetimes used as generic arguments …
    return {'type': strip_soroban_sdk(text(node).strip())}


@lru_cache(maxsize=4096)
def _parse_type_cached(s: str):
    tree = parse(_TYPE_PREFIX + s.encode('utf-8') + b';')
    root = tree.root_node
    if root.has_error or len(root.named_children) != 1:
        return None
    item = root.named_children[0]
    if item.type != 'type_item':
        return None
    ty = item.child_by_field_name('type')
    if ty is None or ty.end_byte - ty.start_byte != len(s.encode('utf-8')):
        return None
    return _type_dict(ty)


def _parse_type_str_textual(s: str) -> dict:
    """Fallback for strings tree-sitter cannot parse as a type."""
    s = s.strip()
    if not s:
        return {'type': ''}
    if s.startswith('(') and s.endswith(')'):
        parts = split_by_comma(s[1:-1])
        return {'type': 'tuple', 'params': [parse_type_str(p) for p in parts]} if parts else {'type': 'tuple'}
    angle = s.find('<')
    if angle == -1:
        return {'type': strip_soroban_sdk(s)}
    base = s[:angle].strip()
    depth = 0
    for i, ch in enumerate(s[angle:], angle):
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth -= 1
            if depth == 0:
                params = split_by_comma(s[angle + 1:i])
                result: dict = {'type': strip_soroban_sdk(base)}
                if params:
                    result['params'] = [parse_type_str(p) for p in params]
                return result
    return {'type': s}


def parse_type_str(s: str) -> dict:
    """Parse a Rust type string into a structured dict.

    Simple type:   'u64'            → {'type': 'u64'}
    Generic:       'Vec<Address>'   → {'type': 'Vec', 'params': [{'type': 'Address'}]}
    Nested:        'Map<Address, Vec<u64>>'
                                    → {'type': 'Map', 'params': [{'type': 'Address'},
                                                                  {'type': 'Vec', 'params': [{'type': 'u64'}]}]}
    Tuple:         '(u64, Address)' → {'type': 'tuple', 'params': [{'type': 'u64'}, {'type': 'Address'}]}
    Const generic: 'BytesN<32>'     → {'type': 'BytesN', 'params': [{'type': '32'}]}

    Uses tree-sitter-rust; falls back to a bracket-matching scan for strings
    that are not a complete Rust type.
    """
    s = s.strip()
    if not s:
        return {'type': ''}
    parsed = _parse_type_cached(s)
    if parsed is None:
        return _parse_type_str_textual(s)
    return copy.deepcopy(parsed)   # callers mutate the result
