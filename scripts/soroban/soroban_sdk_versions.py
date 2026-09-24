#!/usr/bin/env python3
"""
soroban_sdk_versions.py
-----------------------
Reports which version(s) of soroban-sdk a Soroban workspace (or single crate)
depends on — directly and transitively — by parsing the Cargo.lock file.

Usage:
    python3 soroban_sdk_versions.py [path-to-project-or-Cargo.lock] [--json]

If no path is given the current directory is used.
--json  Emit machine-readable JSON instead of coloured text.

Supports Cargo.lock format versions 3 and 4.
"""

import os
import sys
import re
import json
from pathlib import Path
from collections import defaultdict


# ── Cargo.lock parser ──────────────────────────────────────────────────────────

def parse_lock(lock_text: str) -> dict:
    packages = {}
    blocks = re.split(r'\[\[package\]\]', lock_text)
    for block in blocks[1:]:
        name_m   = re.search(r'^name\s*=\s*"([^"]+)"',    block, re.MULTILINE)
        ver_m    = re.search(r'^version\s*=\s*"([^"]+)"', block, re.MULTILINE)
        source_m = re.search(r'^source\s*=\s*"([^"]+)"',  block, re.MULTILINE)
        if not (name_m and ver_m):
            continue
        name    = name_m.group(1)
        version = ver_m.group(1)
        source  = source_m.group(1) if source_m else "(path)"

        deps_m = re.search(r'^dependencies\s*=\s*\[(.*?)\]', block,
                           re.MULTILINE | re.DOTALL)
        deps = []
        if deps_m:
            raw = deps_m.group(1)
            quoted = re.findall(r'"([^"]+)"', raw)
            entries = quoted if quoted else [l.strip() for l in raw.splitlines() if l.strip()]
            for entry in entries:
                parts    = entry.split()
                dep_name = parts[0]
                dep_ver  = parts[1] if len(parts) > 1 else None
                deps.append((dep_name, dep_ver))

        packages[(name, version)] = {"source": source, "deps": deps}
    return packages


# ── Graph helpers ──────────────────────────────────────────────────────────────

def resolve_dep(name, hint_ver, packages):
    if hint_ver and (name, hint_ver) in packages:
        return (name, hint_ver)
    candidates = [k for k in packages if k[0] == name]
    return candidates[0] if len(candidates) == 1 else None


def sdk_versions_in_lock(packages):
    return sorted({ver for (nm, ver) in packages if nm == "soroban-sdk"})


def build_dep_graph(packages):
    graph = {}
    for key, info in packages.items():
        resolved = []
        for dep_name, dep_ver in info["deps"]:
            r = resolve_dep(dep_name, dep_ver, packages)
            if r:
                resolved.append(r)
        graph[key] = resolved
    return graph


def reverse_graph(graph):
    rev = defaultdict(set)
    for pkg, deps in graph.items():
        for dep in deps:
            rev[dep].add(pkg)
    return rev


def transitive_sdk_versions(pkg_key, graph, sdk_ver_set):
    visited, stack, found = set(), [pkg_key], set()
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        if cur[0] == "soroban-sdk" and cur[1] in sdk_ver_set:
            found.add(cur[1])
            continue
        stack.extend(graph.get(cur, []))
    return found


def find_workspace_members(packages):
    return sorted(k for k, v in packages.items() if v["source"] == "(path)")


# ── Cargo.toml declared specs ──────────────────────────────────────────────────

SKIP_DIRS = {"target", ".git", "node_modules"}

_DEP_SECTION_RE = re.compile(
    r'^(?:target\.[^\]]+\.)?(?:dev-|build-)?dependencies$|^workspace\.dependencies$')


def _iter_cargo_tomls(root: Path):
    """Yield every Cargo.toml under root, skipping build output and VCS dirs."""
    for toml in sorted(root.rglob("Cargo.toml")):
        rel_parts = toml.relative_to(root).parts[:-1]
        if any(p in SKIP_DIRS for p in rel_parts):
            continue
        yield toml


