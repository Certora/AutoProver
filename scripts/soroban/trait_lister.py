#!/usr/bin/env python3
"""
rust_trait_lister.py
====================
List all Rust trait definitions in a GitHub repository together with their
fully qualified module paths (FQNs).
 
FQN format:  crate_name::module::submodule::TraitName
             (hyphens in crate names are replaced with underscores, following
              Rust's own convention)
 
The script:
  • Discovers the workspace root even when it is not at the repo root
    (e.g. contracts/Cargo.toml).
  • Expands glob workspace members (contracts/*, packages/*, …) via
    Cargo.lock when no sub-workspace Cargo.toml is present.
  • Follows  mod name;  declarations (file-based modules) with BFS.
  • Tracks inline  mod name { … }  blocks for additional path nesting.
  • Reports start + end positions for every trait and its methods.
 
Usage:
    python rust_trait_lister.py <github-url> [options]
 
Examples:
    python rust_trait_lister.py https://github.com/zenith-protocols/soroban-vault
    python rust_trait_lister.py https://github.com/theahaco/stellar-contracts-OZ \\
        --format text -v
    python rust_trait_lister.py https://github.com/kalepail/KALE-sc \\
        --all -o traits.json
 
Options:
    --branch/-b BRANCH   Branch to scan (default: auto-detected)
    --output/-o FILE     Write output to FILE instead of stdout
    --format/-f json|text   Output format (default: json)
    --all                Include private (non-pub) traits (default: pub only)
    --verbose/-v         Print crawl progress to stderr
 
Requires:  requests  (pip install requests)
"""
 
import argparse
import json
import re
import sys
import textwrap
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import urlparse
 
try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' not installed.  Run: pip install requests")
 
 
# ---------------------------------------------------------------------------
# GitHub raw-content helpers
# ---------------------------------------------------------------------------
 
RAW_BASE = "https://raw.githubusercontent.com"
 
_session = requests.Session()
_session.headers.update({"User-Agent": "rust-trait-lister/1.0"})
 
 
def _parse_github_url(url: str) -> tuple[str, str]:
    p = urlparse(url.rstrip("/"))
    parts = p.path.strip("/").split("/")
    if len(parts) < 2:
        sys.exit(f"Cannot parse GitHub URL: {url}")
    return parts[0], parts[1].removesuffix(".git")
 
 
def _fetch_raw(owner: str, repo: str, branch: str, path: str) -> Optional[str]:
    url = f"{RAW_BASE}/{owner}/{repo}/{branch}/{path.lstrip('/')}"
    try:
        r = _session.get(url, timeout=15)
    except requests.RequestException:
        return None
    return r.text if r.status_code == 200 else None
 
 
def _default_branch(owner: str, repo: str) -> str:
    for b in ("main", "master", "develop"):
        r = _session.head(f"{RAW_BASE}/{owner}/{repo}/{b}/Cargo.toml", timeout=10)
        if r.status_code == 200:
            return b
    sys.exit("Cannot determine default branch — try --branch <name>")
 
 
# ---------------------------------------------------------------------------
# Cargo.toml / workspace helpers
# ---------------------------------------------------------------------------
 
_WORKSPACE_SUBDIRS = ("contracts", "packages", "crates", "src")
 
 
def _is_workspace(text: str) -> bool:
    return bool(re.search(r'^\[workspace\]', text, re.MULTILINE))
 
 
def _parse_package_name(text: str) -> str:
    m = re.search(r'^\[package\].*?^name\s*=\s*"([^"]+)"',
                  text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else "unknown"
 
 
def _parse_workspace_members(text: str) -> list[str]:
    try:
        import toml as _toml  # type: ignore
        data = _toml.loads(text)
        return [m for m in data.get("workspace", {}).get("members", [])
                if isinstance(m, str)]
    except Exception:
        pass
    ws = re.search(r'\[workspace\](.*?)(?=\n\[|\Z)', text, re.DOTALL)
    if not ws:
        return []
    m = re.search(r'members\s*=\s*\[(.*?)\]', ws.group(1), re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))
 
 
