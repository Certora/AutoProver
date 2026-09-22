"""Regression tests for compilation-unit curated summary matching in
``SummarySetup.match_summaries_from_all_methods``.

A curated ``library_names`` entry must match for a contract whose compilation unit contains the
library method — even a shared library inlined into several units — and must NOT match for a
contract whose unit does not contain it. Membership comes from the method's
``originatingContracts`` (all units that inline it), not a single attribution.
"""

import json
from pathlib import Path

from certora_autosetup.parsers.method_parser import MethodParser
from certora_autosetup.setup import setup_summaries
from certora_autosetup.setup.setup_summaries import SummarySetup

REGISTRY = {
    "lib_entry": {
        "names": ["libFn"],
        "library_names": ["SomeLib"],
        "summary_file": "specs/summaries/SomeLib.spec",
        "description": "SomeLib.libFn",
    },
}


def _method(name, contract, units):
    return {
        "name": name,
        "contractName": contract,
        "definingContract": contract,
        "originatingContracts": units,
        "isLibrary": True,
        "fullSignature": [],
        "visibility": "internal",
        "stateMutability": "pure",
    }


def _match(tmp_path, monkeypatch, methods, main_contract):
    all_methods = tmp_path / "all_methods.json"
    all_methods.write_text(json.dumps(methods))
    monkeypatch.setattr(setup_summaries, "PATH_ALL_METHODS_JSON", all_methods)
    obj = SummarySetup.__new__(SummarySetup)  # bypass __init__
    obj.methods_parser = MethodParser(str(all_methods))
    obj.function_summaries = REGISTRY
    obj.log = lambda *a, **k: None
    matched, _ = obj.match_summaries_from_all_methods(main_contract)
    return matched


def test_library_matches_when_unit_includes_it(tmp_path, monkeypatch):
    # Shared library inlined into several units; Main is among them -> matches for Main.
    methods = [_method("libFn", "SomeLib", ["OtherUnit", "Main"])]
    assert "lib_entry" in _match(tmp_path, monkeypatch, methods, "Main")


def test_not_matched_when_unit_excludes_it(tmp_path, monkeypatch):
    # Library present in the scene but not in Main's unit -> not matched (no over-match).
    methods = [
        _method("libFn", "SomeLib", ["OtherUnit"]),
        _method("f", "Main", ["Main"]),
    ]
    assert "lib_entry" not in _match(tmp_path, monkeypatch, methods, "Main")


def test_library_scoped_per_unit_across_sibling_contracts(tmp_path, monkeypatch):
    # A library in one contract's unit must NOT be matched for a sibling whose unit lacks it.
    methods = [
        _method("libFn", "SomeLib", ["A"]),  # library only in A's unit
        _method("g", "B", ["B"]),            # B's own method, no library
    ]
    assert "lib_entry" in _match(tmp_path, monkeypatch, methods, "A")
    assert "lib_entry" not in _match(tmp_path, monkeypatch, methods, "B")


def test_shipped_registry_getCollectionId_entry_is_library_scoped():
    reg = json.loads((Path(setup_summaries.__file__).parent / "function_summaries.json").read_text())
    entry = reg["ctHelpers_getCollectionId"]
    assert entry["library_names"] == ["CTHelpers"]
    assert "getCollectionId" in entry["names"]


def test_shipped_registry_safe_transfer_covers_solady_safetransferlib():
    """Solady's SafeTransferLib.safeTransfer/safeTransferFrom carry the same signatures and
    the same revert-on-failure contract as OpenZeppelin's SafeERC20, and the CVL in
    OZ_SafeERC20.spec already matches them through a wildcard receiver. Only the registry's
    library scoping kept the file from being imported for Solady scenes."""
    reg = json.loads((Path(setup_summaries.__file__).parent / "function_summaries.json").read_text())
    for key in ("safeTransfer", "safeTransferFrom"):
        entry = reg[key]
        assert set(entry["library_names"]) == {"SafeERC20", "SafeTransferLib"}, key
        # Both libraries must resolve to the one shared spec: two copies of the same
        # wildcard summary imported into one scene would be a duplicate declaration.
        assert entry["summary_file"] == "specs/summaries/OpenZeppelin/OZ_SafeERC20.spec", key


def test_shipped_registry_canCallWithDelay_entry_is_library_scoped():
    reg = json.loads((Path(setup_summaries.__file__).parent / "function_summaries.json").read_text())
    entry = reg["canCallWithDelay"]
    assert entry["library_names"] == ["AuthorityUtils"]
    assert entry["names"] == ["canCallWithDelay"]


def test_every_registry_summary_file_exists():
    """A registry entry naming a spec that is not shipped matches methods and then imports
    nothing, so the summary silently never applies."""
    setup_dir = Path(setup_summaries.__file__).parent
    reg = json.loads((setup_dir / "function_summaries.json").read_text())
    certora_root = setup_dir.parent / "certora"
    missing = sorted({e["summary_file"] for e in reg.values()
                      if not (certora_root / e["summary_file"]).exists()})
    assert not missing, f"registry entries point at absent spec files: {missing}"


def test_solady_safetransferlib_matches_for_its_own_unit(tmp_path, monkeypatch):
    """End-to-end through the matcher, with the shipped registry rather than the stub."""
    setup_dir = Path(setup_summaries.__file__).parent
    reg = json.loads((setup_dir / "function_summaries.json").read_text())
    all_methods = tmp_path / "all_methods.json"
    methods = [_method("safeTransfer", "SafeTransferLib", ["Main"])]
    all_methods.write_text(json.dumps(methods))
    monkeypatch.setattr(setup_summaries, "PATH_ALL_METHODS_JSON", all_methods)
    obj = SummarySetup.__new__(SummarySetup)
    obj.methods_parser = MethodParser(str(all_methods))
    obj.function_summaries = reg
    obj.log = lambda *a, **k: None
    matched, _ = obj.match_summaries_from_all_methods("Main")
    assert "safeTransfer" in matched