def _sdk_entries(text: str):
    """Yield (section, value) for every soroban-sdk dependency entry in a
    Cargo.toml.  `value` is either a version string or a dict of keys
    (version / workspace / package).  Handles:
      soroban-sdk = "21.6"
      soroban-sdk = { version = "21.6", ... }
      soroban-sdk = { workspace = true, ... }
      [dependencies.soroban-sdk] / [dev-dependencies.soroban-sdk] tables
      renamed deps: sdk = { package = "soroban-sdk", version = "..." }
    in [dependencies], [dev-dependencies], [build-dependencies],
    [target.*.dependencies] and [workspace.dependencies].
    """
    section = None          # current [header]
    table_dep = None        # dict being filled for [x.soroban-sdk] tables
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        hm = re.match(r'^\[\s*([^\[\]]+?)\s*\]$', line)
        if hm:
            if table_dep is not None:
                yield table_dep.pop("__section"), table_dep
            table_dep = None
            section = hm.group(1).replace('"', "")
            base, _, dep = section.rpartition(".")
            if dep == "soroban-sdk" and _DEP_SECTION_RE.match(base):
                table_dep = {"__section": base}
            continue
        if table_dep is not None:
            km = re.match(r'^(version|workspace|package)\s*=\s*("?)([^"]+)\2', line)
            if km:
                table_dep[km.group(1)] = km.group(3).strip()
            continue
        if not (section and _DEP_SECTION_RE.match(section)):
            continue
        dm = re.match(r'^"?([A-Za-z0-9_-]+)"?\s*=\s*(.+)$', line)
        if not dm:
            continue
        key, rhs = dm.group(1), dm.group(2).strip()
        if rhs.startswith('"'):
            if key == "soroban-sdk":
                yield section, rhs.strip('"')
            continue
        if rhs.startswith("{"):
            fields = dict(re.findall(r'(version|workspace|package)\s*=\s*"?([^",}]+)"?', rhs))
            fields = {k: v.strip() for k, v in fields.items()}
            if key == "soroban-sdk" or fields.get("package") == "soroban-sdk":
                yield section, fields
    if table_dep is not None:
        yield table_dep.pop("__section"), table_dep


def _concrete_spec(value):
    if isinstance(value, str):
        return value
    return value.get("version")


def workspace_sdk_spec(start: Path):
    """Walk upward from `start` to the workspace root and return the
    soroban-sdk spec from its [workspace.dependencies], if any."""
    for d in [start, *start.parents]:
        toml = d / "Cargo.toml"
        if not toml.is_file():
            continue
        text = toml.read_text(errors="replace")
        if not re.search(r'^\s*\[workspace\]', text, re.MULTILINE) and \
           "workspace.dependencies" not in text:
            continue
        for section, value in _sdk_entries(text):
            if section == "workspace.dependencies":
                spec = _concrete_spec(value)
                if spec:
                    return spec, toml
    return None, None


def read_cargo_toml_sdk_specs(project_root: Path) -> dict:
    """Return {relative_toml_path: version_spec} for every Cargo.toml that
    declares a concrete soroban-sdk version (the first one found in the file).

    `workspace = true` entries carry no version of their own and are not
    listed here; the workspace root's [workspace.dependencies] entry is.
    """
    results = {}
    for toml in _iter_cargo_tomls(project_root):
        try:
            text = toml.read_text(errors="replace")
        except OSError:
            continue
        for _section, value in _sdk_entries(text):
            spec = _concrete_spec(value)
            if spec:
                results[str(toml.relative_to(project_root))] = spec
                break
    return results


def _crate_inherits_workspace_sdk(crate_dir: Path) -> bool:
    toml = crate_dir / "Cargo.toml"
    if not toml.is_file():
        return False
    return any(isinstance(v, dict) and v.get("workspace") == "true"
               for _s, v in _sdk_entries(toml.read_text(errors="replace")))


# ── Version summary helpers ────────────────────────────────────────────────────

def all_versions_mentioned(lock_versions: list, toml_specs: dict) -> dict:
    """
    Collect every SDK version string that appears anywhere:
      - resolved in Cargo.lock  (source: "lock")
      - declared in a Cargo.toml (source: "toml")
    Returns {version_string: {"sources": [...], "toml_files": [...]}}
    """
    summary = {}

    for v in lock_versions:
        summary.setdefault(v, {"sources": [], "toml_files": []})
        summary[v]["sources"].append("lock")

    for path, spec in toml_specs.items():
        summary.setdefault(spec, {"sources": [], "toml_files": []})
        if "toml" not in summary[spec]["sources"]:
            summary[spec]["sources"].append("toml")
        summary[spec]["toml_files"].append(path)

    return dict(sorted(summary.items()))