def _find_cargo_toml(owner: str, repo: str, branch: str) -> tuple[Optional[str], str]:
    """
    Try repo root first, then common subdirectories.
    Returns (cargo_text, workspace_root) where workspace_root is the
    repo-relative prefix to prepend to member paths (e.g. "" or "contracts").
    """
    text = _fetch_raw(owner, repo, branch, "Cargo.toml")
    if text:
        return text, ""
    for sub in _WORKSPACE_SUBDIRS:
        text = _fetch_raw(owner, repo, branch, f"{sub}/Cargo.toml")
        if text:
            return text, sub
    return None, ""
 
 
def _local_package_names_from_lock(
    owner: str, repo: str, branch: str, lock_root: str = ""
) -> list[str]:
    """Local packages from Cargo.lock (entries without a 'source' field)."""
    path = f"{lock_root}/Cargo.lock" if lock_root else "Cargo.lock"
    text = _fetch_raw(owner, repo, branch, path)
    if not text:
        return []
    names: list[str] = []
    for block in re.split(r'\n\[\[package\]\]', text):
        if re.search(r'\bsource\s*=', block):
            continue
        m = re.search(r'\bname\s*=\s*"([^"]+)"', block)
        if m:
            names.append(m.group(1))
    return names
 
 
def _expand_glob_member(
    owner: str, repo: str, branch: str, pattern: str, lock_root: str = ""
) -> list[str]:
    if "*" not in pattern and "?" not in pattern:
        return [pattern]
    parent = pattern.split("/*")[0].split("/?")[0]
 
    # Strategy 1: parent has its own sub-workspace Cargo.toml
    parent_cargo = _fetch_raw(owner, repo, branch, f"{parent}/Cargo.toml")
    if parent_cargo:
        nested = _parse_workspace_members(parent_cargo)
        results = [nm if "/" in nm else f"{parent}/{nm}" for nm in nested]
        if results:
            return results
 
    # Strategy 2: probe each local name from Cargo.lock
    results = []
    for name in _local_package_names_from_lock(owner, repo, branch, lock_root):
        candidate = f"{parent}/{name}"
        if _fetch_raw(owner, repo, branch, f"{candidate}/Cargo.toml"):
            results.append(candidate)
    return results
 
 
def _resolve_members(
    owner: str, repo: str, branch: str,
    raw_members: list[str], lock_root: str = ""
) -> list[str]:
    result: list[str] = []
    for m in raw_members:
        if "*" in m or "?" in m:
            result.extend(_expand_glob_member(owner, repo, branch, m, lock_root))
        else:
            result.append(m)
    seen: set[str] = set()
    return [p for p in result if not (p in seen or seen.add(p))]  # type: ignore
 
 
# ---------------------------------------------------------------------------
# Rust file BFS  (follows mod name; declarations)
# ---------------------------------------------------------------------------
 
_MOD_DECL_RE = re.compile(r'(?:pub\s+)?mod\s+(\w+)\s*;')
 
 
def _child_dir_of(file_path: str) -> str:
    base = file_path.rsplit("/", 1)[0]
    stem = PurePosixPath(file_path).stem
    return base if stem in ("mod", "lib", "main") else f"{base}/{stem}"
 
 
def _list_rust_files(
    owner: str, repo: str, branch: str,
    package_path: str, verbose: bool = False
) -> dict[str, str]:
    """BFS from lib.rs/main.rs following mod declarations. Returns {path: source}."""
    src_root = f"{package_path}/src" if package_path else "src"
    sources: dict[str, str] = {}
    queue: deque[str] = deque()
 
    def try_enqueue(path: str) -> bool:
        if path in sources:
            return False
        src = _fetch_raw(owner, repo, branch, path)
        if src is None:
            return False
        sources[path] = src
        queue.append(path)
        if verbose:
            print(f"    found: …/{'/'.join(path.split('/')[-3:])}", file=sys.stderr)
        return True
 
    for entry in (f"{src_root}/lib.rs", f"{src_root}/main.rs"):
        try_enqueue(entry)
 
    while queue:
        fp = queue.popleft()
        child_dir = _child_dir_of(fp)
        for m in _MOD_DECL_RE.finditer(sources[fp]):
            mod_name = m.group(1)
            if mod_name == "test":
                continue
            for candidate in (
                f"{child_dir}/{mod_name}/mod.rs",
                f"{child_dir}/{mod_name}.rs",
            ):
                if try_enqueue(candidate):
                    break
 
    return sources
 
 
# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
 
@dataclass
class Pos:
    """1-based source position."""
    line: int
    col: int
 
 
@dataclass
class Span:
    """Inclusive source range [start, end], both 1-based."""
    start: Pos
    end: Pos
 
 
@dataclass
class MethodSig:
    name: str
    params: str   # raw text inside the outer ( )
    ret: str      # return type, or ""
    span: Optional[Span] = None
 
 
@dataclass
class TraitDef:
    fqn: str                       # fully qualified name
    name: str                      # short name
    crate: str                     # normalised crate name
    module: str                    # module path (including crate, excluding trait)
    visibility: str                # "pub", "pub(crate)", … or "" for private
    annotations: list[str]         # immediately-preceding #[…] attributes
    methods: list[MethodSig]       # method signatures from the trait body
    source_file: str               # repo-relative file path
    span: Optional[Span] = None    # location of the full `pub trait … { }`
 
 
@dataclass
class ImplSite:
    """Where an external trait is implemented inside this repo."""
    struct_name: str    # the implementing type
    source_file: str    # repo-relative path
    line: int           # 1-based line of the `impl … for` statement
 
 
@dataclass
class ImportedTrait:
    """An externally-defined trait that is used/implemented inside this repo."""
    fqn: str                          # e.g. "soroban_sdk::token::TokenInterface"
    name: str                         # short name, e.g. "TokenInterface"
    external_crate: str               # first path segment, e.g. "soroban_sdk"
    implementations: list[ImplSite] = field(default_factory=list)
    imported_in: list[str]            = field(default_factory=list)  # files with a `use`
 
 
# ---------------------------------------------------------------------------
# Use-statement parser  (handles nested brace trees)
# ---------------------------------------------------------------------------
 
def _brace_inner(text: str) -> str:
    """Return everything between the first matching { } pair."""
    depth, end = 0, 0
    for i, ch in enumerate(text):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    return text[1:end]
 
 
