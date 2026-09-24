#!/usr/bin/env python3
"""
generate_sanity.py — Generate Soroban sanity wrapper files

Scans a Soroban project and generates sanity wrappers for:
  1. #[contract] structs  → src/sanity.rs (inline module, no new Cargo.toml)
  2. #[contracttrait] traits → sanity/<name>/ crate (separate package)

Usage:
    python generate_sanity.py <project_root> [--dry-run] [--force]

Options:
    --dry-run   Print generated content without writing any files
    --force     Overwrite existing sanity files
"""

import sys, os, re, argparse, json
from pathlib import Path
from typing import Optional
from collections import defaultdict
from util import *

# ─── Helpers ────────────────────────────────────────────────────────────────

SOROBAN_SDK_TYPES = {
    'Address', 'Env', 'Symbol', 'String', 'Vec', 'Map', 'Bytes',
    'BytesN', 'Val', 'MuxedAddress', 'U256', 'I256', 'Error',
    'Duration', 'Timepoint',
}

_RUST_PRIMITIVES = {
    'bool', 'u8', 'u16', 'u32', 'u64', 'u128', 'usize',
    'i8', 'i16', 'i32', 'i64', 'i128', 'isize', 'f32', 'f64',
    'char', 'Option', 'Result', 'Some', 'None', 'Ok', 'Err',
    'Box', 'Rc', 'Arc', 'Clone', 'Copy', 'Default', 'Send', 'Sync',
    'Iterator', 'IntoIterator', 'From', 'Into', 'AsRef', 'AsMut',
    'Self',
}

def to_kebab(name: str) -> str:
    """Convert CamelCase to kebab-case."""
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1-\2', s)
    return s.lower()

def to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    return to_kebab(name).replace('-', '_')

def extract_block(text: str, start: int) -> tuple[str, int]:
    """Extract balanced {…} starting at index `start` (must be '{')."""
    if start >= len(text) or text[start] != '{':
        return '', start
    depth, i = 0, start
    while i < len(text):
        ch = text[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], len(text)

def strip_comments(text: str) -> str:
    """Strip // line comments and /* */ block comments (naively)."""
    # Block comments
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.DOTALL)
    # Line comments
    text = re.sub(r'//[^\n]*', '', text)
    return text


# Matches attributes containing the word 'test' (e.g. #[cfg(test)],
# #[cfg(any(test, feature="testutils"))]) followed by a mod block.
_TEST_ATTR_MOD_PAT = re.compile(
    r'#\[[^\]]*\btest\b[^\]]*\]\s*(?:pub\s+)?mod\s+\w+\s*\{'
)

# Matches cfg_if::cfg_if! { ... } and cfg_if! { ... }
_CFG_IF_PAT = re.compile(
    r'\bcfg_if\s*(?:::\s*cfg_if\s*)?\s*!\s*\{'
)


def _strip_brace_blocks(cleaned: str, pattern: re.Pattern) -> str:
    """Remove all blocks matched by *pattern* (which must end just before the
    opening '{') from an already-comment-stripped source string."""
    pos = 0
    parts: list[str] = []
    while pos < len(cleaned):
        m = pattern.search(cleaned, pos)
        if not m:
            parts.append(cleaned[pos:])
            break
        parts.append(cleaned[pos:m.start()])
        brace_pos = m.end() - 1   # the opening '{'
        _, end_pos = extract_block(cleaned, brace_pos)
        pos = end_pos
    return ''.join(parts)


def strip_test_blocks(cleaned: str) -> str:
    """Remove test-attributed mod blocks (e.g. #[cfg(test)] mod tests { … })
    from an already-comment-stripped source string so that test-only
    #[contract] structs are not mistaken for real contracts."""
    return _strip_brace_blocks(cleaned, _TEST_ATTR_MOD_PAT)


def strip_cfg_if_blocks(cleaned: str) -> str:
    """Remove cfg_if::cfg_if! { … } macro invocations from an
    already-comment-stripped source string.  Conditional compilation blocks
    often contain alternate module declarations or type definitions that do
    not match the current build target and would confuse the analysis."""
    return _strip_brace_blocks(cleaned, _CFG_IF_PAT)


def strip_non_contract_blocks(content: str) -> str:
    """Full cleaning pipeline: strip comments, test mod blocks, and cfg_if blocks."""
    cleaned = strip_comments(content)
    cleaned = strip_test_blocks(cleaned)
    cleaned = strip_cfg_if_blocks(cleaned)
    return cleaned

def parse_param(s: str) -> Optional[tuple[str, str]]:
    """Parse 'name: Type' → (name, type). Returns None for self variants."""
    s = s.strip()
    if re.match(r'^&?mut?\s*self$', s):
        return None
    idx = s.find(':')
    if idx < 0:
        return None
    name = s[:idx].strip()
    ty = s[idx + 1:].strip()
    # Remove mut from name (e.g. `mut foo: Bar`)
    name = re.sub(r'^mut\s+', '', name)
    return (name, ty)

def collect_soroban_types(params: list[tuple[str, str]], ret: Optional[str]) -> set[str]:
    """Gather soroban_sdk type names from a function signature."""
    types = set()
    all_ty = ' '.join(ty for _, ty in params) + ' ' + (ret or '')
    for t in re.findall(r'\b([A-Z][A-Za-z0-9]*)\b', all_ty):
        if t in SOROBAN_SDK_TYPES:
            types.add(t)
    return types


# ─── Use-statement analysis ──────────────────────────────────────────────────

def expand_use_tree(tree: str, prefix: str = '') -> list[tuple[str, str, bool]]:
    """
    Recursively expand a use tree into (local_name, full_path, is_self) triples.

    - is_self=True  means the name was introduced via `{self}` — i.e. the
      module itself was imported, so the name is the last path segment and
      sub-items should be grouped with it under `use full_path::{self, ...}`.
    - is_self=False means a regular name or alias import.
    """
    tree = tree.strip()
    if not tree:
        return []

    # `... as Alias` at this level (no braces before `as`)
    as_m = re.match(r'^([^{]+?)\s+as\s+(\w+)$', tree)
    if as_m:
        path_part = as_m.group(1).strip()
        alias = as_m.group(2)
        full = f'{prefix}::{path_part}'.lstrip(':') if prefix else path_part
        return [(alias, full, False)]

    # Find first top-level `{`
    brace_idx = tree.find('{')

    if brace_idx == -1:
        # Simple path: `foo::bar::Name` or `self` or `*`
        if tree == 'self':
            local_name = prefix.split('::')[-1] if prefix else 'self'
            return [(local_name, prefix, True)]
        if tree == '*':
            return []
        full = f'{prefix}::{tree}'.lstrip(':') if prefix else tree
        last = tree.split('::')[-1]
        return [(last, full, False)]

    # Contains braces: `prefix_part::{...}`
    prefix_part = tree[:brace_idx].rstrip(':').strip()
    if prefix and prefix_part:
        new_prefix = f'{prefix}::{prefix_part}'
    elif prefix_part:
        new_prefix = prefix_part
    else:
        new_prefix = prefix

    # Find matching `}`
    depth = 0
    end_idx = len(tree) - 1
    for i in range(brace_idx, len(tree)):
        if tree[i] == '{':
            depth += 1
        elif tree[i] == '}':
            depth -= 1
            if depth == 0:
                end_idx = i
                break

    inner = tree[brace_idx + 1:end_idx]
    items = split_by_comma(inner)

    result = []
    for item in items:
        item = item.strip()
        if item:
            result.extend(expand_use_tree(item, new_prefix))
    return result


