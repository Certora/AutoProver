"""Drift guards for the hand-maintained union wiring in unions.py."""

from typing import get_args

from pydantic import BaseModel

from certora_autosetup.solidity_ast import unions
from certora_autosetup.solidity_ast.base import UNKNOWN_TAG, UnknownNode

UNION_TO_TAGSET = {
    "Expression": unions._EXPRESSION_TAGS,
    "Statement": unions._STATEMENT_TAGS,
    "TypeName": unions._TYPENAME_TAGS,
    "SourceUnitNode": unions._SOURCEUNITNODE_TAGS,
    "ContractBodyNode": unions._CONTRACTBODYNODE_TAGS,
    "Node": unions._NODE_TAGS,
}


def _tags_of(alias: object) -> set[str]:
    """The Tag names attached to a union alias's members (excluding the fallback)."""
    union_type, _discriminator = get_args(alias)
    tags = set()
    for member in get_args(union_type):
        _member_type, tag = get_args(member)
        tags.add(tag.tag)
    return tags - {UNKNOWN_TAG}


def test_union_tag_sets_match_members() -> None:
    """A member whose tag is missing from the discriminator's frozenset would be
    silently routed to UnknownNode — assert the hand-written sets cannot drift."""
    for name, tagset in UNION_TO_TAGSET.items():
        alias = getattr(unions, name)
        assert _tags_of(alias) == set(tagset), name


def test_every_union_has_unknown_fallback() -> None:
    for name in UNION_TO_TAGSET:
        union_type, _ = get_args(getattr(unions, name))
        members = {get_args(m)[0] for m in get_args(union_type)}
        assert UnknownNode in members, name


def test_registry_classes_are_models() -> None:
    for def_name, cls in unions.MODEL_BY_SCHEMA_DEF.items():
        assert isinstance(cls, type) and issubclass(cls, BaseModel), def_name
