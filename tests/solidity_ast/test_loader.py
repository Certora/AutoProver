"""Loader behavior: degradation policy, Vyper passthrough, unknown-node fallback."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from certora_autosetup.solidity_ast import AstDump, SourceUnit, UnknownNode, walk

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solidity_ast"

MINIMAL_SOURCE_UNIT = {
    "id": 1,
    "src": "0:10:0",
    "absolutePath": "a.sol",
    "exportedSymbols": {},
    "nodes": [],
    "nodeType": "SourceUnit",
}


def _dump_of(flat: dict) -> dict:
    return {"a.sol": {"/abs/a.sol": flat}}


def test_minimal_source_unit_parses() -> None:
    dump = AstDump.from_dict(_dump_of({"1": MINIMAL_SOURCE_UNIT}), on_error="raise")
    [(_, source)] = list(dump.iter_sources())
    assert isinstance(source.root, SourceUnit)
    assert source.nodes.keys() == {1}
    assert dump.find_node("/abs/a.sol", 1) is source.root


def test_vyper_source_is_raw_passthrough() -> None:
    dump = AstDump.load(FIXTURES / "vyper_mixed.asts.json", on_error="raise")
    kinds = {source.source_path: source.raw_kind for _, source in dump.iter_sources()}
    assert kinds == {"counter.sol": "solidity", "counter.vy": "vyper"}
    vyper = next(s for _, s in dump.iter_sources() if s.raw_kind == "vyper")
    assert vyper.root is None and vyper.nodes == {} and vyper.raw
    # typed iteration skips it
    assert all(path == "counter.sol" for _, path, _ in dump.iter_parsed_roots())


def test_unknown_node_type_degrades_per_node() -> None:
    root = dict(MINIMAL_SOURCE_UNIT)
    root["nodes"] = [
        {"id": 2, "src": "0:5:0", "nodeType": "FrobnicationDefinition", "frob": True}
    ]
    dump = AstDump.from_dict(_dump_of({"1": root}), on_error="raise")
    [(_, source)] = list(dump.iter_sources())
    assert source.root is not None
    [unknown] = [n for n in walk(source.root) if isinstance(n, UnknownNode)]
    assert unknown.nodeType == "FrobnicationDefinition"
    assert unknown.model_extra == {"frob": True}


def test_shape_mismatch_degrades_per_source() -> None:
    broken = dict(MINIMAL_SOURCE_UNIT)
    broken["exportedSymbols"] = "not-a-dict"
    data = _dump_of({"1": broken})

    dump = AstDump.from_dict(data, on_error="raw")
    [(_, source)] = list(dump.iter_sources())
    assert source.raw_kind == "parse_failed"
    assert source.root is None and source.raw and source.parse_error

    with pytest.raises(ValidationError):
        AstDump.from_dict(data, on_error="raise")


def test_stream_units_equivalent_to_load() -> None:
    path = FIXTURES / "solc_0_8_30.asts.json"
    loaded = AstDump.load(path, on_error="raise", solc_version="0.8.30")
    streamed = list(AstDump.stream_units(path, on_error="raise", solc_version="0.8.30"))
    assert [u.original_file for u in streamed] == list(loaded.files)
    for unit in streamed:
        expected = loaded.files[unit.original_file]
        assert unit.sources.keys() == expected.sources.keys()
        for source_path, source in unit.sources.items():
            other = expected.sources[source_path]
            assert source.raw_kind == other.raw_kind
            assert source.nodes.keys() == other.nodes.keys()
            assert source.root == other.root


def test_stream_units_unit_filter_skips_before_validation() -> None:
    path = FIXTURES / "solc_0_8_30.asts.json"
    assert list(AstDump.stream_units(path, unit_filter=lambda rel: False)) == []
    kept = list(AstDump.stream_units(path, unit_filter=lambda rel: rel.endswith(".sol")))
    assert kept


def test_missing_source_unit_degrades() -> None:
    data = _dump_of({"7": {"id": 7, "src": "0:1:0", "nodeType": "PragmaDirective", "literals": []}})
    dump = AstDump.from_dict(data, on_error="raw")
    [(_, source)] = list(dump.iter_sources())
    assert source.raw_kind == "parse_failed"
    with pytest.raises(ValueError):
        AstDump.from_dict(data, on_error="raise")
