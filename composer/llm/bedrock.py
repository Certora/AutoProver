"""Backend selection for Claude, and the model-id mapping for Claude on Amazon Bedrock.

Claude is reached either through the Anthropic API or through Amazon Bedrock's Mantle
endpoint (the native Anthropic Messages API, served at ``bedrock-mantle.{region}.api.aws``).
``AUTOPROVER_LLM_BACKEND`` picks one per process.

Model ids stay logical (``claude-opus-5-5``) everywhere outside the Bedrock chat client:
CLI defaults, budgets, logs, pricing and usage records all key on them. Only the request
sent to Bedrock carries the Bedrock id, and the model name a Bedrock response reports is
replaced by the logical id the request was built from before anything downstream sees it.

Bedrock has no Files API, so binary documents travel inline as base64 in every request
that carries them, and Bedrock caps a request at 20 MB. Two byte budgets, measured on the
serialized request, keep a run under that cap (see :mod:`composer.llm.bedrock_inline`):

- ``AUTOPROVER_BEDROCK_MAX_DOCUMENT_BYTES`` (default 8,000,000): the largest base64 body one
  document may have;
- ``AUTOPROVER_BEDROCK_MAX_REQUEST_BYTES`` (default 16,000,000): the largest serialized
  request body. The margin below 20 MB absorbs request headers and the difference between
  this estimate and what the SDK sends.

A document over budget is replaced by a short text block naming it, never an error.

Bedrock admits a request against the account's input-tokens-per-minute quota by its input
tokens plus its ``max_tokens``, before the model has produced anything. A ``max_tokens``
far above what a turn really produces therefore throttles a run long before its real usage
reaches the quota. ``AUTOPROVER_BEDROCK_MAX_TOKENS`` (default 64,000) caps ``max_tokens``
on Bedrock requests; see :data:`DEFAULT_MAX_TOKENS`.

Transient failures are retried, deterministic ones are not (see
:meth:`composer.llm.anthropic.AnthropicService.should_retry`). Besides the HTTP statuses the
Anthropic API shares, Bedrock reports some errors by its own exception names
(:data:`TRANSIENT_ERROR_TYPES`), and a request can fail before it is sent, while the AWS
credentials that sign it are fetched (:class:`BedrockCredentialRefreshError`,
:class:`BedrockCredentialsError`).

Kept free of langchain imports so the registry can use the id predicates cheaply; the chat
model itself lives in :mod:`composer.llm.bedrock_chat`.
"""

import os
from collections.abc import Mapping
from typing import Literal

import anthropic

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


MAX_DOCUMENT_BYTES_ENV_VAR = "AUTOPROVER_BEDROCK_MAX_DOCUMENT_BYTES"
MAX_REQUEST_BYTES_ENV_VAR = "AUTOPROVER_BEDROCK_MAX_REQUEST_BYTES"
DEFAULT_MAX_DOCUMENT_BYTES = 8_000_000
DEFAULT_MAX_REQUEST_BYTES = 16_000_000


def _positive_setting(var: str, default: int, unit: str) -> int:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"{var}={os.environ[var]!r} is not a positive number of {unit}")
    return value


def _byte_budget(var: str, default: int) -> int:
    return _positive_setting(var, default, "bytes")


def max_document_bytes() -> int:
    """The base64 size above which one inline document is left out of a Bedrock request.
    Raises ``ValueError`` for a value that is not a positive integer."""
    return _byte_budget(MAX_DOCUMENT_BYTES_ENV_VAR, DEFAULT_MAX_DOCUMENT_BYTES)


def max_request_bytes() -> int:
    """The serialized request size above which inline documents are left out of a Bedrock
    request. Raises ``ValueError`` for a value that is not a positive integer."""
    return _byte_budget(MAX_REQUEST_BYTES_ENV_VAR, DEFAULT_MAX_REQUEST_BYTES)


MAX_TOKENS_ENV_VAR = "AUTOPROVER_BEDROCK_MAX_TOKENS"
DEFAULT_MAX_TOKENS = 64_000
"""The ``max_tokens`` cap on Bedrock requests when ``AUTOPROVER_BEDROCK_MAX_TOKENS`` is unset.

A turn's output is its thinking plus its visible text and tool calls. On models before 4.6
thinking takes a fixed budget, ``--thinking-tokens`` (16,384 by default), which has to stay
below ``max_tokens``; on 4.6 and later thinking is adaptive and bounded only by
``max_tokens``. The longest visible output a composer turn produces is a tool call carrying a
whole spec or harness file, a few thousand to some tens of thousands of tokens. 64,000 holds
the default thinking budget plus about 47,000 tokens of such output, and is Anthropic's
suggested ``max_tokens`` for streamed requests; it reserves half the quota that the
128,000-token default would. A turn that reaches the cap ends with ``stop_reason``
``max_tokens``, so a lower cap trades quota for a higher chance of a cut-off turn."""


def max_output_tokens() -> int:
    """The ``max_tokens`` cap for Bedrock requests. Raises ``ValueError`` for a value that is
    not a positive integer."""
    return _positive_setting(MAX_TOKENS_ENV_VAR, DEFAULT_MAX_TOKENS, "tokens")


TRANSIENT_ERROR_TYPES = frozenset({
    "ThrottlingException",          # 429: over the account's request or token quota
    "ModelNotReadyException",       # 429: the model is not ready to serve yet
    "ServiceUnavailableException",  # 503
    "InternalServerException",      # 500
    "ModelTimeoutException",        # 408: the model took too long to answer
    "ModelStreamErrorException",    # an error while the response streamed
    "ExpiredTokenException",        # 403: signed with credentials that expired in flight
})
"""Bedrock's names for the errors worth retrying. Most of them come with a status code that
already says so; the names matter for an error reported inside an open stream, whose status
is the stream's 200, and for ``ExpiredTokenException``, a 403 that the next request, signed
with refreshed credentials, does not repeat. The name is read from the payload's
``error.type``, or, for a payload without one, from the ``x-amzn-errortype`` response header
(see :func:`aws_error_type`); a payload with an Anthropic error type is classified by that."""

AWS_ERROR_TYPE_HEADER = "x-amzn-errortype"
"""The response header in which AWS front ends name the exception behind an error response,
as ``<ExceptionName>`` or ``<ExceptionName>:<detail>``."""


def aws_error_type(headers: Mapping[str, str]) -> str | None:
    """The exception name in the ``x-amzn-errortype`` header of an error response, without
    any ``:`` suffix, or ``None`` when the header is absent or empty."""
    name = headers.get(AWS_ERROR_TYPE_HEADER, "").partition(":")[0].strip()
    return name or None


def is_transient_error_type(error_type: str | None) -> bool:
    """Whether ``error_type``, the exception name of an error response, is a Bedrock
    exception name in :data:`TRANSIENT_ERROR_TYPES`."""
    return error_type in TRANSIENT_ERROR_TYPES


class BedrockCredentialRefreshError(anthropic.RetryableError):
    """Fetching the AWS credentials that sign a Bedrock request failed for a reason that can
    pass, such as the container credentials endpoint timing out. The request was not sent.

    A ``RetryableError``, so the SDK retries the request with its own backoff; once its
    retries are spent, this reaches the caller and the run-wide retry policy takes over."""


class BedrockCredentialsError(anthropic.AnthropicError):
    """The AWS credentials that sign a Bedrock request could not be found or are malformed.
    The request was not sent, and retrying does not help."""


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
