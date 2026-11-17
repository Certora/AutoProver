"""Typed rustdoc JSON: what loads, and what is refused rather than read partly."""

import copy
import json

import pytest

from composer.cargo.rustdoc import (
    FORMAT_VERSION,
    FunctionInner,
    MacroInner,
    ModuleInner,
    OtherInner,
    ProcMacroInner,
    UnreadableRustdoc,
    parse_crate,
)


def _item(id: int, name: str | None, inner: dict) -> dict:
    return {
        "id": id, "crate_id": 0, "name": name, "visibility": "public", "docs": None,
        "attrs": ["macro_export", {"other": '#[cfg(feature = "std")]'}], "deprecation": None,
        "inner": inner, "span": None, "links": {},
    }


_CRATE = {
    "format_version": FORMAT_VERSION,
    "root": 0,
    "index": {
        "0": _item(0, "demo", {"module": {"items": [1, 2, 3, 4], "is_crate": True, "is_stripped": False}}),
        "1": _item(1, "f", {"function": {
            "sig": {"inputs": [["x", {"primitive": "u64"}]], "output": None, "is_c_variadic": False},
            "generics": {"params": [], "where_predicates": []},
            "header": {"is_const": False, "is_unsafe": False, "is_async": False, "abi": "Rust"},
            "has_body": True,
        }}),
        "2": _item(2, "m", {"macro": "macro_rules! m {\n    () => { ... };\n}"}),
        "3": _item(3, "Derive", {"proc_macro": {"kind": "derive", "helpers": []}}),
        "4": _item(4, "T", {"assoc_type": {"generics": {"params": []}, "bounds": [], "type": None}}),
    },
    "paths": {"0": {"crate_id": 0, "path": ["demo"], "kind": "module"}},
    "external_crates": {"1": {"name": "core", "html_root_url": None}},
    "crate_version": "0.1.0",
}


def _text(payload: dict) -> str:
    return json.dumps(payload)


def test_each_kind_the_walk_reads_is_typed():
    crate = parse_crate(_text(_CRATE))
    kinds = {i: type(item.inner) for i, item in crate.index.items()}
    assert kinds == {
        0: ModuleInner, 1: FunctionInner, 2: MacroInner, 3: ProcMacroInner, 4: OtherInner,
    }
    assert crate.external_crates[1].name == "core"
    assert crate.index[1].attrs[1] == {"other": '#[cfg(feature = "std")]'}


def test_another_format_version_is_named_as_one():
    """A format bump reports as a bump, not as whichever field happened to move first."""
    payload = copy.deepcopy(_CRATE)
    payload["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(UnreadableRustdoc, match=f"format_version {FORMAT_VERSION + 1}"):
        parse_crate(_text(payload))


def test_a_moved_field_fails_the_load_rather_than_reading_as_absent():
    payload = copy.deepcopy(_CRATE)
    del payload["index"]["1"]["inner"]["function"]["sig"]["inputs"]
    with pytest.raises(UnreadableRustdoc, match="inputs"):
        parse_crate(_text(payload))


def test_an_unknown_procedural_macro_kind_is_refused():
    # Which of `thing!()`, `#[thing]` and `#[derive(Thing)]` to write is the whole of the entry.
    payload = copy.deepcopy(_CRATE)
    payload["index"]["3"]["inner"]["proc_macro"]["kind"] = "function_like"
    with pytest.raises(UnreadableRustdoc, match="function_like"):
        parse_crate(_text(payload))


def test_not_json_is_refused():
    with pytest.raises(UnreadableRustdoc, match="not JSON"):
        parse_crate("{")
