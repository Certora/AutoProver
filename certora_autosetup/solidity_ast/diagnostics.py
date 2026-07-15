"""Fidelity diagnostics: does a typed tree serialize back to the exact source JSON?

Used by the round-trip tests and by ``python -m certora_autosetup.solidity_ast`` to
validate the models against real dumps at scale.
"""

from __future__ import annotations

from typing import Any

from .loader import SourceAst


def roundtrip_diffs(source: SourceAst, limit: int = 50) -> list[str]:
    """Structural differences between ``source.root`` re-serialized and the raw
    SourceUnit JSON it was parsed from (empty list == byte-loyal modulo key order).

    ``model_dump(exclude_unset=True)`` keeps exactly the fields present in the input
    (absent optional fields stay absent, explicit nulls stay null, unknown fields ride
    along in ``model_extra``). The one deliberate normalization is reversed before
    comparing: the certoraRun contract-name stamp that lands inside the plain
    ``internalFunctionIDs`` map is dropped by the model, so it is dropped from the
    raw side too.
    """
    if source.root is None:
        return []
    raw_root = next(
        n
        for n in source.raw.values()
        if isinstance(n, dict) and n.get("nodeType") == "SourceUnit"
    )
    dumped = source.root.model_dump(mode="json", by_alias=True, exclude_unset=True)
    expected = _drop_internal_function_id_stamp(raw_root)
    diffs: list[str] = []
    _diff(dumped, expected, "", diffs, limit)
    return diffs


def _drop_internal_function_id_stamp(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "internalFunctionIDs" and isinstance(value, dict):
                value = {k: v for k, v in value.items() if k != "certora_contract_name"}
            out[key] = _drop_internal_function_id_stamp(value)
        return out
    if isinstance(node, list):
        return [_drop_internal_function_id_stamp(item) for item in node]
    return node


def _diff(dumped: Any, original: Any, path: str, out: list[str], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(dumped, dict) and isinstance(original, dict):
        for key in sorted(dumped.keys() | original.keys()):
            if key not in original:
                out.append(f"{path}.{key}: only in re-serialized output")
            elif key not in dumped:
                out.append(f"{path}.{key}: lost from the original")
            else:
                _diff(dumped[key], original[key], f"{path}.{key}", out, limit)
    elif isinstance(dumped, list) and isinstance(original, list):
        if len(dumped) != len(original):
            out.append(f"{path}: list length {len(dumped)} != {len(original)}")
        else:
            for i, (d, o) in enumerate(zip(dumped, original)):
                _diff(d, o, f"{path}[{i}]", out, limit)
    elif dumped != original:
        out.append(f"{path}: {dumped!r} != {original!r}")
