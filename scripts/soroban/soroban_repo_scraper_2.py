#!/usr/bin/env python3
"""
soroban_repo_reporter.py
========================
Generate a Markdown contract-function summary from a Soroban/Stellar GitHub
repository, without requiring any local clone or GitHub API authentication.
Uses only raw.githubusercontent.com for file fetching.
 
Handles two layouts:
  • Workspace repos   – root Cargo.toml has [workspace]; one or more member crates
  • Single-crate repos – root Cargo.toml has [package]; src/ is at the root
 
Captures two Soroban annotation styles:
  • #[contracttrait]           – public trait defining the contract interface
  • #[contractimpl]            – impl block (inherent or trait) on a #[contract] struct
 
Usage:
    python soroban_repo_reporter.py <github-url> [options]
 
Examples:
    python soroban_repo_reporter.py https://github.com/owner/repo
    python soroban_repo_reporter.py https://github.com/owner/repo --branch develop
    python soroban_repo_reporter.py https://github.com/owner/repo -o report.md
    python soroban_repo_reporter.py https://github.com/owner/repo --packages-only
    python soroban_repo_reporter.py https://github.com/owner/repo \\
        --include packages/tokens packages/governance
 
Requires:  requests  (pip install requests)
"""
 
import argparse
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
_session.headers.update({"User-Agent": "soroban-repo-reporter/1.0"})
 
 
def _parse_github_url(url: str) -> tuple[str, str]:
    """Return (owner, repo) from a github.com URL."""
    p = urlparse(url.rstrip("/"))
    parts = p.path.strip("/").split("/")
    if len(parts) < 2:
        sys.exit(f"Cannot parse GitHub URL: {url}")
    return parts[0], parts[1].removesuffix(".git")
 
 
def _fetch_raw(owner: str, repo: str, branch: str, path: str) -> Optional[str]:
    """Fetch a file from raw.githubusercontent.com; return None on error."""
    url = f"{RAW_BASE}/{owner}/{repo}/{branch}/{path.lstrip('/')}"
    try:
        r = _session.get(url, timeout=15)
    except requests.RequestException:
        return None
    return r.text if r.status_code == 200 else None
 
 
def _default_branch(owner: str, repo: str) -> str:
    for b in ("main", "master", "develop"):
        r = _session.head(
            f"{RAW_BASE}/{owner}/{repo}/{b}/Cargo.toml", timeout=10
        )
        if r.status_code == 200:
            return b
    sys.exit(
        "Cannot determine default branch — "
        "try specifying one with --branch <name>"
    )
 
 
# ---------------------------------------------------------------------------
# Cargo.toml parsing  (no 'toml' dep required — pure regex)
# ---------------------------------------------------------------------------
 
def _is_workspace(cargo_toml_text: str) -> bool:
    return bool(re.search(r'^\[workspace\]', cargo_toml_text, re.MULTILINE))
 
 
def _parse_package_name(cargo_toml_text: str) -> str:
    m = re.search(r'^\[package\].*?^name\s*=\s*"([^"]+)"',
                  cargo_toml_text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else "unknown"
 
 
def _parse_workspace_members(cargo_toml_text: str) -> list[str]:
    """Return workspace member path patterns from the root Cargo.toml."""
    try:
        import toml as _toml  # type: ignore
        data = _toml.loads(cargo_toml_text)
        members = data.get("workspace", {}).get("members", [])
        return [m for m in members if isinstance(m, str)]
    except Exception:
        pass
    # Regex fallback
    ws_match = re.search(r'\[workspace\](.*?)(?=\n\[|\Z)', cargo_toml_text, re.DOTALL)
    if not ws_match:
        return []
    ws_block = ws_match.group(1)
    m = re.search(r'members\s*=\s*\[(.*?)\]', ws_block, re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))
 
 
def _local_package_names_from_lock(owner: str, repo: str, branch: str) -> list[str]:
    """
    Parse Cargo.lock to find locally-defined packages (no 'source' field).
    Returns a list of package names.
    """
    lock_text = _fetch_raw(owner, repo, branch, "Cargo.lock")
    if not lock_text:
        return []
    names: list[str] = []
    for block in re.split(r'\n\[\[package\]\]', lock_text):
        if re.search(r'\bsource\s*=', block):
            continue          # external dependency — skip
        m = re.search(r'\bname\s*=\s*"([^"]+)"', block)
        if m:
            names.append(m.group(1))
    return names
 
 
