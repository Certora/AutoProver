"""``ChatAnthropic`` pointed at Claude on Amazon Bedrock through the Mantle endpoint.

Mantle serves the Anthropic Messages API itself, so everything ``ChatAnthropic`` does to
build requests and parse responses applies unchanged. This subclass changes six things:

- the SDK clients are the Bedrock Mantle ones, which sign requests with SigV4 from the
  default AWS credential chain and need no Anthropic API key;
- the request carries the Bedrock model id, mapped from the logical one;
- inline documents are dropped, latest first, until the request fits the request budget,
  and a request that refers to a Files API upload fails before it is sent (see
  :func:`composer.llm.bedrock_inline.fit_request_to_budget`);
- the model name a response reports is replaced by the chat model's own logical id, so usage
  records, pricing and logs see the same name the run was configured with;
- a failure to fetch the AWS credentials that sign a request is raised as an error that says
  whether retrying can help (see :class:`CredentialErrorMiddleware`);
- the SDK's retries back off for longer (see :func:`composer.llm.bedrock.retry_delay_seconds`),
  and a request Bedrock rejects as too large is sent again with fewer inline documents.
"""

import asyncio
import logging
import random
from functools import cached_property
from typing import Any, override

import anthropic
import boto3
import httpx
import botocore.exceptions
from anthropic._models import FinalRequestOptions
from langchain_anthropic import ChatAnthropic
from langchain_anthropic._client_utils import (
    _get_default_async_httpx_client, _get_default_httpx_client,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatResult
from pydantic import Field, PrivateAttr

from composer.llm.bedrock import (
    TRANSIENT_CREDENTIAL_ERROR_CODES, BedrockCredentialRefreshError, BedrockCredentialsError,
    max_request_bytes, retry_delay_seconds, to_bedrock_model_id,
)
from composer.llm.bedrock_inline import (
    DocumentKey, fit_request_to_budget, serialized_request_size,
)

# Response fields that name the model: ``model`` as Anthropic returns it, and
# ``model_name``, which langchain copies it into.
_MODEL_NAME_KEYS = ("model", "model_name")


def _normalize_model_names(metadata: dict[str, Any] | None, logical: str) -> None:
    """Replace every model name in ``metadata`` with ``logical``. Bedrock reports the model
    under a name of its own (vendor-qualified, versioned, geography-prefixed or an ARN), and
    the request was built from ``logical``, so that is the model that answered."""
    if metadata is None:
        return
    for key in _MODEL_NAME_KEYS:
        if isinstance(metadata.get(key), str):
            metadata[key] = logical


# Raised while botocore fetches credentials from a remote source (the container or instance
# metadata endpoint, an STS call) for reasons that can pass: the source answered with an
# error or not at all.
_TRANSIENT_CREDENTIAL_ERRORS: tuple[type[Exception], ...] = (
    botocore.exceptions.CredentialRetrievalError,
    botocore.exceptions.MetadataRetrievalError,
    botocore.exceptions.ConnectionError,
    botocore.exceptions.HTTPClientError,
)


def _credential_error(exc: botocore.exceptions.BotoCoreError) -> anthropic.AnthropicError:
    if isinstance(exc, _TRANSIENT_CREDENTIAL_ERRORS):
        return BedrockCredentialRefreshError(
            f"Could not fetch AWS credentials to sign the Bedrock request: {exc}"
        )
    return BedrockCredentialsError(
        "Could not sign the Bedrock request with AWS credentials from the default credential "
        f"chain: {exc}"
    )


def _client_error(exc: botocore.exceptions.ClientError) -> anthropic.AnthropicError:
    """The error to raise for ``exc``, an AWS API call that failed while credentials were
    fetched: retryable when its error code or status says the failure can pass."""
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    transient = error.get("Code") in TRANSIENT_CREDENTIAL_ERROR_CODES or (
        isinstance(status, int) and (status == 429 or status >= 500)
    )
    if transient:
        return BedrockCredentialRefreshError(
            f"Could not fetch AWS credentials to sign the Bedrock request: {exc}"
        )
    return BedrockCredentialsError(
        f"Could not get AWS credentials to sign the Bedrock request: {exc}"
    )


def _no_credentials_error(exc: RuntimeError, aws_region: str) -> anthropic.AnthropicError | None:
    """The error to raise for ``exc``, a ``RuntimeError`` from a request attempt, when the
    default AWS credential chain has no credentials; ``None`` when it has some, so ``exc``
    has another cause.

    The SDK's signer raises a plain ``RuntimeError`` when its boto3 session resolves no
    credentials. Resolving them again here, from the same chain, tells that case apart
    without depending on the SDK's message, and costs nothing on requests that succeed."""
    try:
        credentials = boto3.Session(region_name=aws_region).get_credentials()
    except botocore.exceptions.BotoCoreError as boto_exc:
        return _credential_error(boto_exc)
    if credentials is not None:
        return None
    return BedrockCredentialsError(
        "Could not sign the Bedrock request: the default AWS credential chain has no "
        "credentials (environment variables, shared config and credentials files, container "
        "or instance role)"
    )


class CredentialErrorMiddleware(anthropic.Middleware):
    """Turns a failure of SigV4 signing into a :class:`BedrockCredentialRefreshError` or a
    :class:`BedrockCredentialsError`, chained to the original: a botocore error while
    credentials are fetched, a failed AWS API call made for them (such as a throttled STS
    role assumption), or the ``RuntimeError`` the SDK raises when the default credential
    chain has none.

    The Mantle clients sign each attempt before sending it, and the SDK leaves an error from
    signing as it is, so a passing failure to fetch credentials would otherwise end the
    request at once. The SDK runs middleware once per attempt, inside its retry loop, and
    retries a ``RetryableError`` as it does a dropped connection."""

    def __init__(self, aws_region: str) -> None:
        self._aws_region = aws_region

    @override
    def handle(self, request: anthropic.APIRequest, call_next: anthropic.CallNext) -> anthropic.APIResponse[Any]:
        try:
            return call_next(request)
        except botocore.exceptions.BotoCoreError as exc:
            raise _credential_error(exc) from exc
        except botocore.exceptions.ClientError as exc:
            raise _client_error(exc) from exc
        except RuntimeError as exc:
            if (error := _no_credentials_error(exc, self._aws_region)) is None:
                raise
            raise error from exc

    @override
    async def handle_async(
        self, request: anthropic.APIRequest, call_next: anthropic.AsyncCallNext
    ) -> anthropic.AsyncAPIResponse[Any]:
        try:
            return await call_next(request)
        except botocore.exceptions.BotoCoreError as exc:
            raise _credential_error(exc) from exc
        except botocore.exceptions.ClientError as exc:
            raise _client_error(exc) from exc
        except RuntimeError as exc:
            # Resolving credentials can block on a metadata endpoint, as the SDK's own
            # signing does, so it runs off the event loop like that.
            error = await asyncio.to_thread(_no_credentials_error, exc, self._aws_region)
            if error is None:
                raise
            raise error from exc


def _bedrock_retry_timeout(
    client: anthropic.AnthropicBedrockMantle | anthropic.AsyncAnthropicBedrockMantle,
    remaining_retries: int,
    options: FinalRequestOptions,
    response_headers: httpx.Headers | None,
) -> float:
    """The SDK's retry wait with the Bedrock backoff of
    :func:`composer.llm.bedrock.retry_delay_seconds`: a ``retry-after`` of up to a minute is
    honoured as the SDK does, otherwise the wait doubles up to a minute, less up to a quarter
    of jitter so that parallel requests do not retry in step."""
    retry_after = client._parse_retry_after_header(response_headers)
    if retry_after is not None and 0 < retry_after <= 60:
        return retry_after
    retries_done = options.get_max_retries(client.max_retries) - remaining_retries
    return retry_delay_seconds(retries_done) * (1 - 0.25 * random.random())


class _MantleClient(anthropic.AnthropicBedrockMantle):
    @override
    def _calculate_retry_timeout(
        self,
        remaining_retries: int,
        options: FinalRequestOptions,
        response_headers: httpx.Headers | None = None,
    ) -> float:
        return _bedrock_retry_timeout(self, remaining_retries, options, response_headers)


class _AsyncMantleClient(anthropic.AsyncAnthropicBedrockMantle):
    @override
    def _calculate_retry_timeout(
        self,
        remaining_retries: int,
        options: FinalRequestOptions,
        response_headers: httpx.Headers | None = None,
    ) -> float:
        return _bedrock_retry_timeout(self, remaining_retries, options, response_headers)


_logger = logging.getLogger(__name__)

# How many times a request Bedrock rejects as too large is sent again with a lower request
# budget. Each try leaves out more inline documents; a request that holds none is not retried.
_TOO_LARGE_RETRIES = 3


class ChatAnthropicBedrock(ChatAnthropic):
    """``ChatAnthropic`` whose requests go to Bedrock Mantle in ``aws_region``.

    ``model`` holds the logical id; only the request payload carries the Bedrock id."""

    aws_region: str
    max_request_bytes: int = Field(default_factory=max_request_bytes)
    """The serialized request size above which inline documents are left out."""

    # The inline documents this model has warned about leaving out, so a document dropped
    # from every request of a conversation is warned about once.
    _omitted_documents: set[DocumentKey] = PrivateAttr(default_factory=set)
    # The request budget after Bedrock rejected a request as too large; ``None`` until then.
    _lowered_request_bytes: int | None = PrivateAttr(default=None)

    def _mantle_params(self) -> dict[str, Any]:
        """The parent's client settings (timeout, retries, default headers) minus the
        Anthropic API key and base URL, which belong to the first-party endpoint. With no
        key given, the Mantle client signs with SigV4, and :class:`CredentialErrorMiddleware`
        classifies a failure to fetch the credentials it signs with."""
        params = self._client_params
        mantle: dict[str, Any] = {
            "aws_region": self.aws_region,
            "max_retries": params["max_retries"],
            "default_headers": params["default_headers"],
            "middleware": [CredentialErrorMiddleware(self.aws_region)],
        }
        if "timeout" in params:
            mantle["timeout"] = params["timeout"]
        return mantle

    def _http_client_params(self) -> dict[str, Any]:
        """Arguments for langchain's cached httpx clients, as the parent builds them: one
        connection pool shared by every model instance with the same settings, and
        ``anthropic_proxy`` honoured. The SDK resolves each request URL against its own
        base URL, so ``base_url`` here only keys the cache per region."""
        http: dict[str, Any] = {"base_url": f"https://bedrock-mantle.{self.aws_region}.api.aws"}
        if "timeout" in self._client_params:
            http["timeout"] = self._client_params["timeout"]
        if self.anthropic_proxy:
            http["anthropic_proxy"] = self.anthropic_proxy
        return http

    # The Mantle clients are not ``anthropic.Client`` subclasses, but expose the
    # ``messages`` and ``beta.messages`` resources ``ChatAnthropic`` calls.
    @cached_property
    @override
    def _client(self) -> anthropic.AnthropicBedrockMantle:  # pyright: ignore[reportIncompatibleVariableOverride]
        return _MantleClient(
            **self._mantle_params(),
            http_client=_get_default_httpx_client(**self._http_client_params()),
        )

    @cached_property
    @override
    def _async_client(self) -> anthropic.AsyncAnthropicBedrockMantle:  # pyright: ignore[reportIncompatibleVariableOverride]
        return _AsyncMantleClient(
            **self._mantle_params(),
            http_client=_get_default_async_httpx_client(**self._http_client_params()),
        )

    @override
    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: dict,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload["model"] = to_bedrock_model_id(payload["model"])
        return fit_request_to_budget(payload, self._request_budget(), self._omitted_documents)

    def _request_budget(self) -> int:
        return self._lowered_request_bytes or self.max_request_bytes

    def _refit_after_too_large(
        self, payload: dict, exc: anthropic.RequestTooLargeError
    ) -> dict | None:
        """``payload`` refitted to a lower request budget after Bedrock rejected it as too
        large, or ``None`` when it holds no inline document left to drop. The budget becomes
        three quarters of the smaller of the current budget and the rejected request's size,
        so at least one more document is left out. It stays lowered for the later requests
        of this model, which carry the same history."""
        budget = min(self._request_budget(), serialized_request_size(payload)) * 3 // 4
        refitted = fit_request_to_budget(payload, budget, self._omitted_documents)
        if refitted is payload:
            return None
        _logger.warning(
            "Bedrock rejected a request as too large (%s); lowering the request budget for "
            "inline documents to %d bytes and sending it again", exc, budget,
        )
        self._lowered_request_bytes = budget
        return refitted

    @override
    def _create(self, payload: dict) -> Any:
        for _ in range(_TOO_LARGE_RETRIES):
            try:
                return super()._create(payload)
            except anthropic.RequestTooLargeError as exc:
                if (refitted := self._refit_after_too_large(payload, exc)) is None:
                    raise
                payload = refitted
        return super()._create(payload)

    @override
    async def _acreate(self, payload: dict) -> Any:
        for _ in range(_TOO_LARGE_RETRIES):
            try:
                return await super()._acreate(payload)
            except anthropic.RequestTooLargeError as exc:
                if (refitted := self._refit_after_too_large(payload, exc)) is None:
                    raise
                payload = refitted
        return await super()._acreate(payload)

    @override
    def _make_message_chunk_from_anthropic_event(
        self,
        event: anthropic.types.RawMessageStreamEvent,
        *,
        stream_usage: bool = True,
        coerce_content_to_string: bool,
        block_start_event: anthropic.types.RawMessageStreamEvent | None = None,
    ) -> tuple[AIMessageChunk | None, anthropic.types.RawMessageStreamEvent | None]:
        chunk, block_start = super()._make_message_chunk_from_anthropic_event(
            event,
            stream_usage=stream_usage,
            coerce_content_to_string=coerce_content_to_string,
            block_start_event=block_start_event,
        )
        if chunk is not None:
            _normalize_model_names(chunk.response_metadata, self.model)
        return chunk, block_start

    @override
    def _format_output(self, data: Any, **kwargs: Any) -> ChatResult:
        result = super()._format_output(data, **kwargs)
        _normalize_model_names(result.llm_output, self.model)
        for generation in result.generations:
            _normalize_model_names(generation.message.response_metadata, self.model)
        return result