def parse_use_stmts_from_source(source_text: str) -> list[str]:
    """
    Extract all `use ...;` statements from Rust source text.
    Handles multi-line use statements by tracking brace depth.
    """
    cleaned = strip_comments(source_text)
    stmts = []
    i = 0
    while i < len(cleaned):
        m = re.search(r'(?<![a-zA-Z_0-9])(?:pub\s+)?use\s+', cleaned[i:])
        if not m:
            break
        use_start = i + m.start()
        j = i + m.end()
        depth = 0
        found = False
        while j < len(cleaned):
            ch = cleaned[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                if depth == 0:
                    break   # hit outer block boundary — not a use stmt
                depth -= 1
            elif ch == ';' and depth == 0:
                stmts.append(cleaned[use_start:j + 1].strip())
                found = True
                i = j + 1
                break
            j += 1
        if not found:
            i = j + 1
    return stmts


def extract_local_type_names(all_fns: list[dict],
                              also_include: Optional[set[str]] = None) -> set[str]:
    """
    Extract type-name tokens from function signatures that may need a
    project-local `use` statement (i.e. not soroban_sdk and not a Rust primitive).
    Also captures module qualifiers like `storage` from `storage::Fee`.

    `also_include`: names normally filtered (e.g. SDK-shadowed types) that
    should still be collected if they appear in signatures.
    """
    also_include = also_include or set()
    names: set[str] = set()
    for fn in all_fns:
        all_ty = ' '.join(ty for _, ty in fn['params']) + ' ' + (fn['ret'] or '')
        for t in re.findall(r'\b([A-Z][A-Za-z0-9]*)\b', all_ty):
            if t in also_include:
                names.add(t)
            elif t not in SOROBAN_SDK_TYPES and t not in _RUST_PRIMITIVES:
                names.add(t)
        # Module qualifiers before `::`, e.g. `storage` in `storage::Fee`
        for t in re.findall(r'\b([a-z_][a-z_0-9]*)\s*::', all_ty):
            if t not in ('crate', 'super', 'self', 'std', 'core', 'alloc'):
                names.add(t)
    return names


def reconstruct_use_stmts(needed_names: set[str],
                           all_triples: list[tuple[str, str, bool]]) -> list[str]:
    """
    Build minimal `use` statements that together import exactly `needed_names`.

    Strategy:
    - Identify "module" (is_self=True) triples in the needed set.
    - Any other needed triple whose full_path starts with a module_path + '::'
      is grouped under that module so they emit `use mod::{self, Item, ...}`.
    - Everything else is grouped by parent path and emits `use parent::Item;`
      or `use parent::{A, B};` for multiple items from the same parent.
    """
    # Filter to only needed triples
    needed = [(n, p, s) for n, p, s in all_triples if n in needed_names]
    if not needed:
        return []

    # Full paths of module ("self") imports in the needed set
    module_paths: set[str] = {p for _, p, s in needed if s}

    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for name, path, is_self in needed:
        if is_self:
            # Group under its own path; emit as `self` item
            groups[path].append(('self', name))
        else:
            # Find the longest module_path that is a strict prefix of this path
            best = ''
            for mp in module_paths:
                if path.startswith(mp + '::') and len(mp) > len(best):
                    best = mp
            if best:
                seg = path[len(best) + 2:]   # strip 'module_path::'
                groups[best].append((seg, name))
            else:
                # Normal: group by parent
                if '::' in path:
                    parent, seg = path.rsplit('::', 1)
                else:
                    parent, seg = '', path
                groups[parent].append((seg, name))

    stmts = []
    for stem in sorted(groups):
        items = groups[stem]
        if not stem:
            for seg, _ in sorted(items):
                stmts.append(f'use {seg};')
        elif len(items) == 1:
            seg, _ = items[0]
            if seg == 'self':
                stmts.append(f'use {stem};')
            else:
                stmts.append(f'use {stem}::{seg};')
        else:
            # Multiple items — use braces, `self` first then alphabetical
            segs_seen: set[str] = set()
            ordered: list[str] = []
            for seg, _ in sorted(items, key=lambda x: (x[0] != 'self', x[0])):
                if seg not in segs_seen:
                    segs_seen.add(seg)
                    ordered.append(seg)
            stmts.append(f'use {stem}::{{{", ".join(ordered)}}};')

    return stmts


def _fn_uses_unresolvable(fn: dict, unresolvable: set[str]) -> bool:
    """Return True if fn's signature references any name in unresolvable."""
    all_ty = ' '.join(ty for _, ty in fn['params']) + ' ' + (fn['ret'] or '')
    return any(t in unresolvable for t in re.findall(r'\b([A-Z][A-Za-z0-9]*)\b', all_ty))


def find_locally_defined_names(filepath: Path) -> set[str]:
    """
    Return the set of type/trait names *defined* in this file (not imported).
    Scans for `pub trait`, `pub struct`, `pub enum`, `pub type` declarations.
    """
    try:
        content = filepath.read_text(encoding='utf-8')
        cleaned = strip_non_contract_blocks(content)
    except Exception:
        return set()
    names: set[str] = set()
    for m in re.finditer(r'\bpub\s+(?:trait|struct|enum|type)\s+(\w+)', cleaned):
        names.add(m.group(1))
    return names


def source_module_parts(source_file: Path) -> list[str]:
    """
    Return the module path of source_file as a list of segments relative to the
    nearest 'src/' directory ancestor.
    E.g.  src/foo/bar.rs  → ['foo', 'bar']
          src/foo/mod.rs  → ['foo']
          src/lib.rs      → []
    """
    src_dir: Optional[Path] = None
    for p in reversed(source_file.parents):
        if p.name == 'src':
            src_dir = p
            break
    if src_dir is None:
        return []
    try:
        rel = source_file.relative_to(src_dir)
        parts = list(rel.parts)
        if parts[-1] in ('lib.rs', 'mod.rs'):
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]  # strip .rs
        return parts
    except ValueError:
        return []


def resolve_super_path(path: str, module_parts: list[str]) -> str:
    """
    Resolve a path that starts with one or more 'super::' segments into a
    'crate::' absolute path, given the module path of the file the use statement
    lives in.

    Example:  path='super::types::Foo', module_parts=['foo', 'bar']
              → 'crate::foo::types::Foo'  (super of 'bar' is 'foo')
    """
    if not (path == 'super' or path.startswith('super::')):
        return path
    segs = path.split('::')
    n_super = 0
    while n_super < len(segs) and segs[n_super] == 'super':
        n_super += 1
    rest = segs[n_super:]
    ancestor = module_parts[:max(0, len(module_parts) - n_super)]
    all_parts = ['crate'] + ancestor + rest
    return '::'.join(all_parts)


def normalize_triples_super(triples: list[tuple[str, str, bool]],
                              module_parts: list[str]) -> list[tuple[str, str, bool]]:
    """Rewrite any super:: paths in a triple list to crate:: paths."""
    result = []
    for name, path, is_self in triples:
        if path == 'super' or path.startswith('super::') or '::super::' in path:
            path = resolve_super_path(path, module_parts)
        result.append((name, path, is_self))
    return result


_KNOWN_EXTERNAL_PREFIXES = frozenset({
    'crate', 'super', 'self', 'std', 'core', 'alloc',
    'soroban_sdk', 'stellar_xdr',
})


def find_declared_modules(source_text: str) -> set[str]:
    """Return the set of submodule names declared via `mod name;` in source_text."""
    cleaned = strip_comments(source_text)
    return set(re.findall(r'\bmod\s+(\w+)\s*;', cleaned))


def normalize_triples_bare_mods(triples: list[tuple[str, str, bool]],
                                  declared_mods: set[str],
                                  module_parts: list[str]) -> list[tuple[str, str, bool]]:
    """
    Convert bare paths that start with a locally-declared submodule name to
    absolute crate:: paths.

    Example: source is lib.rs (module_parts=[]), has `mod error;`
             triple ('Error', 'error::Error', False)
             → ('Error', 'crate::error::Error', False)

    Example: source is src/foo/bar.rs (module_parts=['foo','bar']), has `mod baz;`
             triple ('Qux', 'baz::Qux', False)
             → ('Qux', 'crate::foo::bar::baz::Qux', False)
    """
    result = []
    for name, path, is_self in triples:
        segs = path.split('::')
        first = segs[0]
        if first in declared_mods and first not in _KNOWN_EXTERNAL_PREFIXES:
            # Bare path starting with a local submodule — make absolute
            absolute = '::'.join(['crate'] + module_parts + segs)
            result.append((name, absolute, is_self))
        else:
            result.append((name, path, is_self))
    return result


def _find_workspace_crate_src(crate_name: str, near_path: Path) -> Optional[Path]:
    """
    Given an external crate name (hyphens or underscores), find its src/
    directory by locating the workspace Cargo.toml and matching package names
    among workspace members.
    """
    crate_kebab = crate_name.replace('_', '-')
    crate_snake = crate_name.replace('-', '_')

    # Walk up to find the workspace Cargo.toml
    candidate = near_path if near_path.is_dir() else near_path.parent
    ws_root: Optional[Path] = None
    while True:
        cargo = candidate / 'Cargo.toml'
        if cargo.exists():
            try:
                if '[workspace]' in cargo.read_text():
                    ws_root = candidate
                    break
            except Exception:
                pass
        if candidate.parent == candidate:
            break
        candidate = candidate.parent

    if ws_root is None:
        return None

    # Search all non-target Cargo.toml files in the workspace
    import os as _os2
    for dirpath, dirnames, filenames in _os2.walk(ws_root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in ('target', 'sanity', '.git')]
        if 'Cargo.toml' not in filenames:
            continue
        cargo_path = Path(dirpath) / 'Cargo.toml'
        try:
            content = cargo_path.read_text()
            # Only package Cargo.toml files (skip workspace root if it lacks [package])
            if '[package]' not in content:
                continue
            nm = re.search(r'^\[package\].*?^name\s*=\s*"([^"]+)"',
                           content, re.MULTILINE | re.DOTALL)
            if not nm:
                continue
            pkg = nm.group(1)
            if pkg == crate_kebab or pkg == crate_snake:
                src = cargo_path.parent / 'src'
                if src.is_dir():
                    return src
        except Exception:
            continue
    return None