# ── JSON output ────────────────────────────────────────────────────────────────

def build_json(packages, lock_versions, graph, rev, members, toml_specs):
    sdk_ver_set = set(lock_versions)

    # direct dependents per lock version
    direct_by_version = {}
    for v in lock_versions:
        key = ("soroban-sdk", v)
        dependents = sorted(rev.get(key, []))
        direct_by_version[v] = [
            {
                "name":    n,
                "version": ver,
                "local":   packages[(n, ver)]["source"] == "(path)",
            }
            for n, ver in dependents
        ]

    # workspace members → transitive sdk versions
    workspace = []
    for key in members:
        sdk_found = sorted(transitive_sdk_versions(key, graph, sdk_ver_set))
        if sdk_found:
            workspace.append({
                "name":            key[0],
                "version":         key[1],
                "sdk_versions":    sdk_found,
                "version_conflict": len(sdk_found) > 1,
            })

    # version summary
    version_summary = all_versions_mentioned(lock_versions, toml_specs)

    latest = max(lock_versions, key=_semver_key) if lock_versions else None

    return {
        "lock_versions": lock_versions,
        "latest_version": latest,
        "multiple_lock_versions": len(lock_versions) > 1,
        "version_summary": {
            ver: {
                "sources":    info["sources"],
                "toml_files": info["toml_files"] if info["toml_files"] else None,
            }
            for ver, info in version_summary.items()
        },
        "direct_dependents_by_sdk_version": direct_by_version,
        "workspace_crates": workspace,
        "cargo_toml_specs": toml_specs,
    }


# ── Coloured text output ───────────────────────────────────────────────────────

BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
RESET  = "\033[0m"

def header(title):
    width = 72
    print(f"\n{BOLD}{'─' * width}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─' * width}{RESET}")


def print_text(data, packages=None, lock_label=None, summary=True):
    """Print one lock's analysis.  `summary=False` omits the lock+toml
    summary and Cargo.toml sections (printed once globally instead)."""
    lock_versions = data["lock_versions"]
    toml_specs    = data["cargo_toml_specs"] if summary else {}

    # 1. Versions in lock
    where = f"  ({lock_label})" if lock_label else ""
    header(f"soroban-sdk versions present in Cargo.lock{where}")
    color = RED if data["multiple_lock_versions"] else GREEN
    for v in lock_versions:
        print(f"  {color}• {v}{RESET}")
    if not lock_versions:
        print(f"  {YELLOW}(none — soroban-sdk is not in this lock file){RESET}")
    if data["multiple_lock_versions"]:
        print(f"\n  {YELLOW}⚠  Multiple versions detected — may cause ABI mismatches.{RESET}")

    if summary:
        print_version_summary(data["version_summary"])

    _print_lock_details(data, toml_specs)


def print_version_summary(vs):
    header("All soroban-sdk versions mentioned (lock + Cargo.toml)")
    for ver, info in sorted(vs.items(), key=lambda kv: _semver_key(kv[0])):
        in_lock = "lock" in info["sources"]
        in_toml = "toml" in info["sources"]
        tags = []
        if in_lock:
            tags.append(f"{GREEN}resolved in lock{RESET}")
        if in_toml:
            tags.append(f"{CYAN}declared in toml{RESET}")
        tag_str = ", ".join(tags)
        print(f"  {BOLD}{ver}{RESET}  [{tag_str}]")
        for f in info.get("lock_files") or []:
            print(f"    {f}  {GREEN}(lock){RESET}")
        if info["toml_files"]:
            for f in info["toml_files"]:
                print(f"    {f}")