def _expand_glob_member(
    owner: str, repo: str, branch: str, pattern: str
) -> list[str]:
    """Expand a single-level glob pattern like 'contracts/*' to real paths.
 
    Strategy (tried in order):
      1. Look for a sub-workspace Cargo.toml inside the glob parent directory.
      2. Fall back to Cargo.lock: probe {parent}/{name}/Cargo.toml for every
         local package name found in Cargo.lock.
    """
    if "*" not in pattern and "?" not in pattern:
        return [pattern]
    parent = pattern.split("/*")[0].split("/?")[0]
 
    # ── strategy 1: parent has its own Cargo.toml with [workspace] ──────────
    parent_cargo = _fetch_raw(owner, repo, branch, f"{parent}/Cargo.toml")
    if parent_cargo:
        nested = _parse_workspace_members(parent_cargo)
        results = []
        for nm in nested:
            results.append(nm if "/" in nm else f"{parent}/{nm}")
        if results:
            return results
 
    # ── strategy 2: probe each local package from Cargo.lock ────────────────
    local_names = _local_package_names_from_lock(owner, repo, branch)
    results = []
    for name in local_names:
        candidate = f"{parent}/{name}"
        if _fetch_raw(owner, repo, branch, f"{candidate}/Cargo.toml"):
            results.append(candidate)
    return results
 
 
