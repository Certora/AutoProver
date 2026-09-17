#!/usr/bin/env python3
"""
classify_cargo_toml.py
 
Walks a Rust project (or monorepo) and classifies every Cargo.toml file found
into one of three categories:
 
  1) UNRELATED_STANDALONE
                - a Cargo.toml with no workspace relationship to any other
                  manifest: it has no [workspace] table with resolvable
                  members, and it isn't a member of some ancestor
                  workspace. This covers both the case where it's the only
                  Cargo.toml in the project, and the case where other,
                  unrelated Cargo.toml files happen to sit elsewhere in the
                  same repo.
  2) WORKSPACE_ROOT
                - a top-level Cargo.toml with a [workspace] table whose
                  `members` (globs) resolve to one or more other Cargo.toml
                  files beneath it. Those children can use `xxx.workspace =
                  true` to inherit settings from this file.
  3) MEMBER     - a Cargo.toml that is listed (directly, or via a glob) as a
                  member of some ancestor workspace root, and can therefore
                  inherit settings from that parent file.
 
A Cargo.toml that declares [workspace] but resolves zero members is
reported separately as WORKSPACE_ROOT_EMPTY so nothing is silently
misclassified.
 
Usage:
    python classify_cargo_toml.py /path/to/repo
 
Requires Python 3.11+ (uses the stdlib `tomllib`). Falls back to the
`tomli` package on older interpreters if it's installed.
"""
 
from __future__ import annotations
 
import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
 
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for <3.11
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        sys.exit(
            "This script needs Python 3.11+ (stdlib tomllib) or the "
            "'tomli' package installed (`pip install tomli`)."
        )
 
# Directories we never want to descend into when searching for manifests.
SKIP_DIRS = {".git", "target", "node_modules", ".cargo", ".idea", ".vscode"}
 
 
@dataclass
class Manifest:
    path: Path                      # absolute path to Cargo.toml
    dir: Path                       # its containing directory
    data: dict = field(default_factory=dict)
    has_workspace: bool = False
    has_package: bool = False
    member_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    resolved_members: set[Path] = field(default_factory=set)  # filled in later
    self_referential: bool = False  # True if members resolve back to its own dir
    parent_workspace: Path | None = None  # filled in later
 
 
