"""Mock LLM backend for CI testing.

Generates valid Pydantic model instances without making any API calls.
Activated via ``PREAUDIT_LLM_BACKEND=mock`` environment variable.
"""

import random
import types
import typing
from typing import Any, TypeVar, get_args, get_origin

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

_MOCK_TEXT = "## Mock Analysis\n\nThis is a mock LLM response generated for CI testing.\n\nNo real API call was made.\n"


def generate_mock_structured(output_type: type[M]) -> M:
    """Generate a valid Pydantic model instance with random but valid field values."""
    kwargs: dict[str, Any] = {}
    for name, field_info in output_type.model_fields.items():
        kwargs[name] = _generate_field_value(name, field_info.annotation, field_info.default)
    return output_type(**kwargs)


def generate_mock_text() -> str:
    """Return a fixed mock text response."""
    return _MOCK_TEXT


def _generate_field_value(name: str, annotation: Any, default: Any) -> Any:
    """Generate a valid value for a Pydantic field based on its type annotation."""
    if default is not None and not _is_pydantic_required(default):
        return default

    if annotation is None:
        return f"Mock {name}"

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Literal["YES", "NO"] etc. — random choice
    if origin is typing.Literal:
        return random.choice(args)

    # Optional[X] or X | None — return None
    if (origin is typing.Union or origin is types.UnionType) and type(None) in args:
        return None

    # list[X] — return empty list
    if origin is list:
        return []

    # dict[K, V] — return empty dict
    if origin is dict:
        return {}

    # Nested BaseModel — recurse
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return generate_mock_structured(annotation)

    # Primitive types
    if annotation is str:
        return f"Mock {name}"
    if annotation is int:
        return 50
    if annotation is float:
        return 0.5
    if annotation is bool:
        return random.choice([True, False])

    # Fallback
    return f"Mock {name}"


def _is_pydantic_required(default: Any) -> bool:
    """Check if a Pydantic field default means 'required' (no actual default)."""
    from pydantic_core import PydanticUndefinedType
    return isinstance(default, PydanticUndefinedType)