def find_publicly_exported_names(lib_rs: Path) -> set[str]:
    """
    Return all upper-case-named types/traits publicly accessible from a crate
    lib.rs, including those re-exported via `pub use`.

    Handles:
    - `pub struct/enum/trait/type Foo` — defined inline
    - `pub use module::Foo;`           — named re-export
    - `pub use module::*;`             — glob re-export (scans the module file)
    """
    try:
        text = lib_rs.read_text(encoding='utf-8')
    except Exception:
        return set()

    src_dir = lib_rs.parent
    result: set[str] = find_locally_defined_names(lib_rs)

    for stmt in parse_use_stmts_from_source(text):
        if not re.match(r'\s*pub\s+use\b', stmt):
            continue  # only pub use

        body = re.sub(r'^\s*pub\s+use\s+', '', stmt).rstrip(';').strip()

        # Glob re-export: pub use some::module::*
        if body.rstrip().endswith('*'):
            gm = re.match(r'(.*?)::\s*\*$', body.strip())
            if gm:
                mod_path = gm.group(1).strip()
                mod_parts_list = mod_path.replace('::', '/').split('/')
                cand_rs  = src_dir.joinpath(*mod_parts_list).with_suffix('.rs')
                cand_mod = src_dir.joinpath(*mod_parts_list, 'mod.rs')
                for cand in (cand_rs, cand_mod):
                    if cand.exists():
                        result |= find_locally_defined_names(cand)
                        break
        else:
            # Named re-exports — collect the local names
            for name, _path, is_self in expand_use_tree(body):
                if not is_self and re.match(r'[A-Z]', name):
                    result.add(name)

    return result


def collect_glob_triples(raw_stmts: list[str],
                          source_file: Path) -> list[tuple[str, str, bool]]:
    """
    For each 'use X::*;' import in raw_stmts, scan the target module file and
    return (name, full_path, False) triples for every publicly defined name.
    This handles the common pattern of 'use crate::types::*;' where individual
    type names are otherwise invisible to the triple lookup.
    """
    src_module = source_module_parts(source_file)
    # Find the 'src/' directory nearest to source_file
    src_dir: Optional[Path] = None
    for p in reversed(source_file.parents):
        if p.name == 'src':
            src_dir = p
            break
    if src_dir is None:
        return []

    result: list[tuple[str, str, bool]] = []
    for stmt in raw_stmts:
        m = re.match(r'(?:pub\s+)?use\s+(.*?)\s*::\s*\*\s*;', stmt.strip())
        if not m:
            continue
        prefix = m.group(1).strip()
        # Resolve super:: relative to this source file
        if prefix == 'super' or prefix.startswith('super::'):
            prefix = resolve_super_path(prefix, src_module)
        if not prefix.startswith('crate::'):
            # Try to resolve as a local workspace crate (e.g. `use k2_shared::*`)
            if '::' not in prefix:
                ext_src = _find_workspace_crate_src(prefix, source_file)
                if ext_src is not None:
                    lib_rs_ext = ext_src / 'lib.rs'
                    mod_rs_ext = ext_src / 'mod.rs'
                    crate_root_ext = lib_rs_ext if lib_rs_ext.exists() else (mod_rs_ext if mod_rs_ext.exists() else None)
                    if crate_root_ext is not None:
                        for name in find_publicly_exported_names(crate_root_ext):
                            result.append((name, f'{prefix}::{name}', False))
            continue  # skip if external and not found locally
        mod_path = prefix[len('crate::'):]
        mod_parts_list = mod_path.replace('::', '/').split('/')

        # Candidates: src/a/b.rs or src/a/b/mod.rs
        candidate_rs  = src_dir.joinpath(*mod_parts_list).with_suffix('.rs')
        candidate_mod = src_dir.joinpath(*mod_parts_list, 'mod.rs')
        for candidate in (candidate_rs, candidate_mod):
            if candidate.exists():
                for name in find_locally_defined_names(candidate):
                    result.append((name, f'{prefix}::{name}', False))
                break
    return result


def collect_project_use_stmts(all_fns: list[dict],
                               source_file: Path,
                               extra_names: Optional[set[str]] = None
                               ) -> tuple[list[str], set[str]]:
    """
    Return ``(use_stmts, sdk_shadowed)`` where:
    - ``use_stmts``    — `use` statements for project-local types/traits needed by all_fns.
    - ``sdk_shadowed`` — names that appear in SOROBAN_SDK_TYPES but are *also*
                         locally imported (e.g. ``crate::types::Error``); callers
                         should subtract these from the soroban_sdk import line.

    Parses `source_file`'s own use statements, expands them to individual
    (name, path, is_self) triples, then reconstructs minimal use statements
    covering exactly the names needed.

    `extra_names` adds additional local names to look up (e.g. impl trait names).
    """
    try:
        source_text = source_file.read_text(encoding='utf-8')
    except Exception:
        return [], set()

    raw_stmts = parse_use_stmts_from_source(source_text)

    # Module path of the source file (for resolving super:: references)
    mod_parts = source_module_parts(source_file)

    all_triples: list[tuple[str, str, bool]] = []
    glob_stmts: list[str] = []
    for stmt in raw_stmts:
        body = re.sub(r'^\s*(?:pub\s+)?use\s+', '', stmt).rstrip(';').strip()
        if 'soroban_sdk' in stmt:
            # Top-level soroban types (soroban_sdk::Address etc.) are handled by
            # collect_soroban_types().  Sub-module paths (soroban_sdk::auth::Context)
            # are NOT in SOROBAN_SDK_TYPES and need a full `use` statement, so we
            # collect them here.
            for triple in expand_use_tree(body):
                n, p, s = triple
                if p.startswith('soroban_sdk::') and p.count('::') >= 2:
                    all_triples.append(triple)
            continue
        if body.rstrip().endswith('*'):
            # Glob import — expand later against the target module file
            glob_stmts.append(stmt)
            continue
        all_triples.extend(expand_use_tree(body))

    # Bug fix 1a: convert super:: paths to crate:: using the source file's module
    # position.  A use statement like `use super::types::Foo;` in src/foo/bar.rs
    # means crate::foo::types::Foo, but emitting `use super::types::Foo;` from
    # src/sanity.rs (where super:: is the crate root) would resolve to the wrong
    # module.  Normalising to crate:: makes the path unambiguous.
    all_triples = normalize_triples_super(all_triples, mod_parts)

    # Bug fix 1b: convert bare paths like `use error::Error;` that start with a
    # locally-declared submodule (mod error;) to crate:: paths.  Such paths work
    # in lib.rs as relative references but not from src/sanity.rs.
    declared_mods = find_declared_modules(source_text)
    all_triples = normalize_triples_bare_mods(all_triples, declared_mods, mod_parts)

    # Bug fix 2: expand glob imports (use X::*;) so that types only visible via
    # a glob in the source file (e.g. MyType in `Vec<MyType>` when the source has
    # `use crate::types::*;`) are still resolvable.
    if glob_stmts:
        all_triples.extend(collect_glob_triples(glob_stmts, source_file))

    # Bug fix 3: when source_file is not lib.rs, also scan lib.rs for pub use
    # re-exports.  Contracts often rely on types made visible via `pub use` in
    # lib.rs without repeating the import in contract.rs.  For example:
    #   lib.rs:      pub use error::KineticRouterError;
    #   contract.rs: fn foo() -> Result<(), KineticRouterError>  ← no `use` here
    # Without this fix, KineticRouterError ends up in unresolved_needed and is
    # silently dropped.
    src_dir_for_lib: Optional[Path] = None
    for p in reversed(source_file.parents):
        if p.name == 'src':
            src_dir_for_lib = p
            break
    if src_dir_for_lib is not None:
        _lib_rs_candidate  = src_dir_for_lib / 'lib.rs'
        _mod_rs_candidate  = src_dir_for_lib / 'mod.rs'
        lib_rs = _lib_rs_candidate if _lib_rs_candidate.exists() else _mod_rs_candidate
        if lib_rs.exists() and lib_rs.resolve() != source_file.resolve():
            try:
                lib_text = lib_rs.read_text(encoding='utf-8')
                lib_raw_stmts = parse_use_stmts_from_source(lib_text)
                lib_triples: list[tuple[str, str, bool]] = []
                lib_glob_stmts: list[str] = []
                for stmt in lib_raw_stmts:
                    body2 = re.sub(r'^\s*(?:pub\s+)?use\s+', '', stmt).rstrip(';').strip()
                    if 'soroban_sdk' in stmt:
                        for triple in expand_use_tree(body2):
                            n2, p2, s2 = triple
                            if p2.startswith('soroban_sdk::') and p2.count('::') >= 2:
                                lib_triples.append(triple)
                        continue
                    if body2.rstrip().endswith('*'):
                        lib_glob_stmts.append(stmt)
                        continue
                    lib_triples.extend(expand_use_tree(body2))
                # lib.rs is always at the crate root (mod_parts = [])
                lib_triples = normalize_triples_super(lib_triples, [])
                lib_declared_mods = find_declared_modules(lib_text)
                lib_triples = normalize_triples_bare_mods(lib_triples, lib_declared_mods, [])
                if lib_glob_stmts:
                    lib_triples.extend(collect_glob_triples(lib_glob_stmts, lib_rs))
                all_triples.extend(lib_triples)
            except Exception:
                pass

    # Fix A: deduplicate triples by name — when the same name appears from multiple
    # sources (e.g. from both lib.rs scan and external glob expansion), keep only the
    # first occurrence to avoid E0252 "defined multiple times" errors.
    seen_names_dedup: set[str] = set()
    deduped_triples: list[tuple[str, str, bool]] = []
    for t in all_triples:
        if t[0] not in seen_names_dedup:
            seen_names_dedup.add(t[0])
            deduped_triples.append(t)
    all_triples = deduped_triples

    # Names in SOROBAN_SDK_TYPES that are locally re-imported (shadow the SDK)
    sdk_shadowed: set[str] = {n for n, p, s in all_triples if n in SOROBAN_SDK_TYPES}

    needed_names = extract_local_type_names(all_fns, also_include=sdk_shadowed)
    if extra_names:
        needed_names |= extra_names
    if not needed_names:
        return [], sdk_shadowed, set()

    stmts = reconstruct_use_stmts(needed_names, all_triples)

    # Names that were needed but had no matching triple in the source's use stmts
    # (e.g. types defined directly in the same file — no `use` statement for them)
    resolved_names = {n for n, p, s in all_triples if n in needed_names}
    unresolved_needed = needed_names - resolved_names

    return stmts, sdk_shadowed, unresolved_needed


