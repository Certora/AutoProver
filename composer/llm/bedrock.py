"""Backend selection for Claude, and the model-id mapping for Claude on Amazon Bedrock.

Claude is reached either through the Anthropic API or through Amazon Bedrock's Mantle
endpoint (the native Anthropic Messages API, served at ``bedrock-mantle.{region}.api.aws``).
``AUTOPROVER_LLM_BACKEND`` picks one per process.

Model ids stay logical (``claude-opus-5-5``) everywhere outside the Bedrock chat client:
CLI defaults, budgets, logs, pricing and usage records all key on them. Only the request
sent to Bedrock carries the Bedrock id, and the model name a Bedrock response reports is
mapped back before anything downstream sees it.

Kept free of langchain imports so the registry can use the id predicates cheaply; the chat
model itself lives in :mod:`composer.llm.bedrock_chat`.
"""

import os
from typing import Literal

type LLMBackend = Literal["anthropic", "bedrock"]

BACKEND_ENV_VAR = "AUTOPROVER_LLM_BACKEND"
_DEFAULT_BACKEND: LLMBackend = "anthropic"
_BACKENDS: tuple[LLMBackend, ...] = ("anthropic", "bedrock")

# Searched in this order, matching how the AWS SDKs resolve a region from the environment.
REGION_ENV_VARS = ("AWS_REGION", "AWS_DEFAULT_REGION")

# Bedrock names a model ``<vendor>.<model>``, optionally behind a geography prefix for a
# cross-region inference profile (``us.anthropic.<model>``). Claude's vendor segment:
_BEDROCK_VENDOR = "anthropic"


def llm_backend() -> LLMBackend:
    """The backend ``AUTOPROVER_LLM_BACKEND`` selects; unset or empty means ``anthropic``.
    Read on every call rather than once, so the value in force when a model is built is
    the one that model uses. Raises ``ValueError`` for any other value."""
    raw = os.environ.get(BACKEND_ENV_VAR, "").strip().lower()
    if not raw:
        return _DEFAULT_BACKEND
    match raw:
        case "anthropic" | "bedrock":
            return raw
        case _:
            raise ValueError(
                f"{BACKEND_ENV_VAR}={os.environ[BACKEND_ENV_VAR]!r} is not a known LLM "
                f"backend; expected one of: {', '.join(_BACKENDS)}"
            )


def bedrock_region() -> str:
    """The AWS region Bedrock requests go to. Raises ``ValueError`` when none is set, so a
    misconfigured job fails when its model is built, not on its first request."""
    for var in REGION_ENV_VARS:
        if region := os.environ.get(var, "").strip():
            return region
    raise ValueError(
        f"{BACKEND_ENV_VAR}=bedrock needs an AWS region: set "
        f"{' or '.join(REGION_ENV_VARS)}"
    )


def _arn_resource(model_id: str) -> str | None:
    """The resource part of a Bedrock ARN (``arn:<partition>:bedrock:<region>:<account>:
    <resource>``), or ``None`` if ``model_id`` is not one."""
    parts = model_id.split(":", 5)
    if len(parts) == 6 and parts[0] == "arn" and parts[2] == "bedrock":
        return parts[5]
    return None


def _vendor_qualified_suffix(model_id: str) -> str | None:
    """The model name after the ``anthropic`` vendor segment of a dotted Bedrock id
    (``anthropic.claude-x`` or ``us.anthropic.claude-x``), or ``None`` if ``model_id`` has
    no such segment. Claude's own ids contain no dot, so a dot only ever separates a
    vendor or geography prefix."""
    head, sep, rest = model_id.rpartition(f"{_BEDROCK_VENDOR}.")
    if not sep or not rest:
        return None
    if head and not head.endswith("."):
        # ``foo-anthropic.x``: the vendor segment must be a whole dotted component.
        return None
    return rest


def is_bedrock_model_id(model_id: str) -> bool:
    """Whether ``model_id`` is a Bedrock-native name (a Bedrock ARN, or a dotted
    vendor-qualified id) rather than a logical one."""
    return _arn_resource(model_id) is not None or _vendor_qualified_suffix(model_id) is not None


def to_bedrock_model_id(logical: str) -> str:
    """The Bedrock id for a logical Claude id: Bedrock names Claude models by the
    Anthropic id behind an ``anthropic.`` vendor prefix."""
    if is_bedrock_model_id(logical):
        raise ValueError(f"{logical!r} is already a Bedrock model id, not a logical one")
    return f"{_BEDROCK_VENDOR}.{logical}"


def to_logical_model_id(model_id: str) -> str:
    """The logical id for a model name a Bedrock response reports. Names that carry no
    Bedrock prefix pass through unchanged."""
    resource = _arn_resource(model_id)
    if resource is not None:
        # ``inference-profile/us.anthropic.claude-x`` and ``foundation-model/anthropic.claude-x``
        model_id = resource.rpartition("/")[2]
    suffix = _vendor_qualified_suffix(model_id)
    return suffix if suffix is not None else model_id