def _comma_split_depth0(text: str) -> list[str]:
    """Split *text* by ',' only at brace depth 0."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
            current.append(ch)
        elif ch == '}':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return [p for p in parts if p]
 
 
def _expand_use_tree(prefix: str, tree: str, out: dict) -> None:
    """
    Recursively expand a Rust use-tree fragment into {alias: full_path} entries.
 
    prefix: accumulated path so far (e.g. "soroban_sdk::token")
    tree:   remaining spec   (e.g. "TokenInterface", "{X,Y}", "bar::Baz", "*")
    out:    dict being populated
    """
    tree = tree.strip()
    if not tree or tree == '*':
        return
 
    # Group form: {A, B::C, D::{E, F}}
    if tree.startswith('{'):
        for part in _comma_split_depth0(_brace_inner(tree)):
            _expand_use_tree(prefix, part, out)
        return
 
    # Path form: seg::rest
    if '::' in tree:
        seg, rest = tree.split('::', 1)
        new_prefix = f"{prefix}::{seg.strip()}" if prefix else seg.strip()
        _expand_use_tree(new_prefix, rest, out)
        return
 
    # Terminal: "Name" or "Name as Alias"
    if ' as ' in tree:
        name, alias = tree.split(' as ', 1)
        name, alias = name.strip(), alias.strip()
        full = f"{prefix}::{name}" if prefix else name
        out[alias] = full
    else:
        full = f"{prefix}::{tree}" if prefix else tree
        out[tree] = full
 
 
def _parse_use_imports(source: str) -> dict[str, str]:
    """
    Scan all `use` / `pub use` statements and return {alias_or_name: full_path}.
    Handles multi-line statements and nested brace trees.
    """
    result: dict[str, str] = {}
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith('//') or stripped.startswith('/*'):
            i += 1
            continue
        if re.match(r'(?:pub\s+)?use\s+', stripped):
            # Collect continuation lines until we hit a ';'
            parts = [stripped]
            while ';' not in ''.join(parts) and i + 1 < len(lines):
                i += 1
                parts.append(lines[i].strip())
            stmt = ' '.join(parts)
            m = re.match(r'(?:pub\s+)?use\s+(.*?)\s*;', stmt, re.DOTALL)
            if m:
                _expand_use_tree('', m.group(1).strip(), result)
        i += 1
    return result
 
 
# ---------------------------------------------------------------------------
# impl-for reference extractor
# ---------------------------------------------------------------------------
 
# Match:  impl [<generics>]  TraitPath[<generics>]  for  StructName
_IMPL_FOR_RE = re.compile(
    r'\bimpl\s*(?:<[^>]*>)?\s*'        # impl + optional type params
    r'([\w][\w:]*(?:\s*<[^>]*>)?)'     # trait name / path (optional generics)
    r'\s+for\s+([\w]+)'                # for StructName
)
 
 
def _extract_impl_trait_refs(source: str) -> list[tuple[str, str, int]]:
    """
    Return list of (trait_ref, struct_name, line_1based) for every
    `impl TraitRef for Struct` found in *source*.
    trait_ref is stripped of generic parameters.
    """
    results: list[tuple[str, str, int]] = []
    for m in _IMPL_FOR_RE.finditer(source):
        raw = m.group(1).strip()
        # Strip trailing generic params
        trait_ref = raw.split('<')[0].strip()
        struct_name = m.group(2).strip()
        line_num = source[: m.start()].count('\n') + 1
        results.append((trait_ref, struct_name, line_num))
    return results
 
 
# ---------------------------------------------------------------------------
# Imported-trait collector
# ---------------------------------------------------------------------------
 
def _collect_imported_traits(
    sources: dict[str, str],
    crate_name: str,
    verbose: bool = False,
) -> list[ImportedTrait]:
    """
    Scan *sources* (a {file_path: source} dict from _list_rust_files) and
    return every externally-defined trait that is implemented inside this package.
 
    Detection strategy:
      1. Parse every `use` statement to build a per-file name→FQN map.
      2. Find every `impl X for Y` occurrence in each file.
      3. Resolve X through the use map (or treat it as a full path if it
         already contains '::'). Filter out anything that maps to the local
         crate.
      4. Aggregate by FQN.
    """
    crate_norm = crate_name.replace('-', '_')
    local_prefixes = ('crate::', 'self::', 'super::')
 
    # Build a set of trait names that are DEFINED locally so we can exclude them
    _local_def_re = re.compile(r'\btrait\s+(\w+)')
    local_trait_names: set[str] = set()
    for src in sources.values():
        for m in _local_def_re.finditer(src):
            local_trait_names.add(m.group(1))
 
    # Per-file maps
    file_use_maps: dict[str, dict[str, str]] = {}
    file_impl_refs: dict[str, list[tuple[str, str, int]]] = {}
    for fp, src in sources.items():
        file_use_maps[fp] = _parse_use_imports(src)
        file_impl_refs[fp] = _extract_impl_trait_refs(src)
 
    # Aggregate: fqn → ImportedTrait
    imported: dict[str, ImportedTrait] = {}
 
    for fp, impl_refs in file_impl_refs.items():
        use_map = file_use_maps[fp]
 
        for trait_ref, struct_name, line_num in impl_refs:
            # --- Resolve to a full FQN ---
            if '::' in trait_ref:
                # Already a qualified path (e.g. "soroban_sdk::token::Foo")
                fqn = trait_ref
            elif trait_ref in use_map:
                fqn = use_map[trait_ref]
            else:
                # Not importable / locally defined → skip
                continue
 
            # --- Filter local references ---
            if any(fqn.startswith(p) for p in local_prefixes):
                continue
            first_seg = fqn.split('::')[0]
            if first_seg == crate_norm:
                continue
 
            # --- Filter traits that are defined in this crate ---
            short_name = fqn.split('::')[-1]
            if short_name in local_trait_names:
                continue
 
            # --- Record ---
            if fqn not in imported:
                imported[fqn] = ImportedTrait(
                    fqn=fqn,
                    name=short_name,
                    external_crate=first_seg,
                )
 
            entry = imported[fqn]
            site = ImplSite(struct_name=struct_name, source_file=fp, line=line_num)
            if not any(s.source_file == fp and s.line == line_num
                       for s in entry.implementations):
                entry.implementations.append(site)
 
    # Fill imported_in lists
    for fp, use_map in file_use_maps.items():
        for _name, fqn in use_map.items():
            if fqn in imported and fp not in imported[fqn].imported_in:
                imported[fqn].imported_in.append(fp)
 
    result = sorted(imported.values(), key=lambda t: t.fqn)
    if verbose and result:
        names = [t.name for t in result]
        print(f"    imported traits: {names}", file=sys.stderr)
    return result
 
 
# ---------------------------------------------------------------------------
# Source parser — trait extraction with inline-mod tracking
# ---------------------------------------------------------------------------
 
_TRAIT_RE   = re.compile(r'\b(pub(?:\s*\([^)]*\))?\s+)?trait\s+(\w+)')
_MOD_OPEN_RE = re.compile(r'\bmod\s+(\w+)\s*\{')
_ATTR_RE    = re.compile(r'^\s*#\[([^\]]+)\]')
_COMMENT_RE = re.compile(r'^\s*//')
_FN_RE      = re.compile(
    r'\bfn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?:->\s*([^{;]+?))?[{;]'
)
 
 
def _file_module_segments(file_path: str, pkg_path: str) -> list[str]:
    """
    Derive module-path segments from a repo-relative file path.
 
    pkg_path: e.g. "packages/vault" or "" for single-crate root.
    Returns the segment list BELOW the crate root, e.g. ["foo", "bar"]
    for src/foo/bar.rs.  Returns [] for src/lib.rs.
    """
    src_prefix = f"{pkg_path}/src/" if pkg_path else "src/"
    if not file_path.startswith(src_prefix):
        return []
    rel = file_path[len(src_prefix):].removesuffix(".rs")
    parts = rel.split("/")
    # Strip terminal "lib", "mod", "main" — those represent the module root
    if parts and parts[-1] in ("lib", "mod", "main"):
        parts = parts[:-1]
    return [p for p in parts if p]
 
 
def _block_end(lines: list[str], start: int) -> tuple[int, int]:
    """
    Starting at line `start`, find the closing '}' of the first brace-block.
    Returns (end_line_1based, end_col_1based).
    """
    depth = 0
    for i in range(start, len(lines)):
        for k, ch in enumerate(lines[i]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i + 1, k + 1
    return start + 1, 1  # fallback
 
 
def _method_end(lines: list[str], fn_line: int, fn_col0: int, body_end: int) -> tuple[int, int]:
    """
    Scan forward from (fn_line, fn_col0) (0-based) to find the closing ';' or '}'
    of a method declaration/body. Returns (end_line_1based, end_col_1based).
    """
    depth = 0
    for i in range(fn_line, min(body_end, len(lines))):
        k0 = fn_col0 if i == fn_line else 0
        for k in range(k0, len(lines[i])):
            ch = lines[i][k]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i + 1, k + 1
            elif ch == ';' and depth == 0:
                return i + 1, k + 1
    return fn_line + 1, fn_col0 + 1  # fallback
 
 
def _extract_methods(body_flat: str, lines: list[str],
                     body_start: int, body_end: int) -> list[MethodSig]:
    """Extract method signatures from a flattened trait body."""
    methods: list[MethodSig] = []
    _FN_PAT = re.compile(
        r'\bfn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?:->\s*([^{;]+?))?[{;]'
    )
    fn_name_pat = re.compile(r'\bfn\s+(\w+)\b')
 
    for fm in _FN_PAT.finditer(body_flat):
        name = fm.group(1)
        params = re.sub(r'\s+', ' ', (fm.group(2) or '').strip())
        ret = re.sub(r'\s+', ' ', (fm.group(3) or '').strip().rstrip('{').strip())
 
        # Locate this fn in the original lines for span info
        pat = re.compile(rf'\bfn\s+{re.escape(name)}\b')
        fn_line_idx = body_start
        fn_col0 = 0
        for j in range(body_start, min(body_end, len(lines))):
            m = pat.search(lines[j])
            if m:
                fn_line_idx = j
                fn_col0 = m.start()
                break
 
        end_line, end_col = _method_end(lines, fn_line_idx, fn_col0, body_end)
        methods.append(MethodSig(
            name=name, params=params, ret=ret,
            span=Span(
                Pos(fn_line_idx + 1, fn_col0 + 1),
                Pos(end_line, end_col),
            ),
        ))
    return methods
 
 
def _extract_traits(
    source: str,
    crate_name: str,
    file_segments: list[str],
    source_file: str,
    pub_only: bool = True,
) -> list[TraitDef]:
    """
    Extract all trait definitions from one Rust source file.
 
    crate_name:    as it appears in Cargo.toml (hyphens allowed)
    file_segments: from _file_module_segments()
    pub_only:      if True, skip private traits
    """
    crate_norm = crate_name.replace("-", "_")
    base_path  = [crate_norm] + file_segments
 
    lines   = source.splitlines()
    n       = len(lines)
 
    depth     = 0
    mod_stack: list[tuple[int, str]] = []   # (open_depth, mod_name)
    pending_attrs: list[str] = []
    results:  list[TraitDef] = []
 
    def current_module() -> list[str]:
        return base_path + [name for _, name in mod_stack]
 
    i = 0
    while i < n:
        line    = lines[i]
        stripped = line.strip()
 
        # ── Skip pure comment lines (but keep /// doc — they're just noise here)
        if _COMMENT_RE.match(line):
            i += 1
            continue
 
        # ── Attribute lines (#[…]) ─────────────────────────────────────────
        attr_m = _ATTR_RE.match(line)
        if attr_m:
            # Collapse multi-line attributes to a single string
            attr_text = attr_m.group(1).strip()
            pending_attrs.append(attr_text)
            i += 1
            continue
 
        # ── Blank lines — reset pending attrs (attrs must be adjacent) ──────
        if not stripped:
            pending_attrs = []
            i += 1
            continue
 
        # ── Content line ────────────────────────────────────────────────────
 
        # Detect inline mod open BEFORE counting braces so we can compute
        # the correct depth-at-open for the new module scope.
        mod_m = _MOD_OPEN_RE.search(line)
        if mod_m:
            prefix      = line[:mod_m.end()]          # up to and including {
            opens_here  = prefix.count('{')
            closes_here = prefix.count('}')
            open_depth  = depth + opens_here - closes_here
            mod_stack.append((open_depth, mod_m.group(1)))
 
        # Detect trait definition
        trait_m = _TRAIT_RE.search(line)
        if trait_m:
            vis        = (trait_m.group(1) or '').strip()
            trait_name = trait_m.group(2)
 
            if not pub_only or vis.startswith('pub'):
                cur_path   = current_module()
                fqn        = '::'.join(cur_path + [trait_name])
                start_line = i + 1
                start_col  = trait_m.start() + 1
 
                # Find body end
                end_line, end_col = _block_end(lines, i)
                body_end = end_line  # 1-based line index (exclusive next line)
 
                # Flatten body for method extraction
                body_chars: list[str] = []
                bdepth = 0
                for j in range(i, min(end_line, n)):
                    for k, ch in enumerate(lines[j]):
                        if ch == '{':
                            bdepth += 1
                            if bdepth == 1:
                                continue   # skip opening {
                            body_chars.append(ch)
                        elif ch == '}':
                            bdepth -= 1
                            if bdepth == 0:
                                break
                            body_chars.append(ch)
                        else:
                            if bdepth > 0:
                                body_chars.append(ch)
                    else:
                        if bdepth > 0:
                            body_chars.append('\n')
                        continue
                    break
                body_flat = re.sub(r'\s+', ' ', ''.join(body_chars))
 
                methods = _extract_methods(body_flat, lines, i, body_end)
 
                results.append(TraitDef(
                    fqn        = fqn,
                    name       = trait_name,
                    crate      = crate_norm,
                    module     = '::'.join(cur_path),
                    visibility = vis,
                    annotations= list(pending_attrs),
                    methods    = methods,
                    source_file= source_file,
                    span       = Span(
                        Pos(start_line, start_col),
                        Pos(end_line, end_col),
                    ),
                ))
 
        # ── Count braces and maintain mod_stack ────────────────────────────
        for ch in line:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                while mod_stack and depth < mod_stack[-1][0]:
                    mod_stack.pop()
 
        pending_attrs = []
        i += 1
 
    return results
 
 
# ---------------------------------------------------------------------------
# Package analysis
# ---------------------------------------------------------------------------
 
def _analyse_package(
    owner: str, repo: str, branch: str,
    pkg_path: str, pkg_name: str,
    pub_only: bool, verbose: bool,
) -> tuple[list[TraitDef], list[ImportedTrait]]:
    sources = _list_rust_files(owner, repo, branch, pkg_path, verbose)
    if verbose:
        print(f"  [{pkg_path or 'root'}] {len(sources)} .rs files crawled",
              file=sys.stderr)
    traits: list[TraitDef] = []
    for rs_path, src in sources.items():
        segs = _file_module_segments(rs_path, pkg_path)
        found = _extract_traits(src, pkg_name, segs, rs_path, pub_only)
        if found and verbose:
            print(f"    {rs_path}: {[t.name for t in found]}", file=sys.stderr)
        traits.extend(found)
    imported = _collect_imported_traits(sources, pkg_name, verbose)
    return traits, imported
 
 
# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------
 
def _pos_dict(p: Pos) -> dict:
    return {"line": p.line, "col": p.col}
 
 
def _span_dict(s: Optional[Span]) -> Optional[dict]:
    if s is None:
        return None
    return {"start": _pos_dict(s.start), "end": _pos_dict(s.end)}
 
 
def _method_dict(m: MethodSig) -> dict:
    params = []
    for part in m.params.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            pname, ptype = part.split(":", 1)
            params.append({"name": pname.strip(), "type": ptype.strip()})
        else:
            params.append({"name": part, "type": ""})
    return {
        "name":    m.name,
        "params":  params,
        "returns": m.ret or "()",
        "range":   _span_dict(m.span),
    }
 
 
def _trait_dict(t: TraitDef) -> dict:
    return {
        "fqn":         t.fqn,
        "name":        t.name,
        "crate":       t.crate,
        "module":      t.module,
        "visibility":  t.visibility or "private",
        "annotations": t.annotations,
        "methods":     [_method_dict(m) for m in t.methods],
        "source_file": t.source_file,
        "range":       _span_dict(t.span),
    }
 
 
def _impl_site_dict(s: ImplSite) -> dict:
    return {"struct": s.struct_name, "source_file": s.source_file, "line": s.line}
 
 
def _imported_trait_dict(t: ImportedTrait) -> dict:
    return {
        "fqn":             t.fqn,
        "name":            t.name,
        "external_crate":  t.external_crate,
        "implementations": [_impl_site_dict(s) for s in t.implementations],
        "imported_in":     t.imported_in,
    }
 
 
def _generate_json(
    owner: str, repo: str, branch: str,
    traits: list[TraitDef],
    imported: list[ImportedTrait],
) -> str:
    return json.dumps({
        "repository":      f"https://github.com/{owner}/{repo}",
        "branch":          branch,
        "generator":       "rust_trait_lister.py",
        "defined_count":   len(traits),
        "imported_count":  len(imported),
        "traits":          [_trait_dict(t) for t in traits],
        "imported_traits": [_imported_trait_dict(t) for t in imported],
    }, indent=2)
 
 
def _generate_text(
    traits: list[TraitDef],
    imported: list[ImportedTrait],
) -> str:
    lines: list[str] = []
 
    if traits:
        lines.append("=== Defined traits ===")
        for t in traits:
            vis = f"[{t.visibility}] " if t.visibility else "[private] "
            loc = ""
            if t.span:
                loc = f"  ({t.source_file}:{t.span.start.line}:{t.span.start.col})"
            lines.append(f"{vis}{t.fqn}{loc}")
    else:
        lines.append("(no locally defined traits found)")
 
    lines.append("")
 
    if imported:
        lines.append("=== Imported / external traits (implemented here) ===")
        for t in imported:
            impls = ", ".join(
                f"{s.struct_name} ({s.source_file}:{s.line})"
                for s in t.implementations
            )
            lines.append(f"[imported] {t.fqn}  →  impl for: {impls}")
    else:
        lines.append("(no externally-defined traits implemented here)")
 
    return "\n".join(lines)
 
 
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
 
def main() -> None:
    parser = argparse.ArgumentParser(
        description="List all Rust trait definitions with fully qualified names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python rust_trait_lister.py https://github.com/owner/repo
              python rust_trait_lister.py https://github.com/owner/repo \\
                  --format text --all -v
              python rust_trait_lister.py https://github.com/owner/repo \\
                  --branch develop -o traits.json
        """),
    )
    parser.add_argument("url",           help="GitHub repository URL")
    parser.add_argument("--branch", "-b", default=None,
                        help="Branch (default: auto-detected)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file (default: stdout)")
    parser.add_argument("--format", "-f", choices=["json", "text"],
                        default="json",
                        help="Output format: json (default) or text")
    parser.add_argument("--all",  action="store_true",
                        help="Include private (non-pub) traits")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print crawl progress to stderr")
    args = parser.parse_args()
 
    owner, repo = _parse_github_url(args.url)
    branch      = args.branch or _default_branch(owner, repo)
    pub_only    = not args.all
 
    if args.verbose:
        print(f"Repository: {owner}/{repo}  branch: {branch}", file=sys.stderr)
 
    # ── Find workspace / package root ───────────────────────────────────────
    cargo_text, workspace_root = _find_cargo_toml(owner, repo, branch)
    if cargo_text is None:
        sys.exit(
            "ERROR: Could not find Cargo.toml at the repo root or in: "
            + ", ".join(_WORKSPACE_SUBDIRS)
        )
    if args.verbose and workspace_root:
        print(f"Workspace root: {workspace_root}/", file=sys.stderr)
 
    all_traits: list[TraitDef] = []
    all_imported_raw: list[ImportedTrait] = []
 
    if _is_workspace(cargo_text):
        # ── Multi-crate workspace ────────────────────────────────────────────
        raw_members = _parse_workspace_members(cargo_text)
        if workspace_root:
            raw_members = [f"{workspace_root}/{m}" for m in raw_members]
        all_members = _resolve_members(owner, repo, branch, raw_members,
                                       lock_root=workspace_root)
        if not all_members:
            sys.exit("No packages to scan.")
        if args.verbose:
            print(f"Scanning {len(all_members)} package(s): {all_members}",
                  file=sys.stderr)
        for pkg_path in all_members:
            pkg_name = PurePosixPath(pkg_path).name
            if args.verbose:
                print(f"\nPackage: {pkg_path}", file=sys.stderr)
            traits, imported = _analyse_package(
                owner, repo, branch, pkg_path, pkg_name, pub_only, args.verbose
            )
            all_traits.extend(traits)
            all_imported_raw.extend(imported)
    else:
        # ── Single-crate ─────────────────────────────────────────────────────
        pkg_name = _parse_package_name(cargo_text)
        if args.verbose:
            print(f"Single-crate repo: {pkg_name}", file=sys.stderr)
        # For a non-root single crate, pkg_path is the workspace_root itself
        # (the Cargo.toml there has [package], not [workspace])
        pkg_path = workspace_root  # "" for repo root, else e.g. "contracts"
        traits, imported = _analyse_package(
            owner, repo, branch, pkg_path, pkg_name, pub_only, args.verbose
        )
        all_traits.extend(traits)
        all_imported_raw.extend(imported)
 
    # Sort by FQN for deterministic output
    all_traits.sort(key=lambda t: t.fqn)
 
    # Deduplicate imported traits across packages (merge implementation sites)
    imported_by_fqn: dict[str, ImportedTrait] = {}
    for it in all_imported_raw:
        if it.fqn not in imported_by_fqn:
            imported_by_fqn[it.fqn] = it
        else:
            existing = imported_by_fqn[it.fqn]
            for site in it.implementations:
                if not any(s.source_file == site.source_file and s.line == site.line
                           for s in existing.implementations):
                    existing.implementations.append(site)
            for fp in it.imported_in:
                if fp not in existing.imported_in:
                    existing.imported_in.append(fp)
    all_imported = sorted(imported_by_fqn.values(), key=lambda t: t.fqn)
 
    if args.format == "json":
        output = _generate_json(owner, repo, branch, all_traits, all_imported)
    else:
        output = _generate_text(all_traits, all_imported)
 
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to: {args.output}")
    else:
        print(output)
 
 
if __name__ == "__main__":
    main()