# ─── Parsing ────────────────────────────────────────────────────────────────

def _subst_self_assoc(ty: str, assoc_types: dict) -> str:
    """Replace Self::AssocName with the concrete type from the impl's associated-type definitions."""
    def replace(m):
        return assoc_types.get(m.group(1), m.group(0))
    return re.sub(r'\bSelf::(\w+)', replace, ty)


def extract_fn_signatures(block_body: str,
                          require_pub: bool = False) -> list[dict]:
    """
    Pull all fn signatures from the *interior* of an impl or trait block.
    Returns list of dicts: {name, params: [(name, ty)], ret: str|None}

    require_pub — when True only `pub fn` signatures are returned (used for
    direct impl blocks where private helpers must be excluded from the sanity
    wrapper).
    """
    fns = []
    text = strip_comments(block_body)
    i = 0

    # Pattern selects either any `fn` or only `pub fn` depending on require_pub
    fn_pat = (r'\bpub\s+fn\s+(\w+)\s*(?:<[^>]*>)?\s*\('
              if require_pub
              else r'\bfn\s+(\w+)\s*(?:<[^>]*>)?\s*\(')

    while i < len(text):
        # Find next qualifying `fn` keyword
        m = re.search(fn_pat, text[i:])
        if not m:
            break

        fn_name = m.group(1)
        # Position of the opening '(' for params
        paren_open = i + m.end() - 1  # points to '('

        # Walk to find matching ')'
        depth, j = 0, paren_open
        while j < len(text):
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1

        params_str = text[paren_open + 1:j]
        raw_params = split_by_comma(params_str)
        params = [parse_param(p) for p in raw_params]
        params = [p for p in params if p is not None]

        # Everything after ')' up to the body opening '{'
        after_paren = text[j + 1:]
        ret_match = re.match(
            r'\s*->\s*([\w\s<>:,&\'()\[\]!?*+]+?)(?=\s*(?:where\b|\{|;))',
            after_paren
        )
        ret_type = ret_match.group(1).strip() if ret_match else None

        # Skip past the body (or semicolon for trait method stubs)
        search_from = j + 1
        brace_pos = text.find('{', search_from)
        semi_pos = text.find(';', search_from)

        if brace_pos != -1 and (semi_pos == -1 or brace_pos < semi_pos):
            has_default = True
            _, i = extract_block(text, brace_pos)
        elif semi_pos != -1:
            has_default = False
            i = semi_pos + 1
        else:
            has_default = False
            break

        fns.append({'name': fn_name, 'params': params, 'ret': ret_type, 'has_default': has_default})

    return fns


def scan_file(filepath: Path) -> tuple[list[dict], list[dict]]:
    """
    Scan one .rs file.
    Returns (contracts, traits) where each is a list of dicts.
    """
    content = filepath.read_text(encoding='utf-8')
    # Remove comments, test-only mod blocks, and cfg_if blocks so that
    # conditionally-compiled or test-only #[contract] structs are not picked up.
    cleaned = strip_non_contract_blocks(content)

    contracts = []
    traits = []

    # ── #[contract] structs ─────────────────────────────────────────────────
    for m in re.finditer(r'#\[contract\]\s*(?:pub\s+)?struct\s+(\w+)', cleaned):
        struct_name = m.group(1)
        direct_fns = []
        trait_fns = []
        impl_traits: set[str] = set()

        # Walk every #[contractimpl…] block
        for attr_m in re.finditer(r'#\[contractimpl(?:\([^)]*\))?\]', cleaned):
            attr_text = attr_m.group(0)
            is_contracttrait = 'contracttrait' in attr_text

            # Find the `impl` keyword after the attribute
            after_attr = cleaned[attr_m.end():]
            impl_m = re.search(r'\bimpl\b', after_attr)
            if not impl_m:
                continue

            # Slice from `impl` to find the opening `{`
            impl_start = attr_m.end() + impl_m.end()
            brace_pos = cleaned.find('{', impl_start)
            if brace_pos == -1:
                continue

            impl_header = cleaned[impl_start:brace_pos]

            # Determine which type this impl is on
            for_m = re.search(r'\bfor\s+(\w+)', impl_header)
            if for_m:
                impl_type = for_m.group(1)
                is_trait_impl = True
            else:
                direct_m = re.match(r'\s*(\w+)', impl_header)
                impl_type = direct_m.group(1) if direct_m else None
                is_trait_impl = False

            if impl_type != struct_name:
                continue

            block, _ = extract_block(cleaned, brace_pos)
            # For direct (non-trait) impl blocks, skip private helper fns.
            # Trait impl blocks don't use `pub` on their methods, so we keep all.
            fns = extract_fn_signatures(block[1:-1], require_pub=not is_trait_impl)

            if is_trait_impl:
                # Capture the full trait path (e.g. 'token::Interface') and its
                # short last-segment name for use statements.
                trait_m = re.match(r'\s*([\w:]+)', impl_header)
                full_trait_name = trait_m.group(1) if trait_m else None
                if full_trait_name:
                    impl_traits.add(full_trait_name.split('::')[-1])

                # Collect `type AssocName = ConcreteType;` definitions so we can
                # substitute Self::AssocName → ConcreteType in method signatures
                assoc_types: dict[str, str] = {}
                for at_m in re.finditer(r'\btype\s+(\w+)\s*=\s*([^;]+);', block[1:-1]):
                    assoc_types[at_m.group(1)] = at_m.group(2).strip()

                for fn in fns:
                    fn['is_trait'] = True
                    fn['trait_name'] = full_trait_name  # full path, e.g. 'token::Interface'
                    if assoc_types:
                        fn['params'] = [
                            (n, _subst_self_assoc(t, assoc_types))
                            for n, t in fn['params']
                        ]
                        if fn['ret']:
                            fn['ret'] = _subst_self_assoc(fn['ret'], assoc_types)
                trait_fns.extend(fns)
            else:
                for fn in fns:
                    fn['is_trait'] = False
                direct_fns.extend(fns)

        if direct_fns or trait_fns:
            contracts.append({
                'name': struct_name,
                'file': filepath,
                'direct_fns': direct_fns,
                'trait_fns': trait_fns,
                'impl_traits': impl_traits,
            })

    # ── #[contracttrait] traits ─────────────────────────────────────────────
    for m in re.finditer(r'#\[contracttrait\]\s*pub\s+trait\s+(\w+)\s*(?:<[^>]*>)?\s*\{', cleaned):
        trait_name = m.group(1)
        brace_pos = cleaned.find('{', m.start())
        block, _ = extract_block(cleaned, brace_pos)
        block_body = block[1:-1]
        fns = extract_fn_signatures(block_body)
        # Required associated types: `type Foo;` or `type Foo: Bound;` with no `= ...`
        # Strip defaulted types first, then find remaining type declarations.
        stripped = re.sub(r'\btype\s+\w+(?:[^=;]*?)=[^;]+;', '', block_body)
        required_types = re.findall(r'\btype\s+(\w+)\s*(?::[^;{=]*)?\s*;', stripped)
        traits.append({
            'name': trait_name,
            'file': filepath,
            'fns': fns,
            'required_types': required_types,
        })

    return contracts, traits


