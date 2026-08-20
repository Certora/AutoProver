
#!/usr/bin/env python3
"""
extract_enums.py
----------------
Scans a Rust / Soroban repository and prints every enum type together with
the name and argument types of each of its variants (constructors).
 
Usage:
    python3 extract_enums.py [repo_root] [--soroban-only] [--json]
 
Options:
    --soroban-only   Only report enums annotated with #[contracttype] or
                     #[contracterror]
    --json           Emit machine-readable JSON instead of the default
                     human-readable table
 
If repo_root is omitted the current directory is used.
"""
 
import sys
import re
import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

from util import * 
 
# ─────────────────────────── data model ───────────────────────────────────────
 
@dataclass
class Variant:
    name: str
    kind: str                        # "unit" | "tuple" | "struct"
    # unit  → args = []
    # tuple → args = ["TypeA", "TypeB", ...]
    # struct→ args = [{"field": "x", "ty": "i32"}, ...]
    args: list
    discriminant: Optional[str] = None   # e.g. "32_700" for `= 32_700`
 
 
@dataclass
class EnumDef:
    name: str
    use: str
    attributes: List[str]            # e.g. ["contracttype", "derive(Clone)"]
    variants: List[Variant]
    file: str                        # repo-relative path
    line: int                        # 1-based line of the `enum` keyword
 
 
# ─────────────────────── comment stripping ────────────────────────────────────
 
def strip_comments(src: str) -> str:
    """
    Return a copy of `src` with // line comments and /* */ block comments
    replaced by whitespace (preserving newlines for line-number tracking).
    """
    out: List[str] = []
    i = 0
    n = len(src)
    in_str = False
    block_depth = 0
 
    while i < n:
        c = src[i]
 
        if block_depth:
            if src[i:i+2] == "*/":
                out.append("  ")
                i += 2
                block_depth -= 1
            elif c == "\n":
                out.append("\n")
                i += 1
            else:
                out.append(" ")
                i += 1
            continue
 
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(src[i:i+2])
                i += 2
            elif c == '"':
                in_str = False
                out.append(c)
                i += 1
            else:
                out.append(c)
                i += 1
            continue
 
        if src[i:i+2] == "/*":
            out.append("  ")
            i += 2
            block_depth += 1
        elif src[i:i+2] == "//":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
 
    return "".join(out)
 
 
# ─────────────────────── brace / paren helpers ────────────────────────────────
 
def find_matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """
    Return the index of the `close_ch` that balances `open_ch` at `start`.
    Returns -1 if not found.
    """
    assert text[start] == open_ch, repr(text[start])
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1
 
 
def split_top_commas(text: str) -> List[str]:
    """
    Split `text` at commas that sit at nesting depth 0.
    Depth is tracked for () and {} only; < > are ambiguous in Rust but
    they never appear at depth-0 in enum variant lists.
    """
    parts: List[str] = []
    depth = 0
    buf: List[str] = []
 
    for c in text:
        if c in "({":
            depth += 1
            buf.append(c)
        elif c in ")}":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(c)
 
    part = "".join(buf).strip()
    if part:
        parts.append(part)
 
    return parts
 
 
# ────────────────────────── variant parsing ───────────────────────────────────
 
# Strips leading attribute lines (#[...]) from a variant string
_ATTR_LINE = re.compile(r"^\s*#\[[^\]]*\]\s*")
 
def _strip_variant_attrs(text: str) -> str:
    """Remove all leading #[...] attribute blocks from a variant token."""
    while True:
        m = _ATTR_LINE.match(text)
        if not m:
            break
        text = text[m.end():]
    return text.strip()
 
 
def parse_tuple_args(inner: str) -> List[str]:
    """Parse the inside of a tuple variant's `( )` into a list of type strings."""
    return [t.strip() for t in split_top_commas(inner) if t.strip()]
 
 
def parse_struct_args(inner: str) -> List[dict]:
    """Parse the inside of a struct variant's `{ }` into field dicts."""
    result = []
    for field_str in split_top_commas(inner):
        field_str = field_str.strip()
        if not field_str:
            continue
        # strip visibility modifiers
        field_str = re.sub(r"^pub(\s*\([^)]*\))?\s+", "", field_str)
        m = re.match(r"^(\w+)\s*:\s*(.+)$", field_str, re.DOTALL)
        if m:
            result.append({"field": m.group(1), "ty": m.group(2).strip()})
        else:
            result.append({"field": "?", "ty": field_str})
    return result
 
 
def parse_variant(token: str) -> Optional[Variant]:
    """
    Parse one variant token (everything between commas in the enum body)
    into a Variant.  Returns None for empty / whitespace-only tokens.
    """
    token = _strip_variant_attrs(token)
    if not token:
        return None
 
    # variant name is the first bare identifier
    m = re.match(r"^(\w+)\s*(.*)", token, re.DOTALL)
    if not m:
        return None
    name, rest = m.group(1), m.group(2).strip()
 
    # ── unit ──────────────────────────────────────────────────────────────
    if not rest:
        return Variant(name=name, kind="unit", args=[])
 
    if rest.startswith("="):
        disc = rest[1:].strip()
        return Variant(name=name, kind="unit", args=[], discriminant=disc)
 
    # ── tuple ─────────────────────────────────────────────────────────────
    if rest.startswith("("):
        end = find_matching(rest, 0, "(", ")")
        inner = rest[1:end] if end != -1 else rest[1:]
        return Variant(name=name, kind="tuple", args=parse_tuple_args(inner))
 
    # ── struct ────────────────────────────────────────────────────────────
    if rest.startswith("{"):
        end = find_matching(rest, 0, "{", "}")
        inner = rest[1:end] if end != -1 else rest[1:]
        return Variant(name=name, kind="struct", args=parse_struct_args(inner))
 
    # fall-through: treat as unit (handles `Name /* comment */` etc.)
    return Variant(name=name, kind="unit", args=[])
 
 
