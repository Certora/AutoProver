from pathlib import Path

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
    
def parse_type_str(s: str) -> dict:
    """Parse a Rust type string into a structured dict.

    Simple type:   'u64'            → {'type': 'u64'}
    Generic:       'Vec<Address>'   → {'type': 'Vec', 'params': [{'type': 'Address'}]}
    Nested:        'Map<Address, Vec<u64>>'
                                    → {'type': 'Map', 'params': [{'type': 'Address'},
                                                                  {'type': 'Vec', 'params': [{'type': 'u64'}]}]}
    Tuple:         '(u64, Address)' → {'type': 'tuple', 'params': [{'type': 'u64'}, {'type': 'Address'}]}
    """
    s = s.strip()
    if not s:
        return {'type': ''}

    # Tuple types: (T1, T2, ...)
    if s.startswith('(') and s.endswith(')'):
        inner = s[1:-1]
        parts = split_by_comma(inner)
        return {'type': 'tuple', 'params': [parse_type_str(p) for p in parts]} if parts else {'type': 'tuple'}

    # Find the first '<' — everything before it is the base type name
    angle = s.find('<')
    if angle == -1:
        return {'type': strip_soroban_sdk(s)}

    base = s[:angle].strip()
    # Walk to find the matching '>'
    depth = 0
    for i, ch in enumerate(s[angle:], angle):
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth -= 1
            if depth == 0:
                params_str = s[angle + 1:i]
                params = split_by_comma(params_str)
                result: dict = {'type': strip_soroban_sdk(base)}
                if params:
                    result['params'] = [parse_type_str(p) for p in params]
                return result

    # Malformed — return as-is
    return {'type': s}