def is_test_file(path: Path, root: Optional[Path] = None) -> bool:
    """Return True if a .rs file is test-only and should be skipped by default."""
    stem = path.stem
    # Skip files by name pattern
    if stem in ('test', 'tests', 'testutils', 'test_utils', 'mock', 'mocks'):
        return True
    if stem.startswith('test_') or stem.startswith('mock_'):
        return True
    # Skip files inside a test/ tests/ testutils/ directory segment.
    # Only check path components *relative to the project root* so that a
    # project stored under e.g. /tmp/stellar/test/my-project/ is not
    # mistakenly treated as test-only because "test" appears in the absolute
    # path outside the project directory.
    try:
        rel_parts = path.relative_to(root).parts if root is not None else path.parts
    except ValueError:
        rel_parts = path.parts
    if any(part in ('test', 'tests', 'testutils', 'test_utils') for part in rel_parts):
        return True
    # Skip files whose first 400 chars open with #[cfg(test)]
    try:
        text = path.read_text(encoding='utf-8')
        header = text[:400].lstrip()
        if header.startswith('#[cfg(test)]'):
            return True
    except Exception:
        pass
    return False


def scan_project(root: Path, include_tests: bool = False) -> tuple[list[dict], list[dict]]:
    """Recursively scan all .rs files (skipping target/ and existing sanity dirs)."""
    all_contracts, all_traits = [], []

    # Use os.walk with an error handler so that directories that vanish during
    # scanning (e.g. fuzz/target/ build artefacts) don't abort the whole scan.
    import os as _os
    skip_dirs = {'target', 'sanity'}
    for dirpath, dirnames, filenames in _os.walk(root, onerror=lambda _e: None):
        # Prune excluded directories in-place so os.walk won't descend into them
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            if not filename.endswith('.rs'):
                continue
            rs = Path(dirpath) / filename
            if not include_tests and is_test_file(rs, root):
                continue
            try:
                c, t = scan_file(rs)
                all_contracts.extend(c)
                all_traits.extend(t)
            except Exception as ex:
                print(f'  [warn] could not parse {rs}: {ex}', file=sys.stderr)
    return all_contracts, all_traits


# ─── Code generation ─────────────────────────────────────────────────────────

def call_arg(name: str, ty: str) -> str:
    """Format a call argument given the wrapper's param name and type.

    Convention (matches hand-written examples):
      e: Env    (by value)  → pass `name`   (direct impl methods)
      e: &Env   (by ref)    → pass `&name`  (trait impl / trait wrappers)
    The &name case passes &&Env which auto-derefs to &Env in Rust.
    """
    if re.search(r'\bEnv\b', ty):
        if ty.strip().startswith('&'):
            return f'&{name}'   # &Env param → pass &name (→ &&Env, coerces to &Env)
        else:
            return name           # Env by value → pass as-is
    return name


def fn_decl(name: str, params: list[tuple[str, str]], ret: Optional[str]) -> str:
    param_str = ', '.join(f'{n}: {t}' for n, t in params)
    ret_str = f' -> {ret}' if ret else ''
    return f'#[inline(never)]\n#[no_mangle]\npub fn {name}({param_str}){ret_str}'


def fn_call(contract: str, method: str, params: list[tuple[str, str]], ret: Optional[str]) -> str:
    args = ', '.join(call_arg(n, t) for n, t in params)
    expr = f'{contract}::{method}({args})'
    if ret:
        return f'    {expr}'
    else:
        return f'    {expr};'


def fn_call_trait(contract: str, trait_name: str, method: str,
                  params: list[tuple[str, str]], ret: Optional[str]) -> str:
    """Like fn_call but uses UFCS: <Contract as TraitName>::method(args).

    Required for trait impl methods so the call compiles even when the method
    name is only in scope via a trait (e.g. token::Interface::allowance).
    """
    args = ', '.join(call_arg(n, t) for n, t in params)
    expr = f'<{contract} as {trait_name}>::{method}({args})'
    if ret:
        return f'    {expr}'
    else:
        return f'    {expr};'


def resolve_qualified_trait_path(trait_name: str, source_file: Path) -> str:
    """Resolve a module-qualified trait path from an impl header to a full path.

    A Soroban source file may write::

        use soroban_sdk::token::{self, Interface as _};
        impl token::Interface for Token { ... }

    Here ``token`` is not a local module — it's ``soroban_sdk::token``, brought
    into scope via the ``self`` item in the use tree.  This function looks for
    such module-self imports and rewrites the leading segment accordingly, so
    ``token::Interface`` becomes ``soroban_sdk::token::Interface``.

    Returns *trait_name* unchanged when no resolution is found.
    """
    if '::' not in trait_name:
        return trait_name
    first_seg, rest = trait_name.split('::', 1)
    try:
        source_text = source_file.read_text(encoding='utf-8')
    except Exception:
        return trait_name
    raw_stmts = parse_use_stmts_from_source(source_text)
    mod_parts = source_module_parts(source_file)
    for stmt in raw_stmts:
        body = re.sub(r'^\s*(?:pub\s+)?use\s+', '', stmt).rstrip(';').strip()
        for name, path, is_self in expand_use_tree(body):
            if name == first_seg and is_self:
                # e.g. name='token', path='soroban_sdk::token', is_self=True
                # → 'token::Interface' becomes 'soroban_sdk::token::Interface'
                resolved = f'{path}::{rest}'
                if resolved.startswith('super::'):
                    resolved = resolve_super_path(resolved, mod_parts)
                return resolved
    return trait_name


