"""Byte-compatibility of the legacy parent-graph JSON.

`.certora_internal/all_ast_parent_graph.json` is read by other code, so
`build_parent_graph_json` must reproduce the historical output byte-for-byte.
The golden file was produced by the frozen copy of the original algorithm in
tests/fixtures/solidity_ast/generate_fixtures.py --golden.
"""

import json
from pathlib import Path

from certora_autosetup.solidity_ast import build_parent_graph_json

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solidity_ast"


def test_parent_graph_byte_identical_to_golden() -> None:
    with open(FIXTURES / "solc_0_8_30.asts.json") as f:
        raw = json.load(f)
    produced = json.dumps(build_parent_graph_json(raw), indent=2) + "\n"
    golden = (FIXTURES / "expected_parent_graph_0_8_30.json").read_text()
    assert produced == golden
