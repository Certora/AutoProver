"""A base model for structured LLM output that accepts JSON-encoded containers.

A model answering through a forced tool call sometimes encodes a list or object argument as a
JSON string (``"groups": "[{...}]"`` instead of ``"groups": [{...}]``). The content is right; only
the encoding is off by one level. `DecodesJsonStrings` decodes such strings during validation, at
any depth, wherever the field's type does not itself accept a string. A string that does not decode,
or decodes to the wrong shape, is left to pydantic's normal validation and its usual error.
"""
import json
import types
from typing import Annotated, Any, Literal, TypeAliasType, Union, get_args, get_origin

from pydantic import BaseModel, model_validator


def _unwrap(tp: object) -> object:
    """Strip ``type`` aliases and ``Annotated`` down to the underlying type."""
    while True:
        if isinstance(tp, TypeAliasType):
            tp = tp.__value__
        elif get_origin(tp) is Annotated:
            tp = get_args(tp)[0]
        else:
            return tp


def _accepts_str(tp: object) -> bool:
    """Whether a string is a valid value for ``tp`` as it stands, so must not be decoded."""
    tp = _unwrap(tp)
    if tp is Any or tp is object:
        return True
    if isinstance(tp, type):
        return issubclass(tp, str)
    origin = get_origin(tp)
    if origin is Literal:
        return any(isinstance(v, str) for v in get_args(tp))
    if origin is Union or origin is types.UnionType:
        return any(_accepts_str(a) for a in get_args(tp))
    return False


def _decode(value: object, tp: object) -> object:
    """``value`` with every JSON string that sits where ``tp`` expects a non-string decoded."""
    tp = _unwrap(tp)
    if isinstance(value, str) and not _accepts_str(tp):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    origin = get_origin(tp)
    args = get_args(tp)
    if origin in (list, set, frozenset) and isinstance(value, list):
        return [_decode(v, args[0]) for v in value]
    if origin is tuple and isinstance(value, (list, tuple)):
        if len(args) == 2 and args[1] is Ellipsis:
            return [_decode(v, args[0]) for v in value]
        if len(args) == len(value):
            return [_decode(v, a) for v, a in zip(value, args)]
        return value
    if origin is dict and isinstance(value, dict):
        return {k: _decode(v, args[1]) for k, v in value.items()}
    if origin is Union or origin is types.UnionType:
        # Only an optional of one type has an unambiguous target to descend into.
        members = [a for a in args if a is not type(None)]
        return _decode(value, members[0]) if len(members) == 1 else value
    if isinstance(tp, type) and issubclass(tp, BaseModel) and isinstance(value, dict):
        return _decode_fields(tp, value)
    return value


def _decode_fields(model: type[BaseModel], data: dict[str, object]) -> dict[str, object]:
    out = dict(data)
    for name, field in model.model_fields.items():
        key = field.alias or name
        if key in out:
            out[key] = _decode(out[key], field.annotation)
    return out


class DecodesJsonStrings(BaseModel):
    """Base for a model used as structured LLM output: a field (or a nested field, or a list
    element) whose type does not accept a string, but which arrives as a JSON string, is decoded
    before validation."""

    @model_validator(mode="before")
    @classmethod
    def _decode_json_strings(cls, data: object) -> object:
        if isinstance(data, dict):
            return _decode_fields(cls, data)
        return data