def generate_contract_sanity(contract: dict) -> tuple[str, list[dict]] | None:
    """Generate src/sanity.rs for a #[contract] struct.

    Returns (code, emitted_fns) where emitted_fns is the subset of functions
    that were actually written (after Fix-B filtering of unresolvable types).
    """
    struct_name = contract['name']
    source_file = contract['file']
    impl_traits: set[str] = contract.get('impl_traits', set())

    # Work with local copies so we can filter without mutating the contract dict
    direct_fns = list(contract['direct_fns'])
    trait_fns = list(contract['trait_fns'])
    all_fns = direct_fns + trait_fns

    sdk_types = set()
    for fn in all_fns:
        sdk_types |= collect_soroban_types(fn['params'], fn['ret'])

    # For trait impls whose impl header uses a qualified path like `token::Interface`
    # (no explicit `use SomeTrait;` in the source file), we need to emit an import
    # in the generated sanity.rs.
    #
    # Step 1: resolve any module-self alias in the path.
    #   use soroban_sdk::token::{self, Interface as _};
    #   impl token::Interface for Token  →  soroban_sdk::token::Interface
    #
    # Step 2: emit `use <resolved_path>;` and use only the short name in UFCS.
    #
    # Trait names that are already a single identifier (e.g. `FlashLoan`) are
    # handled by collect_project_use_stmts via extra_names=impl_traits below.
    explicit_trait_uses: list[str] = []
    _trait_short: dict[str, str] = {}   # raw trait_name → short identifier for UFCS

    for fn in trait_fns:
        raw_tname = fn.get('trait_name') or ''
        if not raw_tname or '::' not in raw_tname:
            continue  # single name — handled via impl_traits / extra_names

        # Resolve module-self aliases (e.g. token → soroban_sdk::token)
        resolved = resolve_qualified_trait_path(raw_tname, source_file)
        short = resolved.split('::')[-1]
        _trait_short[raw_tname] = short

        # Build the import line
        first = resolved.split('::')[0]
        if first in ('crate', 'super', 'self', 'std', 'core', 'alloc',
                     'soroban_sdk', 'stellar_xdr'):
            explicit_trait_uses.append(f'use {resolved};')
        else:
            # Unrecognised first segment — assume external crate, emit bare
            explicit_trait_uses.append(f'use {resolved};')

    # Deduplicate
    explicit_trait_uses = sorted(set(explicit_trait_uses))

    # Project-local use statements; also returns SDK names shadowed by local imports
    # and names that were needed but had no triple (defined directly in source_file)
    project_uses, sdk_shadowed, unresolved_needed = collect_project_use_stmts(
        all_fns, source_file, extra_names=impl_traits
    )

    # Types/traits defined directly in the same file need to appear in the struct
    # import rather than in a separate `use` statement.
    locally_defined = find_locally_defined_names(source_file)

    # Fix B: types that are needed but have no import AND are not locally defined are
    # permanently unresolvable from sanity.rs (e.g. types generated by
    # soroban_sdk::contractimport! which live in an inaccessible submodule).
    # Skip any function whose signature references such a type so we don't emit code
    # that would fail to compile.
    truly_unresolvable = unresolved_needed - locally_defined
    if truly_unresolvable:
        direct_fns = [fn for fn in direct_fns
                      if not _fn_uses_unresolvable(fn, truly_unresolvable)]
        trait_fns  = [fn for fn in trait_fns
                      if not _fn_uses_unresolvable(fn, truly_unresolvable)]
        all_fns = direct_fns + trait_fns
        # Recompute with filtered function list
        sdk_types = set()
        for fn in all_fns:
            sdk_types |= collect_soroban_types(fn['params'], fn['ret'])
        project_uses, sdk_shadowed, unresolved_needed = collect_project_use_stmts(
            all_fns, source_file, extra_names=impl_traits
        )

    # A trait-impl method is called via UFCS `<Contract as Trait>::method`, so
    # the trait must be importable.  If its name could not be resolved from the
    # source file's `use` statements and it is not defined locally, emitting
    # the wrapper would fail with E0405 — drop those methods with a warning.
    unimportable_traits = (impl_traits & unresolved_needed) - locally_defined
    if unimportable_traits:
        dropped = [fn for fn in trait_fns
                   if (fn.get('trait_name') or '').split('::')[-1] in unimportable_traits
                   and '::' not in (fn.get('trait_name') or '')]
        if dropped:
            print(f'  [warn] {struct_name}: cannot resolve import for trait(s) '
                  f'{", ".join(sorted(unimportable_traits))}; skipping '
                  f'{", ".join(fn["name"] for fn in dropped)}', file=sys.stderr)
            trait_fns = [fn for fn in trait_fns if fn not in dropped]
            all_fns = direct_fns + trait_fns
            sdk_types = set()
            for fn in all_fns:
                sdk_types |= collect_soroban_types(fn['params'], fn['ret'])
            project_uses, sdk_shadowed, unresolved_needed = collect_project_use_stmts(
                all_fns, source_file, extra_names=impl_traits - unimportable_traits
            )

    # Remove locally-shadowed names from the soroban_sdk import
    sdk_types -= sdk_shadowed

    # Bug fix 4: don't import from soroban_sdk a name that is *defined* locally.
    # sdk_shadowed catches names locally *imported* via `use`, but a `pub enum Error`
    # defined inline in lib.rs doesn't appear in any `use` statement, so it stays
    # in sdk_types and generates a spurious `use soroban_sdk::Error;` that conflicts.
    # Track the removed names so we can add them to the struct import instead.
    locally_defined_sdk_names = sdk_types & locally_defined
    sdk_types -= locally_defined_sdk_names

    # impl_traits defined in this file (e.g. `FlashLoan`, `Vault`)
    local_impl_traits_set = impl_traits & locally_defined
    # Other types in signatures that live in this file (e.g. `VoteData`, `Error`)
    # Include locally_defined_sdk_names because they were stripped from sdk_types
    # and must be imported from the crate root instead.
    local_extra_types = (
        (unresolved_needed | locally_defined_sdk_names) & locally_defined
    ) - impl_traits - {struct_name}

    # Build the struct import, bundling all locally-defined names together
    all_local_names = sorted({struct_name} | local_impl_traits_set | local_extra_types)

    # Use the full crate-relative module path (not just stem) so that deeply-nested
    # source files like src/contract/entrypoints.rs produce the correct
    # `use crate::contract::entrypoints::Foo;` rather than `use crate::entrypoints::Foo;`.
    mod_parts_for_import = source_module_parts(source_file)

    # Validate that each segment of the module path is actually declared via
    # `mod name;` in its parent file.  Some crates use inline `mod foo { ... }`
    # blocks or re-export patterns that don't match the filesystem layout, so
    # source_module_parts() may return a path segment that isn't a real `mod`
    # declaration reachable from the crate root.  When validation fails we fall
    # back to `use crate::Name;` (i.e. treat the struct as if it lives in the
    # crate root) so the generated file at least compiles without the bad import.
    if mod_parts_for_import:
        src_dir = next(
            (p for p in reversed(source_file.parents) if p.name == 'src'), None
        )
        validated = True
        if src_dir is not None:
            parent_file = src_dir / 'lib.rs'
            if not parent_file.exists():
                parent_file = src_dir / 'mod.rs'
            def _declared_mods_strict(path: Path) -> set[str]:
                """Like find_declared_modules but also strips cfg_if blocks so
                that `mod foo;` inside cfg_if! { ... } is not counted as a real
                unconditional declaration."""
                text = path.read_text(encoding='utf-8')
                cleaned = strip_cfg_if_blocks(strip_comments(text))
                return set(re.findall(r'\bmod\s+(\w+)\s*;', cleaned))

            for i, seg in enumerate(mod_parts_for_import[:-1] if source_file.stem != 'mod' else mod_parts_for_import):
                if not parent_file.exists():
                    validated = False
                    break
                declared = _declared_mods_strict(parent_file)
                if seg not in declared:
                    validated = False
                    break
                # descend: next parent is either seg/mod.rs or seg.rs
                next_mod = parent_file.parent / seg / 'mod.rs'
                if not next_mod.exists():
                    next_mod = parent_file.parent / f'{seg}.rs'
                parent_file = next_mod
            else:
                # Check the final segment too (the file itself must be declared)
                if mod_parts_for_import:
                    final_seg = mod_parts_for_import[-1]
                    # For mod.rs files the last segment is already checked above
                    if source_file.stem != 'mod' and parent_file.exists():
                        declared = _declared_mods_strict(parent_file)
                        if final_seg not in declared:
                            validated = False
        if not validated:
            # Module path couldn't be validated against mod declarations
            # (e.g. declared only inside cfg_if! or via an inline mod block).
            # Skip this contract rather than emitting a broken import.
            print(f'  [skip] {struct_name}: cannot resolve module path '
                  f'{"/".join(mod_parts_for_import)} — skipping sanity generation')
            return None
        mod_path_str = '::'.join(mod_parts_for_import)
        if len(all_local_names) > 1:
            items_str = ', '.join(all_local_names)
            struct_import = f'use crate::{mod_path_str}::{{{items_str}}};'
        else:
            struct_import = f'use crate::{mod_path_str}::{struct_name};'
    else:
        # source_file is crate root (src/lib.rs or src/mod.rs)
        if len(all_local_names) > 1:
            items_str = ', '.join(all_local_names)
            struct_import = f'use crate::{{{items_str}}};'
        else:
            struct_import = f'use crate::{struct_name};'

    lines = [
        f'// Sanity wrappers for [{struct_name}]',
        f'// Auto-generated by generate_sanity.py',
        '',
    ]

    if sdk_types:
        lines.append(f'use soroban_sdk::{{{", ".join(sorted(sdk_types))}}};')

    lines.extend(project_uses)
    lines.extend(explicit_trait_uses)

    lines.append(struct_import)
    lines.append('')

    # All impl methods → add _sanity suffix
    for fns in [direct_fns, trait_fns]:
        for fn in fns:
            suffix = '_' + struct_name + '_sanity'
            wrapped_name = fn['name'] + suffix
            decl = fn_decl(wrapped_name, fn['params'], fn['ret'])
            raw_trait = fn.get('trait_name') if fn.get('is_trait') else None
            if raw_trait:
                # Use the short name (last segment) in UFCS — the explicit import
                # above makes it resolvable:
                #   <Token as Interface>::allowance(e, from, spender)
                ufcs_trait = _trait_short.get(raw_trait, raw_trait)
                body = fn_call_trait(struct_name, ufcs_trait, fn['name'], fn['params'], fn['ret'])
            else:
                body = fn_call(struct_name, fn['name'], fn['params'], fn['ret'])
            lines += [f'{decl} {{', body, '}', '']

    return '\n'.join(lines), all_fns


def resolve_trait_source_crate(trait: dict, project_root: Path) -> tuple[str, str]:
    """
    Return (crate_name_underscored, module_path) for use in the sanity crate.
    e.g. ("stellar_access", "access_control")
    Looks at the nearest Cargo.toml to the trait's source file.
    """
    trait_file = trait['file']
    # Walk up to find Cargo.toml
    candidate = trait_file.parent
    while candidate != project_root.parent:
        cargo = candidate / 'Cargo.toml'
        if cargo.exists():
            content = cargo.read_text()
            m = re.search(r'^name\s*=\s*"([^"]+)"', content, re.MULTILINE)
            if m:
                crate_name = m.group(1).replace('-', '_')
                # Derive module path from file path relative to src/
                try:
                    rel = trait_file.relative_to(candidate / 'src')
                    parts = list(rel.parts)
                    # Remove filename
                    if parts[-1] in ('mod.rs', 'lib.rs'):
                        parts = parts[:-1]
                    else:
                        parts[-1] = parts[-1].replace('.rs', '')
                    module_path = '::'.join(parts) if parts else ''
                except ValueError:
                    module_path = ''
                return crate_name, module_path
        if candidate == project_root:
            break
        candidate = candidate.parent
    return '', ''


