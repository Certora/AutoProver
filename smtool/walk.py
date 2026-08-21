"""Generic AST traversal over composer.cvl.schema pydantic trees."""
from __future__ import annotations

from typing import Iterator, TypeVar

from pydantic import BaseModel

import composer.cvl.schema as S

T = TypeVar("T")


def iter_instances(obj, cls: type[T]) -> Iterator[T]:
    """Yield every instance of `cls` anywhere inside a pydantic tree / list / tuple."""
    if isinstance(obj, cls):
        yield obj
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from iter_instances(getattr(obj, name), cls)
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from iter_instances(x, cls)


def calls(obj) -> Iterator[S.FunctionApplication]:
    return iter_instances(obj, S.FunctionApplication)


def contract_calls(obj, alias: str | None = None) -> Iterator[S.FunctionApplication]:
    """FunctionApplications that are contract calls (host_contract set). If `alias` given,
    only that contract."""
    for fa in calls(obj):
        if fa.host_contract is not None and (alias is None or fa.host_contract == alias):
            yield fa
