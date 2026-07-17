#!/usr/bin/env python3
"""Dev-only generator for the amenability test fixture dump. NEVER run in CI.

Runs ``certoraRun signals_bait.sol --compilation_steps_only --dump_asts`` in a
temp dir and writes ``signals_bait.asts.json`` next to this script with all
machine-specific absolute paths rewritten to ``signals_bait.sol`` — the same
sanitization the solidity_ast fixtures use (see
tests/fixtures/solidity_ast/generate_fixtures.py).

Requires certoraRun and a solc for ^0.8.30 on PATH (e.g. solc8.30 / solc8.28+).
Usage: uv run python tests/fixtures/amenability/generate.py [--solc solc8.30]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
CONTRACT = "signals_bait.sol"


def sanitize(dump: dict) -> dict:
    def fix_path(p: str) -> str:
        return CONTRACT if p.endswith(CONTRACT) else p

    out = {}
    for cli_path, sources in dump.items():
        new_sources = {}
        for abs_path, flat in sources.items():
            for node in flat.values():
                if isinstance(node, dict) and "absolutePath" in node:
                    node["absolutePath"] = fix_path(node["absolutePath"])
            new_sources[fix_path(abs_path)] = flat
        out[fix_path(cli_path)] = new_sources
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--solc", default="solc8.30")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copy(FIXTURES_DIR / CONTRACT, work / CONTRACT)
        (work / "dummy.spec").write_text("rule trivial { assert true; }\n")
        subprocess.run(
            ["certoraRun", f"{CONTRACT}:PackedBook", "--verify", "PackedBook:dummy.spec",
             "--solc", args.solc, "--compilation_steps_only", "--dump_asts"],
            cwd=work, check=True,
        )
        dumps = sorted((work / ".certora_internal").rglob(".asts.json"))
        if not dumps:
            print("no .asts.json produced", file=sys.stderr)
            return 1
        dump = json.loads(dumps[-1].read_text())

    out = FIXTURES_DIR / "signals_bait.asts.json"
    out.write_text(json.dumps(sanitize(dump), indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