def generate_trait_sanity_lib(trait: dict, trait_crate: str, trait_module: str) -> str:
    """Generate sanity/<name>/src/lib.rs for a #[contracttrait] trait."""
    trait_name = trait['name']
    contract_name = f'{trait_name}Contract'
    fns = trait['fns']

    sdk_types = set()
    for fn in fns:
        sdk_types |= collect_soroban_types(fn['params'], fn['ret'])

    # Collect non-SDK, non-primitive types that need importing from the trait crate
    extra_types: set[str] = set()
    for fn in fns:
        all_ty = ' '.join(t for _, t in fn['params'])
        if fn['ret']:
            all_ty += ' ' + fn['ret']
        for t in re.findall(r'\b([A-Z][A-Za-z0-9]*)\b', all_ty):
            if t not in SOROBAN_SDK_TYPES and t not in _RUST_PRIMITIVES and t != trait_name:
                extra_types.add(t)

    # Build the trait import path
    if trait_module:
        trait_base = f'{trait_crate}::{trait_module}'
    else:
        trait_base = trait_crate

    if extra_types:
        all_names = ', '.join(sorted([trait_name] + list(extra_types)))
        trait_import = f'use {trait_base}::{{{all_names}}};'
    else:
        trait_import = f'use {trait_base}::{trait_name};'

    lines = [
        '#![no_std]',
        f'//! Sanity wrappers for [`{trait_name}`]',
        f'//! Auto-generated by generate_sanity.py',
        '',
        trait_import,
        f'use soroban_sdk::{{contract, contractimpl, {", ".join(sorted(sdk_types))}}};',
        '',
        '#[contract]',
        f'struct {contract_name};',
        '',
        '#[contractimpl]',
        f'impl {trait_name} for {contract_name} {{',
    ]
    # Only emit stubs for required methods (no default body in the trait).
    # Methods with default bodies don't need to be overridden.
    required_fns = [fn for fn in fns if not fn.get('has_default', False)]
    for fn in required_fns:
        param_str = ', '.join(f'{n}: {t}' for n, t in fn['params'])
        ret_str = f' -> {fn["ret"]}' if fn['ret'] else ''
        lines.append(f'    fn {fn["name"]}({param_str}){ret_str} {{ todo!() }}')
    lines += ['}', '']

    for fn in fns:
        suffix = '_' + trait_name + '_sanity'
        wrapped_name = fn['name'] + suffix
        decl = fn_decl(wrapped_name, fn['params'], fn['ret'])
        body = fn_call(contract_name, fn['name'], fn['params'], fn['ret'])
        lines += [f'{decl} {{', body, '}', '']

    return '\n'.join(lines)


def generate_trait_cargo_toml(trait: dict, trait_crate_kebab: str,
                               project_root: Path) -> tuple[str, str]:
    """
    Generate Cargo.toml for sanity/<name>/ crate.
    Returns (cargo_toml_content, relative_dir_from_root).
    """
    trait_name = trait['name']
    sanity_name = f'{to_kebab(trait_name)}-sanity'
    sanity_dir = f'sanity/{to_kebab(trait_name)}'

    # Check if project uses a workspace with workspace.dependencies
    ws_cargo = find_workspace_cargo(trait['file'], project_root)
    uses_workspace = False
    if ws_cargo:
        ws_content = ws_cargo.read_text()
        uses_workspace = '[workspace.dependencies]' in ws_content or \
                         'workspace = true' in ws_content

    if uses_workspace:
        cargo = f'''\
[package]
name = "{sanity_name}"
edition.workspace = true
license.workspace = true
repository.workspace = true
version.workspace = true
publish = true
description = "Sanity wrappers for {trait_name}"

[package.metadata.stellar]
cargo_inherit = true

[lib]
crate-type = ["lib", "cdylib"]
doctest = false

[dependencies]
soroban-sdk = {{ workspace = true }}
{trait_crate_kebab} = {{ workspace = true }}

[dev-dependencies]
soroban-sdk = {{ workspace = true, features = ["testutils"] }}
'''
    else:
        # Standalone project — infer soroban-sdk version and crate version
        sdk_version = '23.4.0'
        crate_version = '0.1.0'

        # Try to read versions from closest Cargo.toml
        closest = find_nearest_cargo(trait['file'], project_root)
        if closest:
            c = closest.read_text()
            sdkv = re.search(r'soroban-sdk\s*=\s*["\{][^"]*?(\d+\.\d+\.\d+)', c)
            if sdkv:
                sdk_version = sdkv.group(1)
            vv = re.search(r'^version\s*=\s*"([^"]+)"', c, re.MULTILINE)
            if vv:
                crate_version = vv.group(1)

        cargo = f'''\
[package]
name = "{sanity_name}"
version = "{crate_version}"
edition = "2021"
publish = true
description = "Sanity wrappers for {trait_name}"

[lib]
crate-type = ["lib", "cdylib"]
doctest = false

[dependencies]
soroban-sdk = {{ version = "{sdk_version}", default-features = false }}
{trait_crate_kebab} = "{crate_version}"

[dev-dependencies]
soroban-sdk = {{ version = "{sdk_version}", features = ["testutils"] }}
'''

    return cargo, sanity_dir


def find_workspace_cargo(start: Path, root: Path) -> Optional[Path]:
    """Find a Cargo.toml with [workspace] walking up from start to root."""
    candidate = start.parent
    while True:
        cargo = candidate / 'Cargo.toml'
        if cargo.exists():
            content = cargo.read_text()
            if '[workspace]' in content:
                return cargo
        if candidate == root or candidate == root.parent:
            break
        candidate = candidate.parent
    return None


def find_nearest_cargo(start: Path, root: Path) -> Optional[Path]:
    """Find the nearest Cargo.toml walking up from start to root."""
    candidate = start.parent
    while True:
        cargo = candidate / 'Cargo.toml'
        if cargo.exists():
            return cargo
        if candidate == root or candidate == root.parent:
            break
        candidate = candidate.parent
    return None


def find_crate_name(file_path: Path, root: Path) -> Optional[str]:
    """Return the [package] name from the Cargo.toml nearest to file_path."""
    cargo = find_nearest_cargo(file_path, root)
    if cargo is None:
        return None
    try:
        content = cargo.read_text()
        m = re.search(r'^\[package\].*?^name\s*=\s*"([^"]+)"', content,
                      re.MULTILINE | re.DOTALL)
        return m.group(1) if m else None
    except Exception:
        return None


# ─── Workspace / lib.rs patching ─────────────────────────────────────────────

def patch_workspace_members(ws_cargo: Path, new_member: str, dry_run: bool):
    """Add new_member to [workspace] members list if not already present."""
    content = ws_cargo.read_text()
    if new_member in content:
        print(f'  [skip] {ws_cargo} already lists "{new_member}"')
        return

    # Find the members = [ ... ] block and append
    m = re.search(r'(members\s*=\s*\[)', content)
    if not m:
        print(f'  [warn] cannot find members = [...] in {ws_cargo}', file=sys.stderr)
        return

    insert_at = m.end()
    new_content = content[:insert_at] + f'\n    "{new_member}",' + content[insert_at:]

    if dry_run:
        print(f'  [dry] patch {ws_cargo}: add "{new_member}" to workspace members')
    else:
        ws_cargo.write_text(new_content)
        print(f'  [write] patched {ws_cargo}: added "{new_member}"')


def remove_workspace_member(ws_cargo: Path, member: str, dry_run: bool):
    """Remove member from [workspace] members list if present."""
    content = ws_cargo.read_text()
    # Match lines like:    "sanity/foo", or    "sanity/foo",\n
    new_content = re.sub(r'\n\s*"' + re.escape(member) + r'"\s*,?', '', content)
    if new_content == content:
        return  # wasn't there
    if dry_run:
        print(f'  [dry] patch {ws_cargo}: remove "{member}" from workspace members')
    else:
        ws_cargo.write_text(new_content)
        print(f'  [write] patched {ws_cargo}: removed "{member}"')


def patch_lib_rs(lib_rs: Path, dry_run: bool):
    """Add `pub mod sanity;` to lib.rs if not already present."""
    content = lib_rs.read_text()
    if 'mod sanity' in content:
        print(f'  [skip] {lib_rs} already declares mod sanity')
        return

    new_content = content.rstrip() + '\npub mod sanity;\n'

    if dry_run:
        print(f'  [dry] patch {lib_rs}: append `pub mod sanity;`')
    else:
        lib_rs.write_text(new_content)
        print(f'  [write] patched {lib_rs}')


# ─── Main ─────────────────────────────────────────────────────────────────────

