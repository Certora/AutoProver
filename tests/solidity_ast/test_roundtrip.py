"""Round-trip fidelity: parsing a dump and re-serializing must reproduce the source
JSON exactly (modulo the reversed contract-name stamp inside internalFunctionIDs)."""

from pathlib import Path

import pytest

from certora_autosetup.solidity_ast import AstDump
from certora_autosetup.solidity_ast.diagnostics import roundtrip_diffs

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solidity_ast"


@pytest.mark.parametrize(
    "fixture",
    ["solc_0_4_26", "solc_0_5_17", "solc_0_6_12", "solc_0_7_6", "solc_0_8_30", "vyper_mixed"],
)
def test_roundtrip_is_loyal(fixture: str) -> None:
    dump = AstDump.load(FIXTURES / f"{fixture}.asts.json", on_error="raise")
    for _, source in dump.iter_sources():
        if source.root is None:
            continue
        diffs = roundtrip_diffs(source)
        assert not diffs, f"{fixture}/{source.source_path}:\n" + "\n".join(diffs[:20])
