#!/usr/bin/env python3
"""Dev-only generator for the solidity_ast test fixtures. NEVER run in CI.

Default mode
------------
For each (solc version, contract file) pair in ``PAIRS``, run ``certoraRun``
with ``--dump_asts --compilation_steps_only`` (fully local: compiles and dumps
ASTs, sends nothing anywhere) inside a temporary working directory, pick up the
``.asts.json`` written to the newest ``.certora_internal/<run>/`` build dir,
sanitize it (see below), and write ``solc_<ver>.asts.json`` next to this script.

Requires ``certoraRun`` (e.g. ``uv run --with certora-cli python
tests/fixtures/solidity_ast/generate_fixtures.py``) and the solc binaries for
the versions in ``PAIRS`` (solc-select artifacts, ``solc<ver>``/``solc-<ver>``
on PATH, or a directory of ``solc-<ver>`` binaries via ``--solc-dir``).

Sanitization
------------
The raw dump is a three-level mapping::

    {cli_path: {absolute_path: {node_id: node}}}

Level-1 keys are the contract paths as passed on the certoraRun command line,
level-2 keys are machine-specific absolute paths, and SourceUnit nodes carry
``absolutePath`` string fields holding the same absolute paths. All three are
rewritten to paths relative to the ``contracts/`` directory (e.g.
``"breadth_08.sol"``) so the fixtures are machine-independent. Everything else
— values, key order, node contents — is kept exactly as dumped.

--golden mode
-------------
Reads ``solc_0_8_30.asts.json`` and writes
``expected_parent_graph_0_8_30.json`` using the FROZEN legacy parent-graph
algorithm, copied verbatim from
``certora_autosetup/setup/setup_prover.py`` (``generate_ast_graph`` +
``_extract_child_node_ids``). The copy is intentional: it pins the legacy
semantics as a golden reference even if setup_prover.py later migrates to the
typed AST models. Do not "fix" or modernize it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent
CONTRACTS_DIR = FIXTURES_DIR / "contracts"

# (solc version, contract file, main contract) — breadth_06.sol is shared by
# 0.6 and 0.7 (pragma >=0.6.12 <0.8.0).
PAIRS: list[tuple[str, str, str]] = [
    ("0.6.12", "breadth_06.sol", "Diamond"),
    ("0.7.6", "breadth_06.sol", "Diamond"),
    ("0.8.30", "breadth_08.sol", "Diamond"),
]

DUMMY_SPEC = "rule dummy { assert true; }\n"


def find_solc(version: str, solc_dir: Path | None) -> Path:
    """Locate a solc binary for ``version`` (see module docstring)."""
    candidates: list[Path] = []
    if solc_dir is not None:
        candidates += [solc_dir / f"solc-{version}", solc_dir / f"solc{version}"]
    for name in (f"solc{version}", f"solc-{version}"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates.append(
        Path.home() / ".solc-select" / "artifacts" / f"solc-{version}" / f"solc-{version}"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No solc {version} binary found (tried {[str(c) for c in candidates]}); "
        f"download it from https://binaries.soliditylang.org/ and pass --solc-dir"
    )


def _rel_to_contracts(path_str: str) -> str:
    """Rewrite a dump path to be relative to the contracts/ directory.

    Both the level-1 keys (CLI-relative, e.g. ``contracts/breadth_06.sol``) and
    the level-2 keys / ``absolutePath`` values (absolute paths into the temp
    working dir) contain a ``contracts/`` component; everything after its last
    occurrence is the machine-independent path.
    """
    parts = PurePosixPath(path_str).parts
    if "contracts" in parts:
        idx = len(parts) - 1 - tuple(reversed(parts)).index("contracts")
        return str(PurePosixPath(*parts[idx + 1 :]))
    return path_str


def _rewrite_absolute_paths(obj: Any) -> None:
    """Recursively rewrite every string-valued ``absolutePath`` field in-place."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "absolutePath" and isinstance(value, str):
                obj[key] = _rel_to_contracts(value)
            else:
                _rewrite_absolute_paths(value)
    elif isinstance(obj, list):
        for item in obj:
            _rewrite_absolute_paths(item)