# ───────────────────── enum-block finder ──────────────────────────────────────

# Matches: (pub|pub(crate)|pub(super))? enum Name<...>? (where ...)? {
_ENUM_RE = re.compile(
    r"""
    (?:pub\s*(?:\([^)]*\))?\s+)?   # optional visibility
    enum \s+
    (?P<name>\w+)                  # enum name
    (?:\s*<[^{]*?>)?               # optional generic params (non-greedy, no brace)
    \s*
    (?:where\s[^{]*)?              # optional where clause
    \s*\{                          # opening brace
    """,
    re.VERBOSE,
)
 
# Recognise Soroban-relevant outer attributes
_SOROBAN_ATTRS = {"contracttype", "contracterror", "contractimpl"}
 
 
def _collect_attrs_before(clean: str, pos: int) -> List[str]:
    """
    Walk backwards from `pos` in `clean` and collect attribute names from
    consecutive #[...] lines immediately preceding the enum keyword.
    """
    before = clean[:pos]
    lines = before.splitlines()
    attrs: List[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Accept attribute lines and lines with only visibility keywords
        if re.fullmatch(r"pub(\s*\([^)]*\))?", stripped):
            continue
        m = re.match(r"#\[(\w+)", stripped)
        if m:
            attrs.insert(0, m.group(1))
            continue
        # A non-attribute, non-blank line means we've left the attribute block
        break
    return attrs
 
 
def extract_enums(path: Path, repo_root: Path) -> List[EnumDef]:
    """Return all EnumDef instances found in a single .rs file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
 
    clean = strip_comments(raw)
    rel_path = str(path.relative_to(repo_root))
    results: List[EnumDef] = []
 
    search_from = 0
    while True:
        m = _ENUM_RE.search(clean, search_from)
        if not m:
            break
 
        brace_start = m.end() - 1           # the opening `{`
        brace_end   = find_matching(clean, brace_start, "{", "}")
        if brace_end == -1:
            search_from = m.end()
            continue
 
        body      = clean[brace_start + 1: brace_end]
        enum_line = raw[:m.start()].count("\n") + 1   # 1-based
        attrs     = _collect_attrs_before(clean, m.start())
 
        # parse variants
        variants: List[Variant] = []
        for token in split_top_commas(body):
            v = parse_variant(token)
            if v:
                variants.append(v)
 
        results.append(EnumDef(
            name       = m.group("name"),
            use        = src_path_to_module(rel_path),
            attributes = attrs,
            variants   = variants,
            file       = rel_path,
            line       = enum_line,
        ))
 
        search_from = brace_end + 1
 
    return results
 
 
# ─────────────────────────── formatting ──────────────────────────────────────
 
def _fmt_variant(v: Variant) -> str:
    """Return a single-line human-readable representation of a variant."""
    if v.kind == "unit":
        disc = f" = {v.discriminant}" if v.discriminant is not None else ""
        return f"  {v.name}{disc}"
    if v.kind == "tuple":
        args = ", ".join(v.args)
        return f"  {v.name}({args})"
    if v.kind == "struct":
        fields = ", ".join(f"{a['field']}: {a['ty']}" for a in v.args)
        return f"  {v.name} {{ {fields} }}"
    return f"  {v.name}"
 
 
def print_human(enums: List[EnumDef]) -> None:
    for e in enums:
        attr_tag = ""
        if e.attributes:
            attr_tag = "  [" + ", ".join(f"#{a}" for a in e.attributes) + "]"
        print(f"\nenum {e.name}{attr_tag}")
        print(f"     → {e.file}:{e.line}")
        for v in e.variants:
            print(_fmt_variant(v))
    if enums:
        print()
 
 
# ─────────────────────────────── main ────────────────────────────────────────
 
def main() -> None:
    args       = sys.argv[1:]
    soroban    = "--soroban-only" in args
    emit_json  = "--json"         in args
    paths      = [a for a in args if not a.startswith("--")]
 
    repo_root  = Path(paths[0]).resolve() if paths else Path(".").resolve()
 
    if not repo_root.is_dir():
        sys.exit(f"Error: {repo_root} is not a directory")
 
    all_enums: List[EnumDef] = []
    src_root = repo_root / "src" if (repo_root / "src").is_dir() else repo_root
 
    for rs_file in sorted(src_root.rglob("*.rs")):
        all_enums.extend(extract_enums(rs_file, repo_root))
 
    if soroban:
        soroban_attrs = {"contracttype", "contracterror"}
        all_enums = [e for e in all_enums if soroban_attrs & set(e.attributes)]
 
    if not all_enums:
        print("No enums found." + (" (try without --soroban-only)" if soroban else ""))
        return
 
    if emit_json:
        print(json.dumps({ "enums": [asdict(e) for e in all_enums]}, indent=2))
    else:
        print_human(all_enums)
 
 
if __name__ == "__main__":
    main()