def _print_lock_details(data, toml_specs):
    # 3. Direct dependents
    header("Direct dependents of each soroban-sdk version")
    for v, deps in data["direct_dependents_by_sdk_version"].items():
        print(f"\n  soroban-sdk {BOLD}{v}{RESET}  ({len(deps)} direct dependent(s))")
        for d in deps:
            local = f"  {YELLOW}← local{RESET}" if d["local"] else ""
            print(f"    {CYAN}{d['name']} {d['version']}{RESET}{local}")

    # 4. Workspace crates
    ws = data["workspace_crates"]
    if ws:
        header("Workspace / local crates → soroban-sdk version(s) they reach")
        for crate in ws:
            c = RED if crate["version_conflict"] else GREEN
            sdk_str = ", ".join(crate["sdk_versions"])
            print(f"  {crate['name']} {crate['version']}  →  {c}{sdk_str}{RESET}")

    # 5. Cargo.toml specs
    print_toml_specs(toml_specs)
    print()


def print_toml_specs(toml_specs):
    if toml_specs:
        header("soroban-sdk version specs declared in Cargo.toml files")
        for path, spec in toml_specs.items():
            print(f"  {path}")
            print(f"    version = {CYAN}\"{spec}\"{RESET}")


# ── Toml-only fallback (no Cargo.lock present) ────────────────────────────────

def _semver_key(v: str) -> tuple:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _report_toml_only(project_root: Path, emit_json: bool) -> None:
    """Report soroban-sdk specs from Cargo.toml when no Cargo.lock is available."""
    print("Note: no Cargo.lock found; reading declared specs from Cargo.toml files.",
          file=sys.stderr)
    toml_specs = read_cargo_toml_sdk_specs(project_root)
    if _crate_inherits_workspace_sdk(project_root):
        # Run inside a member crate that uses `soroban-sdk = { workspace = true }`:
        # the version lives in the workspace root's Cargo.toml.
        spec, ws_toml = workspace_sdk_spec(project_root.parent)
        if spec:
            toml_specs[os.path.relpath(ws_toml, project_root)] = spec
    unique_specs = sorted(set(toml_specs.values()), key=_semver_key)
    latest = max(unique_specs, key=_semver_key) if unique_specs else None
    if emit_json:
        print(json.dumps({
            "lock_versions": [],
            "latest_version": latest,
            "multiple_lock_versions": False,
            "note": "No Cargo.lock found; versions are declared specs, not resolved.",
            "version_summary": {},
            "direct_dependents_by_sdk_version": {},
            "workspace_crates": [],
            "cargo_toml_specs": toml_specs,
        }, indent=2))
        return

    if not toml_specs:
        print("No soroban-sdk dependency found in any Cargo.toml.")
        return

    specs_by_version: dict[str, list[str]] = {}
    for path, spec in toml_specs.items():
        specs_by_version.setdefault(spec, []).append(path)

    header("soroban-sdk specs declared in Cargo.toml (no Cargo.lock found)")
    color = RED if len(specs_by_version) > 1 else GREEN
    for spec, files in sorted(specs_by_version.items()):
        print(f"  {color}• {spec}{RESET}  ({len(files)} file(s))")
        for f in files:
            print(f"    {f}")
    if len(specs_by_version) > 1:
        print(f"\n  {YELLOW}⚠  Multiple version specs — run 'cargo generate-lockfile' to resolve.{RESET}")
    print()


def find_locks(root: Path):
    """Return every Cargo.lock relevant to `root`:
      - the one governing `root` itself (in `root` or an ancestor — a member
        crate uses its workspace root's lock), and
      - every Cargo.lock below `root` (nested / sibling workspaces),
    skipping build output and VCS dirs.  Shallowest first."""
    governing = None
    for d in [root, *root.parents]:
        cand = d / "Cargo.lock"
        if cand.is_file():
            governing = cand
            break
    below = [p for p in root.rglob("Cargo.lock")
             if not any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1])
             and p != governing]
    below.sort(key=lambda p: (len(p.parts), str(p)))
    return ([governing] if governing else []) + below


def analyze_lock(lock_path: Path):
    packages      = parse_lock(lock_path.read_text())
    lock_versions = sdk_versions_in_lock(packages)
    graph         = build_dep_graph(packages)
    rev           = reverse_graph(graph)
    members       = find_workspace_members(packages)
    toml_specs    = read_cargo_toml_sdk_specs(lock_path.parent)
    data = build_json(packages, lock_versions, graph, rev, members, toml_specs)
    return packages, data