def sanitize(asts_data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a raw .asts.json dump (see module docstring). Key order is kept."""
    sanitized: dict[str, Any] = {}
    for level1_key, level1_value in asts_data.items():
        new_level1: dict[str, Any] = {}
        for level2_key, nodes in level1_value.items():
            _rewrite_absolute_paths(nodes)
            new_level1[_rel_to_contracts(level2_key)] = nodes
        sanitized[_rel_to_contracts(level1_key)] = new_level1
    return sanitized


def run_certora_dump(
    certora_run: str, contract_file: str, main_contract: str, solc_path: Path
) -> dict[str, Any]:
    """Run certoraRun (compile-only) in a temp dir and return the raw dump."""
    with tempfile.TemporaryDirectory(prefix="solidity_ast_fixtures_") as tmp:
        workdir = Path(tmp)
        shutil.copytree(CONTRACTS_DIR, workdir / "contracts")
        spec = workdir / "dummy.spec"
        spec.write_text(DUMMY_SPEC)
        cmd = [
            certora_run,
            f"contracts/{contract_file}:{main_contract}",
            "--verify",
            f"{main_contract}:dummy.spec",
            "--compilation_steps_only",
            "--dump_asts",
            "--solc",
            str(solc_path),
        ]
        print(f"+ {' '.join(cmd)}  (cwd={workdir})")
        subprocess.run(cmd, cwd=workdir, check=True)
        dumps = sorted(
            workdir.glob(".certora_internal/*/.asts.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if not dumps:
            raise RuntimeError(f"certoraRun produced no .asts.json under {workdir}")
        with open(dumps[-1]) as f:
            return json.load(f)


def generate_fixtures(certora_run: str, solc_dir: Path | None) -> None:
    for version, contract_file, main_contract in PAIRS:
        solc_path = find_solc(version, solc_dir)
        raw = run_certora_dump(certora_run, contract_file, main_contract, solc_path)
        sanitized = sanitize(raw)
        out_path = FIXTURES_DIR / f"solc_{version.replace('.', '_')}.asts.json"
        with open(out_path, "w") as f:
            json.dump(sanitized, f, indent=2)
            f.write("\n")
        node_count = sum(len(nodes) for lvl1 in sanitized.values() for nodes in lvl1.values())
        print(f"wrote {out_path} ({node_count} nodes)")


# ---------------------------------------------------------------------------
# --golden: FROZEN legacy parent-graph algorithm, copied verbatim from
# certora_autosetup/setup/setup_prover.py (generate_ast_graph /
# _extract_child_node_ids). Iteration and key-order semantics must not change.
# ---------------------------------------------------------------------------


def _extract_child_node_ids(node: Any) -> list[int]:
    child_ids = []

    if isinstance(node, dict):
        for key, value in node.items():
            # Look for 'id' fields in nested structures
            if isinstance(value, dict) and 'id' in value:
                child_ids.append(value['id'])
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and 'id' in item:
                        child_ids.append(item['id'])

    return child_ids


def build_legacy_parent_graph(asts_data: dict[str, Any]) -> dict[str, Any]:
    # Build parent graph: node_id -> parent_node_id
    parent_graph = {}

    # Structure: dict[relative_path: dict[absolute_path: dict[node_id: node_data]]]
    for relative_path, path_data in asts_data.items():
        parent_graph[relative_path] = {}

        for absolute_path, nodes in path_data.items():
            parent_graph[relative_path][absolute_path] = {}

            # For each node, find all child node IDs and map them to this parent
            for node_id, node in nodes.items():
                if not isinstance(node, dict):
                    continue

                # Find all child node IDs referenced in this node
                child_ids = _extract_child_node_ids(node)
                for child_id in child_ids:
                    parent_graph[relative_path][absolute_path][str(child_id)] = str(node_id)

    return parent_graph


def generate_golden() -> None:
    fixture_path = FIXTURES_DIR / "solc_0_8_30.asts.json"
    with open(fixture_path) as f:
        asts_data = json.load(f)
    parent_graph = build_legacy_parent_graph(asts_data)
    out_path = FIXTURES_DIR / "expected_parent_graph_0_8_30.json"
    with open(out_path, "w") as f:
        json.dump(parent_graph, f, indent=2)
        f.write("\n")
    edge_count = sum(
        len(edges) for lvl1 in parent_graph.values() for edges in lvl1.values()
    )
    print(f"wrote {out_path} ({edge_count} parent edges)")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument(
        "--golden",
        action="store_true",
        help="derive expected_parent_graph_0_8_30.json from solc_0_8_30.asts.json "
        "instead of regenerating the .asts.json fixtures",
    )
    parser.add_argument(
        "--certora-run",
        default="certoraRun",
        help="certoraRun executable (default: from PATH)",
    )
    parser.add_argument(
        "--solc-dir",
        type=Path,
        default=None,
        help="directory containing solc-<version> binaries (checked before PATH "
        "and ~/.solc-select/artifacts)",
    )
    args = parser.parse_args()

    if args.golden:
        generate_golden()
    else:
        generate_fixtures(args.certora_run, args.solc_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
