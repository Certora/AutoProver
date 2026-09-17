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

def read_cargo_toml_sdk_specs(project_root: Path) -> dict:
    results = {}
    pattern = re.compile(
        r'soroban-sdk\s*=\s*(?:"([^"]+)"|[^#\n]*?version\s*=\s*"([^"]+)")',
        re.MULTILINE,
    )
    for toml in sorted(project_root.rglob("Cargo.toml")):
        try:
            text = toml.read_text(errors="replace")
        except OSError:
            continue
        m = pattern.search(text)
        if m:
            spec = m.group(1) or m.group(2)
            results[str(toml.relative_to(project_root))] = spec
    return results


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

    def semver_key(v):
        return tuple(int(p) for p in v.split(".") if p.isdigit())

    latest = max(lock_versions, key=semver_key) if lock_versions else None

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


def print_text(data, packages):
    lock_versions = data["lock_versions"]
    toml_specs    = data["cargo_toml_specs"]
    sdk_ver_set   = set(lock_versions)

    # 1. Versions in lock
    header("soroban-sdk versions present in Cargo.lock")
    color = RED if data["multiple_lock_versions"] else GREEN
    for v in lock_versions:
        print(f"  {color}• {v}{RESET}")
    if data["multiple_lock_versions"]:
        print(f"\n  {YELLOW}⚠  Multiple versions detected — may cause ABI mismatches.{RESET}")

    # 2. Version summary (all mentioned)
    header("All soroban-sdk versions mentioned (lock + Cargo.toml)")
    vs = data["version_summary"]
    for ver, info in sorted(vs.items()):
        in_lock = "lock" in info["sources"]
        in_toml = "toml" in info["sources"]
        tags = []
        if in_lock:
            tags.append(f"{GREEN}resolved in lock{RESET}")
        if in_toml:
            tags.append(f"{CYAN}declared in toml{RESET}")
        tag_str = ", ".join(tags)
        print(f"  {BOLD}{ver}{RESET}  [{tag_str}]")
        if info["toml_files"]:
            for f in info["toml_files"]:
                print(f"    {f}")

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
    if toml_specs:
        header("soroban-sdk version specs declared in Cargo.toml files")
        for path, spec in toml_specs.items():
            print(f"  {path}")
            print(f"    version = {CYAN}\"{spec}\"{RESET}")

    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    args      = sys.argv[1:]
    emit_json = "--json" in args
    paths     = [a for a in args if not a.startswith("--")]

    root      = Path(paths[0]) if paths else Path(".")
    lock_path = (root / "Cargo.lock") if root.is_dir() else root
    if not lock_path.exists():
        sys.exit(f"Cargo.lock not found at {lock_path}")

    project_root  = lock_path.parent
    packages      = parse_lock(lock_path.read_text())
    lock_versions = sdk_versions_in_lock(packages)

    if not lock_versions:
        out = {"lock_versions": [], "multiple_lock_versions": False,
               "version_summary": {}, "direct_dependents_by_sdk_version": {},
               "workspace_crates": [], "cargo_toml_specs": {}}
        if emit_json:
            print(json.dumps(out, indent=2))
        else:
            print("No soroban-sdk dependency found in Cargo.lock.")
        return

    graph      = build_dep_graph(packages)
    rev        = reverse_graph(graph)
    members    = find_workspace_members(packages)
    toml_specs = read_cargo_toml_sdk_specs(project_root)

    data = build_json(packages, lock_versions, graph, rev, members, toml_specs)

    if emit_json:
        print(json.dumps(data, indent=2))
    else:
        print_text(data, packages)


if __name__ == "__main__":
    main()