def _rel(path: Path, base: Path) -> str:
    return os.path.relpath(path, base)


def merged_summary(per_lock: dict, toml_specs: dict) -> dict:
    """{version: {sources, lock_files, toml_files}} across all lock files
    and every Cargo.toml under the scan root."""
    summary = {}
    for lock_rel, data in per_lock.items():
        for v in data["lock_versions"]:
            e = summary.setdefault(v, {"sources": [], "lock_files": [], "toml_files": []})
            if "lock" not in e["sources"]:
                e["sources"].append("lock")
            e["lock_files"].append(lock_rel)
    for path, spec in toml_specs.items():
        e = summary.setdefault(spec, {"sources": [], "lock_files": [], "toml_files": []})
        if "toml" not in e["sources"]:
            e["sources"].append("toml")
        e["toml_files"].append(path)
    return dict(sorted(summary.items(), key=lambda kv: _semver_key(kv[0])))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    args      = sys.argv[1:]
    emit_json = "--json" in args
    paths     = [a for a in args if not a.startswith("--")]

    root = Path(paths[0]) if paths else Path(".")
    if not root.exists():
        sys.exit(f"error: {root} does not exist")
    root = root.resolve()
    # Accept a Cargo.toml (or any non-lock file) by using its directory.
    if root.is_file() and root.name != "Cargo.lock":
        root = root.parent

    if root.is_file():                      # explicit Cargo.lock
        locks = [root]
        root = root.parent
    else:
        locks = find_locks(root)
    if not locks:
        # No Cargo.lock anywhere — fall back to Cargo.toml specs.
        _report_toml_only(root, emit_json)
        return

    # Scan root for Cargo.toml files: `root`, or the workspace root above it
    # if the governing lock lives in an ancestor directory.
    scan_root = root
    for l in locks:
        if l.parent in root.parents:
            scan_root = l.parent
    if len(locks) > 1 or scan_root != root:
        print("Note: analysing " + ", ".join(_rel(l, root) for l in locks), file=sys.stderr)

    per_lock, pkgs = {}, {}
    for l in locks:
        key = _rel(l, scan_root)
        pkgs[key], per_lock[key] = analyze_lock(l)

    toml_specs = read_cargo_toml_sdk_specs(scan_root)
    if _crate_inherits_workspace_sdk(root) and scan_root == root:
        spec, ws_toml = workspace_sdk_spec(root.parent)
        if spec:
            toml_specs[_rel(ws_toml, scan_root)] = spec

    all_lock_versions = sorted({v for d in per_lock.values() for v in d["lock_versions"]},
                               key=_semver_key)
    summary = merged_summary(per_lock, toml_specs)
    latest  = max(all_lock_versions, key=_semver_key) if all_lock_versions else None

    if emit_json:
        if len(per_lock) == 1:
            out = dict(next(iter(per_lock.values())))
            out["lock_file"] = next(iter(per_lock))
        else:
            out = {"lock_versions": all_lock_versions,
                   "multiple_lock_versions": len(all_lock_versions) > 1,
                   "lock_files": per_lock}
        out["lock_versions"] = all_lock_versions
        out["latest_version"] = latest
        out["multiple_lock_versions"] = len(all_lock_versions) > 1
        out["version_summary"] = {
            v: {"sources": i["sources"],
                "lock_files": i["lock_files"] or None,
                "toml_files": i["toml_files"] or None}
            for v, i in summary.items()}
        out["cargo_toml_specs"] = toml_specs
        print(json.dumps(out, indent=2))
        return

    if len(per_lock) > 1:
        header(f"Cargo.lock files found ({len(per_lock)})")
        for key, data in per_lock.items():
            vers = ", ".join(data["lock_versions"]) or "(no soroban-sdk)"
            print(f"  {key}  →  {vers}")
        if len(all_lock_versions) > 1:
            print(f"\n  {YELLOW}⚠  Different soroban-sdk versions across lock files: "
                  f"{', '.join(all_lock_versions)}{RESET}")
    print_version_summary(summary)
    for key, data in per_lock.items():
        print_text(data, pkgs[key], lock_label=key, summary=False)
    print_toml_specs(toml_specs)
    print()


if __name__ == "__main__":
    main()