def dedup_use_stmts_in_source(source: str) -> str:
    """Remove duplicate `use ...;` lines from a Rust source string.

    Keeps the first occurrence of each import so that combining multiple
    contract sections into one sanity.rs doesn't produce E0252 errors.
    """
    seen: set[str] = set()
    result: list[str] = []
    for line in source.split('\n'):
        stripped = line.strip()
        if stripped.startswith('use ') and stripped.endswith(';'):
            if stripped not in seen:
                seen.add(stripped)
                result.append(line)
            # else skip the duplicate
        else:
            result.append(line)
    return '\n'.join(result)


def write_file(path: Path, content: str, dry_run: bool, force: bool) -> bool:
    if path.exists() and not force:
        print(f'  [skip] {path} already exists (use --force to overwrite)')
        return False
    if dry_run:
        print(f'\n{"─"*60}')
        print(f'  [dry] would write {path}:')
        print(f'{"─"*60}')
        print(content)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f'  [write] {path}')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('project', help='Path to the Soroban project root')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print output without writing files')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing sanity files')
    parser.add_argument('--include-tests', action='store_true',
                        help='Also generate sanity for contracts found in test files '
                             '(default: skip test.rs / tests.rs / #[cfg(test)] files)')
    args = parser.parse_args()

    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f'error: {root} is not a directory', file=sys.stderr)
        sys.exit(1)

    print(f'Scanning {root} …')
    contracts, traits = scan_project(root, include_tests=args.include_tests)

    print(f'\nFound {len(contracts)} contract(s), {len(traits)} trait(s)\n')

    def fn_summary(fn: dict) -> dict:
        def param_entry(n: str, t: str) -> dict:
            t = t.strip()
            by_ref = t.startswith('&')
            base = t.lstrip('&').strip()
            return {'name': n, 'type': parse_type_str(base), 'by_ref': by_ref}
        ret = fn['ret']
        if ret:
            ret = ret.strip()
            ret_by_ref = ret.startswith('&')
            ret_parsed = {'type': parse_type_str(ret.lstrip('&').strip()), 'by_ref': ret_by_ref}
        else:
            ret_parsed = None
        return {
            'name': fn['name'],
            'params': [param_entry(n, t) for n, t in fn['params']],
            'ret': ret_parsed,
        }

    summary: dict = {
        'project': str(root),
        'contracts': [],
        'traits': [],
        'skipped_traits': [],
    }

    # ── Handle contracts ───────────────────────────────────────────────────
    # Group contracts by their target src/ directory so multiple contracts
    # in the same crate are combined into one sanity.rs
    contracts_by_dir: dict = defaultdict(list)
    for contract in contracts:
        contracts_by_dir[contract['file'].parent].append(contract)

    for src_dir, group in contracts_by_dir.items():
        names = ', '.join(c['name'] for c in group)
        print(f'Contract(s): {names} ({src_dir.relative_to(root)})')

        # Concatenate sanity code for all contracts in this directory,
        # then deduplicate any repeated `use` statements that arise when
        # multiple contracts share the same soroban_sdk or crate imports.
        # Also capture the filtered function lists so the summary reflects
        # exactly what was emitted (Fix-B may drop unresolvable functions).
        sections = []
        emitted_fns_by_contract: dict[str, list[dict]] = {}
        for contract in group:
            result = generate_contract_sanity(contract)
            if result is None:
                continue
            code, emitted_fns = result
            sections.append(code)
            emitted_fns_by_contract[contract['name']] = emitted_fns
        if not sections:
            print(f'  [skip] all contracts in group skipped — no sanity.rs written')
            print()
            continue

        sanity_code = dedup_use_stmts_in_source('\n'.join(sections))

        sanity_path = src_dir / 'sanity.rs'
        write_file(sanity_path, sanity_code, args.dry_run, args.force)

        # Patch lib.rs (or mod.rs) to declare mod sanity
        lib_rs = src_dir / 'lib.rs'
        mod_rs = src_dir / 'mod.rs'
        module_root = lib_rs if lib_rs.exists() else (mod_rs if mod_rs.exists() else None)
        if module_root is not None:
            patch_lib_rs(module_root, dry_run=args.dry_run)
        else:
            print(f'  [warn] no lib.rs or mod.rs found in {src_dir}')

        for contract in group:
            if contract['name'] not in emitted_fns_by_contract:
                continue  # was skipped during generation
            emitted_fns = emitted_fns_by_contract[contract['name']]
            summary['contracts'].append({
                'name': contract['name'],
                'crate': find_crate_name(contract['file'], root),
                'file': str(contract['file'].relative_to(root)),
                'sanity_file': str(sanity_path.relative_to(root)),
                'functions': [fn_summary(fn) for fn in emitted_fns],
            })

        print()

    # ── Handle traits ─────────────────────────────────────────────────────────
    # Skip traits that have any required methods (no default body) — those are
    # abstract and cannot be implemented safely without knowing the concrete
    # semantics.  Traits where every method has a default body are concrete and
    # get a sanity crate with an empty impl (no stubs needed).
    for trait in traits:
        required_methods = [fn['name'] for fn in trait['fns'] if not fn.get('has_default', False)]
        required_types = trait.get('required_types', [])
        is_abstract = bool(required_methods or required_types)
        if is_abstract:
            parts = []
            if required_methods:
                parts.append(f'methods: {", ".join(required_methods)}')
            if required_types:
                parts.append(f'types: {", ".join(required_types)}')
            reason = f'required {"; ".join(parts)}'
            print(f'Skipped trait {trait["name"]} (abstract — {reason})')
            summary['skipped_traits'].append({
                'name': trait['name'],
                'file': str(trait['file'].relative_to(root)),
                'reason': reason,
                'required_methods': required_methods,
                'required_types': required_types,
            })
            # Remove stale workspace entry if it was added by a previous run
            stale_dir = f'sanity/{to_kebab(trait["name"])}'
            ws_cargo = find_workspace_cargo(root / stale_dir, root)
            if ws_cargo:
                remove_workspace_member(ws_cargo, stale_dir, args.dry_run)
            continue

        trait_name = trait['name']
        print(f'Trait: {trait_name} ({trait["file"].relative_to(root)})')

        # Determine the crate name and module for the trait import
        nearest_cargo = find_nearest_cargo(trait['file'], root)
        if nearest_cargo:
            import_re = re.compile(r'\[package\].*?name\s*=\s*"([^"]+)"', re.DOTALL)
            m = import_re.search(nearest_cargo.read_text())
            trait_crate_kebab = m.group(1) if m else to_kebab(trait_name)
        else:
            trait_crate_kebab = to_kebab(trait_name)
        trait_crate = trait_crate_kebab.replace('-', '_')

        # Derive module path from file location relative to src/.
        # e.g. packages/access/src/access_control.rs → "access_control"
        # e.g. packages/access/src/lib.rs            → "" (crate root)
        trait_file = trait['file']
        try:
            # Find the src/ ancestor
            src_anchor = next(
                p for p in reversed(trait_file.parents)
                if p.name == 'src'
            )
            rel_parts = trait_file.relative_to(src_anchor).parts
            # Drop the filename; if it's lib.rs it contributes nothing
            module_parts = list(rel_parts[:-1])
            stem = trait_file.stem
            if stem != 'lib' and stem != 'mod':
                module_parts.append(stem)
            trait_module = '::'.join(module_parts)
        except StopIteration:
            trait_module = ''

        lib_content = generate_trait_sanity_lib(trait, trait_crate, trait_module)
        cargo_content, sanity_dir = generate_trait_cargo_toml(trait, trait_crate_kebab, root)

        lib_path = root / sanity_dir / 'src' / 'lib.rs'
        cargo_path = root / sanity_dir / 'Cargo.toml'

        write_file(lib_path, lib_content, args.dry_run, args.force)
        write_file(cargo_path, cargo_content, args.dry_run, args.force)

        # Register in workspace Cargo.toml
        ws_cargo = find_workspace_cargo(root / sanity_dir, root)
        if ws_cargo:
            patch_workspace_members(ws_cargo, sanity_dir, args.dry_run)
        else:
            print(f'  [warn] no workspace Cargo.toml found — add "{sanity_dir}" to members manually')

        summary['traits'].append({
            'name': trait_name,
            'file': str(trait['file'].relative_to(root)),
            'sanity_file': str(lib_path.relative_to(root)),
            'functions': [fn_summary(fn) for fn in trait['fns']],
        })

        print()

    if not contracts and not traits:
        print('No applicable types found.\n'
              'Looking for: #[contract] structs.')

    # ── Write JSON summary ─────────────────────────────────────────────────
    summary_path = root / 'sanity_summary.json'
    if not args.dry_run:
        summary_path.write_text(json.dumps(summary, indent=2))
        print(f'Summary written to {summary_path.relative_to(root)}')
    else:
        print(f'\n[dry] sanity_summary.json:\n{json.dumps(summary, indent=2)}')


if __name__ == '__main__':
    main()