def find_cargo_tomls(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os_walk(root):
        # prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "Cargo.toml" in filenames:
            found.append((dirpath / "Cargo.toml").resolve())
    return found
 
 
def os_walk(root: Path):
    """Thin wrapper so we can yield Path objects from os.walk."""
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        yield Path(dirpath), dirnames, filenames
 
 
def load_manifest(path: Path) -> Manifest:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    ws = data.get("workspace", {}) or {}
    m = Manifest(
        path=path,
        dir=path.parent,
        data=data,
        has_workspace="workspace" in data,
        has_package="package" in data,
        member_globs=list(ws.get("members", []) or []),
        exclude_globs=list(ws.get("exclude", []) or []),
    )
    return m
 
 
def resolve_globs(base_dir: Path, patterns: list[str]) -> set[Path]:
    """Resolve Cargo workspace `members`/`exclude` entries to absolute
    directories, the way Cargo does (paths are relative to the directory
    containing the workspace's Cargo.toml).
 
    Entries aren't always globs — Cargo also accepts plain literal paths
    (including "." to mean "this same directory", a common cargo-fuzz
    pattern). pathlib's glob() rejects "." and similar non-wildcard-only
    patterns, so literal paths are resolved directly instead of via glob().
    """
    resolved: set[Path] = set()
    for pattern in patterns:
        if any(ch in pattern for ch in "*?[]"):
            for match in base_dir.glob(pattern):
                if match.is_dir():
                    resolved.add(match.resolve())
        else:
            candidate = (base_dir / pattern).resolve()
            if candidate.is_dir():
                resolved.add(candidate)
    return resolved
 
 
def build_relationships(manifests: list[Manifest]) -> None:
    by_dir = {m.dir.resolve(): m for m in manifests}
 
    for ws in manifests:
        if not ws.has_workspace:
            continue
        member_dirs = resolve_globs(ws.dir, ws.member_globs)
        exclude_dirs = resolve_globs(ws.dir, ws.exclude_globs)
        member_dirs -= exclude_dirs
 
        own_dir = ws.dir.resolve()
        if own_dir in member_dirs:
            # e.g. members = ["."] — the common cargo-fuzz idiom for a
            # crate that declares its own trivial workspace specifically
            # to isolate itself from any real parent workspace. That's
            # not "another file beneath it", so don't count it as one.
            ws.self_referential = True
            member_dirs.discard(own_dir)
 
        for mdir in member_dirs:
            child = by_dir.get(mdir)
            if child is None:
                continue  # glob matched a dir with no Cargo.toml
            ws.resolved_members.add(child.path)
            # A file might in theory be globbed by more than one workspace;
            # keep the first (closest ancestor wins if we later sort by depth).
            if child.parent_workspace is None:
                child.parent_workspace = ws.path
 
    # Prefer the *closest* ancestor workspace in case of ambiguity/nesting.
    # (Re-pass, choosing the parent with the deepest dir path among those
    # that actually claim this manifest as a member.)
    claims: dict[Path, list[Path]] = {}
    for ws in manifests:
        if not ws.has_workspace:
            continue
        for member_path in ws.resolved_members:
            claims.setdefault(member_path, []).append(ws.path)
 
    for m in manifests:
        candidates = claims.get(m.path)
        if candidates:
            candidates.sort(key=lambda p: len(p.parts), reverse=True)
            m.parent_workspace = candidates[0]
 
 
def classify(manifests: list[Manifest]) -> list[dict]:
    build_relationships(manifests)
    results = []
 
    for m in manifests:
        if m.has_workspace and len(m.resolved_members) > 0:
            category = "WORKSPACE_ROOT"
            child_list = ", ".join(
                str(p.parent.relative_to(m.dir)) if p.is_relative_to(m.dir) else str(p)
                for p in sorted(m.resolved_members)
            )
            detail = f"Top-level workspace with {len(m.resolved_members)} member(s): {child_list}"
        elif m.parent_workspace is not None:
            category = "MEMBER"
            detail = f"Inherits from workspace root at {m.parent_workspace}"
        elif m.has_workspace and not m.resolved_members and m.self_referential:
            category = "UNRELATED_STANDALONE"
            detail = (
                "Declares a self-referential [workspace] (members = [\".\"] or "
                "equivalent) purely to opt itself out of any parent workspace; "
                "it has no other members and inherits from nothing."
            )
        elif m.has_workspace and not m.resolved_members:
            # Declares [workspace] but no members resolved (empty/virtual
            # workspace with nothing beneath it yet).
            category = "WORKSPACE_ROOT_EMPTY"
            detail = "Declares [workspace] but no member manifests were resolved beneath it."
        else:
            category = "UNRELATED_STANDALONE"
            detail = (
                "Independent package manifest with no workspace relationship "
                "to any parent or child Cargo.toml (whether or not other, "
                "unrelated Cargo.toml files exist elsewhere in the repo)."
            )
 
        results.append(
            {
                "path": str(m.path),
                "category": category,
                "detail": detail,
            }
        )
    return results
 
 
def process(root):
    paths = find_cargo_tomls(root)
    if not paths:
        sys.exit(f"No Cargo.toml files found under {root}")
 
    manifests = []
    for p in paths:
        try:
            manifests.append(load_manifest(p))
        except Exception as e:  # malformed TOML, permissions, etc.
            print(f"warning: failed to parse {p}: {e}", file=sys.stderr)
 
    results = classify(manifests)
 
    output = {}
    for r in results:
        p = Path(r["path"])
        try:
            key = str(p.relative_to(root))
        except ValueError:
            key = r["path"]  # leave as absolute if it's outside root for some reason
        r.pop("path")
        output[key] = r
 
    return dict(sorted(output.items()))

 
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path, help="Path to the Rust project/repo root")
    args = parser.parse_args()
 
    root = args.repo_root.resolve()
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    return process(root)
    
 
if __name__ == "__main__":
    print(json.dumps(main(), indent=2)) 
