"""Regression tests for scene-wide curated library summary matching in
``SummarySetup.match_summaries_from_all_methods``.

A ``library_names`` entry must match even when the library method's
``originatingContract`` is a foreign compilation unit (a library is deduped to a single
originating unit in all_methods.json). Non-library inherited bases stay originating-scoped.
"""

import json
from pathlib import Path

from certora_autosetup.parsers.method_parser import MethodParser
from certora_autosetup.setup import setup_summaries
from certora_autosetup.setup.setup_summaries import SummarySetup

# Controlled registry: one real library and one inherited abstract base.
REGISTRY = {
    "lib_entry": {
        "names": ["libFn"],
        "library_names": ["SomeLib"],
        "summary_file": "specs/summaries/SomeLib.spec",
        "description": "SomeLib.libFn",
    },
    "base_entry": {
        "names": ["baseFn"],
        "library_names": ["SomeBase"],
        "summary_file": "specs/summaries/SomeBase.spec",
        "description": "SomeBase.baseFn",
    },
}


def _method(name, contract, originating, *, library):
    """One all_methods.json entry with the fields the matcher reads."""
    return {
        "name": name,
        "contractName": contract,
        "definingContract": contract,
        "originatingContract": originating,
        "isLibrary": library,
        "fullSignature": [],
        "visibility": "internal",
        "stateMutability": "nonpayable",
    }


def _match(tmp_path, monkeypatch, methods, main_contract):
    all_methods = tmp_path / "all_methods.json"
    all_methods.write_text(json.dumps(methods))
    # The matcher guards on PATH_ALL_METHODS_JSON.exists() before reading via the parser.
    monkeypatch.setattr(setup_summaries, "PATH_ALL_METHODS_JSON", all_methods)
    obj = SummarySetup.__new__(SummarySetup)  # bypass __init__
    obj.methods_parser = MethodParser(str(all_methods))
    obj.function_summaries = REGISTRY
    obj.log = lambda *a, **k: None
    matched, _ = obj.match_summaries_from_all_methods(main_contract)
    return matched


def test_library_matches_despite_foreign_originating_contract(tmp_path, monkeypatch):
    # The library method is attributed to a foreign unit yet must still match for Main.
    methods = [
        _method("libFn", "SomeLib", "OtherUnit", library=True),
        _method("f", "Main", "Main", library=False),
    ]
    assert "lib_entry" in _match(tmp_path, monkeypatch, methods, "Main")


def test_non_library_base_not_matched_scene_wide(tmp_path, monkeypatch):
    # A non-library base originating from a foreign unit must not be matched scene-wide.
    methods = [
        _method("baseFn", "SomeBase", "OtherUnit", library=False),
        _method("f", "Main", "Main", library=False),
    ]
    assert "base_entry" not in _match(tmp_path, monkeypatch, methods, "Main")


def test_non_library_base_matches_when_originating_in_scope(tmp_path, monkeypatch):
    # Originating-scoped matching for non-library entries is preserved.
    methods = [_method("baseFn", "SomeBase", "Main", library=False)]
    assert "base_entry" in _match(tmp_path, monkeypatch, methods, "Main")


def test_library_matches_when_originating_in_scope(tmp_path, monkeypatch):
    # The union does not disturb the ordinary in-scope case.
    methods = [_method("libFn", "SomeLib", "Main", library=True)]
    assert "lib_entry" in _match(tmp_path, monkeypatch, methods, "Main")


def test_shipped_registry_getCollectionId_entry_is_library_scoped():
    # Guard the wiring: the curated library entry the fix relies on stays shaped.
    reg = json.loads((Path(setup_summaries.__file__).parent / "function_summaries.json").read_text())
    entry = reg["ctHelpers_getCollectionId"]
    assert entry["library_names"] == ["CTHelpers"]
    assert "getCollectionId" in entry["names"]