def _resolve_members(
    owner: str, repo: str, branch: str, raw_members: list[str]
) -> list[str]:
    result: list[str] = []
    for m in raw_members:
        if "*" in m or "?" in m:
            result.extend(_expand_glob_member(owner, repo, branch, m))
        else:
            result.append(m)
    seen: set[str] = set()
    return [p for p in result if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
 
 
# ---------------------------------------------------------------------------
# Rust source-code crawling (mod-declaration BFS)
# ---------------------------------------------------------------------------
 
_MOD_DECL_RE = re.compile(r'(?:pub\s+)?mod\s+(\w+)\s*;')
 
 
def _child_dir_of(file_path: str) -> str:
    """Directory where a file's child modules live."""
    base = file_path.rsplit("/", 1)[0]
    stem = PurePosixPath(file_path).stem
    return base if stem in ("mod", "lib", "main") else f"{base}/{stem}"
 
 
def _list_rust_files(
    owner: str, repo: str, branch: str, package_path: str, verbose: bool = False
) -> dict[str, str]:
    """
    BFS starting from <package_path>/src/lib.rs following mod declarations.
    Returns {file_path: source_text}.
    package_path="" means the repo root (single-crate layout).
    """
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
            short = "/".join(path.split("/")[-3:])
            print(f"    found: …/{short}", file=sys.stderr)
        return True
 
    for entry in (f"{src_root}/lib.rs", f"{src_root}/main.rs"):
        try_enqueue(entry)
 
    while queue:
        file_path = queue.popleft()
        src = sources[file_path]
        child_dir = _child_dir_of(file_path)
        for m in _MOD_DECL_RE.finditer(src):
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
# Rust source parsing  — traits AND impl blocks
# ---------------------------------------------------------------------------
 
@dataclass
class FnSig:
    name: str
    params: str   # content inside the outer parens
    ret: str      # return type, or "" if ()
    doc: str = "" # leading /// doc comment
 
 
@dataclass
class ContractTrait:
    name: str
    annotation: str
    functions: list[FnSig] = field(default_factory=list)
    doc: str = ""
    source_file: str = ""
 
 
@dataclass
class ImplBlock:
    """A #[contractimpl] impl block on a #[contract] struct."""
    contract_name: str         # the struct being implemented
    trait_name: Optional[str]  # None for inherent methods
    annotation: str            # "#[contractimpl]" etc.
    functions: list[FnSig] = field(default_factory=list)
    source_file: str = ""
 
 
# Patterns
_DOC_LINE_RE = re.compile(r'^\s*///\s?(.*)$')
_ATTR_LINE_RE = re.compile(r'^\s*#\[([^\]]+)\]')
_TRAIT_RE    = re.compile(r'pub\s+trait\s+(\w+)')
# impl [Trait for] Struct  (optional generic params)
_IMPL_RE     = re.compile(
    r'impl\s*(?:<[^>]*>)?\s*'
    r'(?:(\w+(?:<[^>]*>)?)\s+for\s+)?'  # group 1: optional trait name
    r'(\w+)'                             # group 2: struct name
    r'\s*(?:<[^>]*>)?\s*\{'
)
# fn signatures — stops at the first { or ; after the return type
_FN_RE = re.compile(
    r'\bfn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?:->\s*([^{;]+?))?[{;]'
)
 
 
def _extract_body(lines: list[str], start: int) -> tuple[str, int]:
    """
    Starting at line `start` (which must contain '{'), collect all lines
    until the matching '}' is found (brace-depth tracking).
    Returns (flattened_body, next_line_index).
    """
    brace_depth = 0
    body_parts: list[str] = []
    i = start
    n = len(lines)
    # Find the opening brace (may be on the same line as 'impl' or next line)
    while i < n:
        l = lines[i]
        opens  = l.count('{')
        closes = l.count('}')
        if opens > 0 and brace_depth == 0:
            # Emit everything after the first '{'
            after = l.split('{', 1)[1]
            brace_depth = 1 + opens - 1 - closes
            body_parts.append(after)
            i += 1
            break
        i += 1
    while i < n and brace_depth > 0:
        l = lines[i]
        brace_depth += l.count('{') - l.count('}')
        body_parts.append(l)
        i += 1
    flat = re.sub(r'\s+', ' ', ' '.join(body_parts))
    return flat, i
 
 
def _fns_from_body(body_flat: str, pub_only: bool) -> list[FnSig]:
    """Extract function signatures from a flattened block body."""
    fns: list[FnSig] = []
    pattern = r'\bpub\s+fn\b' if pub_only else r'\bfn\b'
    for fm in _FN_RE.finditer(body_flat):
        # For pub_only, skip functions not preceded by 'pub'
        if pub_only:
            prefix = body_flat[max(0, fm.start()-10):fm.start()]
            if 'pub' not in prefix:
                continue
        name   = fm.group(1)
        params = re.sub(r'\s+', ' ', (fm.group(2) or "").strip())
        ret    = re.sub(r'\s+', ' ', (fm.group(3) or "").strip().rstrip('{').strip())
        fns.append(FnSig(name=name, params=params, ret=ret))
    return fns
 
 
def _parse_source(
    source: str, filename: str = ""
) -> tuple[list[ContractTrait], list[ImplBlock]]:
    """
    Parse one .rs file and return:
      • ContractTrait objects   for #[contracttrait] pub trait …
      • ImplBlock objects       for #[contractimpl] impl …
    """
    traits: list[ContractTrait] = []
    impls:  list[ImplBlock]     = []
    lines   = source.splitlines()
    n       = len(lines)
    i       = 0
 
    while i < n:
        # Collect doc comments
        doc_lines: list[str] = []
        while i < n and _DOC_LINE_RE.match(lines[i]):
            doc_lines.append(_DOC_LINE_RE.match(lines[i]).group(1).strip())
            i += 1
 
        # Collect attribute lines
        attrs: list[str] = []
        while i < n and _ATTR_LINE_RE.match(lines[i]):
            attrs.append(_ATTR_LINE_RE.match(lines[i]).group(1).strip())
            i += 1
 
        if i >= n:
            break
 
        cur = lines[i]
        doc = " ".join(doc_lines)
 
        # ── #[contracttrait] pub trait Name { … } ──────────────────────────
        if any(a.startswith("contracttrait") for a in attrs):
            tm = _TRAIT_RE.search(cur)
            if tm:
                trait_name = tm.group(1)
                annotation = "#[" + next(
                    a for a in attrs if a.startswith("contracttrait")
                ) + "]"
                # collect body starting from current line
                body_flat, i = _extract_body(lines, i)
                fns = _fns_from_body(body_flat, pub_only=False)
                traits.append(ContractTrait(
                    name=trait_name, annotation=annotation,
                    functions=fns, doc=doc, source_file=filename,
                ))
                continue
 
        # ── plain pub trait Name { … }  (no #[contracttrait] annotation) ───
        # Captured tentatively; filtered later to keep only those that
        # appear as the trait in a same-package #[contractimpl] impl block.
        if not attrs:
            tm = _TRAIT_RE.search(cur)
            if tm:
                body_flat, i = _extract_body(lines, i)
                fns = _fns_from_body(body_flat, pub_only=False)
                traits.append(ContractTrait(
                    name=tm.group(1), annotation="pub trait",
                    functions=fns, doc=doc, source_file=filename,
                ))
                continue
 
        # ── #[contractimpl...] impl [Trait for] Struct { … } ───────────────
        if any(a.startswith("contractimpl") for a in attrs):
            annotation = "#[" + next(
                a for a in attrs if a.startswith("contractimpl")
            ) + "]"
            # The impl line might be the current line, or span a few lines
            # (e.g. when the impl keyword has a where clause)
            impl_text = " ".join(lines[i:min(i+5, n)])
            im = _IMPL_RE.search(impl_text)
            if im:
                trait_name    = im.group(1)  # None if inherent
                contract_name = im.group(2)
                is_inherent   = trait_name is None
                body_flat, i  = _extract_body(lines, i)
                fns = _fns_from_body(body_flat, pub_only=is_inherent)
                impls.append(ImplBlock(
                    contract_name=contract_name,
                    trait_name=trait_name,
                    annotation=annotation,
                    functions=fns,
                    source_file=filename,
                ))
                continue
 
        i += 1
 
    return traits, impls
 
 
# ---------------------------------------------------------------------------
# Package analysis
# ---------------------------------------------------------------------------
 
@dataclass
class Package:
    path: str          # e.g. "packages/tokens" or "" for single-crate root
    name: str          # display name
    traits: list[ContractTrait] = field(default_factory=list)
    impls:  list[ImplBlock]     = field(default_factory=list)
 
    def has_content(self) -> bool:
        return bool(self.traits or self.impls)
 
 
def _analyse_package(
    owner: str, repo: str, branch: str,
    pkg_path: str, pkg_name: str,
    verbose: bool,
) -> Package:
    pkg = Package(path=pkg_path, name=pkg_name)
    sources = _list_rust_files(owner, repo, branch, pkg_path, verbose)
    if verbose:
        print(f"  [{pkg_path or 'root'}] {len(sources)} .rs files crawled", file=sys.stderr)
    for rs_path, src in sources.items():
        t_list, i_list = _parse_source(src, rs_path)
        if (t_list or i_list) and verbose:
            tnames = [t.name for t in t_list]
            inames = [f"{b.contract_name}::{b.trait_name or '<inherent>'}" for b in i_list]
            print(f"    traits={tnames}  impls={inames}", file=sys.stderr)
        pkg.traits.extend(t_list)
        pkg.impls.extend(i_list)
 
    # ── Post-processing: filter tentative plain-trait captures ──────────────
    # Keep a plain `pub trait` only when a #[contractimpl] impl block in this
    # same package actually implements it.  This avoids polluting the report
    # with internal helper traits or third-party re-exports.
    impl_trait_names: set[str] = {
        ib.trait_name for ib in pkg.impls if ib.trait_name
    }
    pkg.traits = [
        ct for ct in pkg.traits
        if ct.annotation != "pub trait" or ct.name in impl_trait_names
    ]
 
    return pkg
 
 
# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------
 
def _render_fns(fns: list[FnSig]) -> list[str]:
    if not fns:
        return ["_No functions detected._", ""]
    lines = ["| Function | Parameters | Returns |", "|---|---|---|"]
    for fn in fns:
        p = fn.params.replace("|", "\\|")
        r = fn.ret.replace("|", "\\|") or "()"
        lines.append(f"| `{fn.name}` | `{p}` | `{r}` |")
    lines.append("")
    return lines
 
 
def _render_trait(ct: ContractTrait, h: int = 3) -> str:
    hh = "#" * h
    ls: list[str] = [f"{hh} `{ct.name}`", ""]
    if ct.doc:
        ls += [ct.doc, ""]
    ls.append(f"**Annotation:** `{ct.annotation}`")
    if ct.source_file:
        ls.append(f"**Source:** `…/{'/'.join(ct.source_file.split('/')[-3:])}`")
    ls.append("")
    ls.extend(_render_fns(ct.functions))
    return "\n".join(ls)
 
 
def _render_impls(impls: list[ImplBlock], h: int = 3) -> str:
    """Group impl blocks by contract_name for display."""
    # Group: contract_name → list of ImplBlock
    by_contract: dict[str, list[ImplBlock]] = {}
    for ib in impls:
        by_contract.setdefault(ib.contract_name, []).append(ib)
 
    hh = "#" * h
    lines: list[str] = []
    for contract_name, blocks in by_contract.items():
        lines += [f"{hh} `{contract_name}` *(#[contract] struct)*", ""]
        source = blocks[0].source_file
        if source:
            lines.append(f"**Source:** `…/{'/'.join(source.split('/')[-3:])}`")
            lines.append("")
 
        for ib in blocks:
            hh2 = "#" * (h + 1)
            if ib.trait_name:
                lines += [f"{hh2} Implements `{ib.trait_name}`", ""]
            else:
                lines += [f"{hh2} Inherent methods", ""]
            lines.append(f"**Annotation:** `{ib.annotation}`")
            lines.append("")
            lines.extend(_render_fns(ib.functions))
 
    return "\n".join(lines)
 
 
def _generate_json(
    owner: str, repo: str, branch: str, packages: list[Package],
) -> str:
    import json
 
    def fn_to_dict(fn: FnSig) -> dict:
        # Split raw params string into a list of {name, type} dicts
        params = []
        for part in fn.params.split(","):
            part = part.strip().rstrip(",")
            if not part:
                continue
            if ":" in part:
                pname, ptype = part.split(":", 1)
                params.append({"name": pname.strip(), "type": ptype.strip()})
            else:
                params.append({"name": part, "type": ""})
        return {
            "name": fn.name,
            "params": params,
            "returns": fn.ret or "()",
        }
 
    def trait_to_dict(ct: ContractTrait) -> dict:
        kind = (
            "contracttrait" if ct.annotation.startswith("#[contracttrait")
            else "pub_trait"
        )
        return {
            "name": ct.name,
            "kind": kind,
            "annotation": ct.annotation,
            "doc": ct.doc or None,
            "source_file": ct.source_file or None,
            "functions": [fn_to_dict(f) for f in ct.functions],
        }
 
    def impl_to_dict(ib: ImplBlock) -> dict:
        return {
            "trait": ib.trait_name,          # null for inherent methods
            "annotation": ib.annotation,
            "source_file": ib.source_file or None,
            "functions": [fn_to_dict(f) for f in ib.functions],
        }
 
    # Group impl blocks by contract_name within each package
    pkg_list = []
    for pkg in packages:
        if not pkg.has_content():
            continue
        # Collect contract structs from impl blocks
        contracts: dict[str, dict] = {}
        for ib in pkg.impls:
            if ib.contract_name not in contracts:
                contracts[ib.contract_name] = {
                    "name": ib.contract_name,
                    "kind": "contract",
                    "source_file": ib.source_file or None,
                    "impl_blocks": [],
                }
            contracts[ib.contract_name]["impl_blocks"].append(impl_to_dict(ib))
 
        pkg_list.append({
            "path": pkg.path or None,
            "name": pkg.name,
            "traits": [trait_to_dict(t) for t in pkg.traits],
            "contracts": list(contracts.values()),
        })
 
    root = {
        "repository": f"https://github.com/{owner}/{repo}",
        "branch": branch,
        "generator": "soroban_repo_reporter.py",
        "packages": pkg_list,
    }
    return json.dumps(root, indent=2)
 
 
def _generate_report(
    owner: str, repo: str, branch: str, packages: list[Package],
) -> str:
    repo_url = f"https://github.com/{owner}/{repo}"
    pkgs = [p for p in packages if p.has_content()]
    total_traits = sum(len(p.traits) for p in pkgs)
    total_impls  = sum(len(p.impls)  for p in pkgs)
 
    lines: list[str] = []
    lines += [
        f"# Soroban Contract Summary: `{owner}/{repo}`", "",
        f"Repository: [{repo_url}]({repo_url})  ",
        f"Branch: `{branch}`", "",
        f"Auto-generated by `soroban_repo_reporter.py`. "
        f"Found **{total_traits}** contract trait definitions and "
        f"**{total_impls}** `#[contractimpl]` blocks across "
        f"**{len(pkgs)}** package(s).",
        "", "---", "",
    ]
 
    # Table of contents
    lines += ["## Table of Contents", ""]
    for idx, pkg in enumerate(pkgs, 1):
        display = pkg.path or pkg.name
        anchor  = (pkg.path or pkg.name).lower().replace("/", "-").replace("_", "-")
        lines.append(f"{idx}. [{display}](#{anchor})")
        for ct in pkg.traits:
            lines.append(f"   - [trait `{ct.name}`](#{ct.name.lower()})")
        # Group impl blocks by contract name
        seen: set[str] = set()
        for ib in pkg.impls:
            if ib.contract_name not in seen:
                seen.add(ib.contract_name)
                lines.append(f"   - [struct `{ib.contract_name}`](#{ib.contract_name.lower()})")
    lines += ["", "---", ""]
 
    # Body
    for idx, pkg in enumerate(pkgs, 1):
        display = pkg.path or pkg.name
        anchor  = (pkg.path or pkg.name).lower().replace("/", "-").replace("_", "-")
        lines += [f"## {idx}. `{display}` {{#{anchor}}}", ""]
        if pkg.traits:
            lines += ["### Contract Traits (`#[contracttrait]`)", ""]
            for ct in pkg.traits:
                lines.append(_render_trait(ct, h=4))
        if pkg.impls:
            lines += ["### Contract Implementations (`#[contractimpl]`)", ""]
            lines.append(_render_impls(pkg.impls, h=4))
 
    lines += [
        "---", "",
        "_Report generated by `soroban_repo_reporter.py`. "
        "Signatures extracted by static regex parsing — "
        "macro-generated methods may not appear._", "",
    ]
    return "\n".join(lines)
 
 
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
 
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a Soroban contract summary from a GitHub repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python soroban_repo_reporter.py https://github.com/owner/repo
              python soroban_repo_reporter.py https://github.com/owner/repo \\
                  --branch develop -o report.md
              python soroban_repo_reporter.py https://github.com/owner/repo \\
                  --packages-only -v
        """),
    )
    parser.add_argument("url", help="GitHub repository URL")
    parser.add_argument("--branch", "-b", default=None,
                        help="Branch name (default: auto-detected)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file (default: stdout)")
    parser.add_argument("--packages-only", action="store_true",
                        help="Only scan packages/ members, skip examples/")
    parser.add_argument("--include", nargs="+", metavar="PATH",
                        help="Only scan these package paths")
    parser.add_argument("--format", "-f", choices=["markdown", "json"],
                        default="json",
                        help="Output format: json (default) or markdown")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print progress to stderr")
    args = parser.parse_args()
 
    owner, repo = _parse_github_url(args.url)
    branch = args.branch or _default_branch(owner, repo)
    if args.verbose:
        print(f"Repository: {owner}/{repo}  branch: {branch}", file=sys.stderr)
 
    cargo_text = _fetch_raw(owner, repo, branch, "Cargo.toml")
    if cargo_text is None:
        sys.exit("ERROR: Could not fetch root Cargo.toml.")
 
    packages: list[Package] = []
 
    if _is_workspace(cargo_text):
        # ── Workspace layout ────────────────────────────────────────────────
        raw_members = _parse_workspace_members(cargo_text)
        if args.verbose:
            print(f"Workspace members: {raw_members}", file=sys.stderr)
        all_members = _resolve_members(owner, repo, branch, raw_members)
 
        if args.include:
            members = [m for m in all_members if m in args.include]
            if not members:
                sys.exit(f"None of the --include paths matched. Available: {all_members}")
        elif args.packages_only:
            members = [m for m in all_members if m.startswith("packages/")]
        else:
            members = all_members
 
        if not members:
            sys.exit("No packages to scan.")
 
        if args.verbose:
            print(f"Scanning {len(members)} package(s) …", file=sys.stderr)
 
        for pkg_path in members:
            pkg_name = PurePosixPath(pkg_path).name
            if args.verbose:
                print(f"\nPackage: {pkg_path}", file=sys.stderr)
            pkg = _analyse_package(owner, repo, branch, pkg_path, pkg_name, args.verbose)
            packages.append(pkg)
 
    else:
        # ── Single-crate layout ─────────────────────────────────────────────
        pkg_name = _parse_package_name(cargo_text)
        if args.verbose:
            print(f"Single-crate repo: {pkg_name}", file=sys.stderr)
        pkg = _analyse_package(owner, repo, branch, "", pkg_name, args.verbose)
        packages.append(pkg)
 
    # Sort: packages with content first
    packages.sort(key=lambda p: (not p.has_content(), p.path or p.name))
 
    if args.format == "json":
        report = _generate_json(owner, repo, branch, packages)
    else:
        report = _generate_report(owner, repo, branch, packages)
 
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report written to: {args.output}")
    else:
        print(report)
 
 
if __name__ == "__main__":
    main()
