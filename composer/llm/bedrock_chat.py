"""``ChatAnthropic`` pointed at Claude on Amazon Bedrock through the Mantle endpoint.

Mantle serves the Anthropic Messages API itself, so everything ``ChatAnthropic`` does to
build requests and parse responses applies unchanged. This subclass changes five things:

- the SDK clients are the Bedrock Mantle ones, which sign requests with SigV4 from the
  default AWS credential chain and need no Anthropic API key;
- the request carries the Bedrock model id, mapped from the logical one;
- inline documents are dropped, latest first, until the request fits the request budget,
  and a request that refers to a Files API upload fails before it is sent (see
  :func:`composer.llm.bedrock_inline.fit_request_to_budget`);
- the model name a response reports is mapped back to the logical id, so usage records,
  pricing and logs see the same name the run was configured with;
- a failure to fetch the AWS credentials that sign a request is raised as an error that says
  whether retrying can help (see :class:`CredentialErrorMiddleware`).
"""

from functools import cached_property
from typing import Any, override

import anthropic
import botocore.exceptions
from langchain_anthropic import ChatAnthropic
from langchain_anthropic._client_utils import (
    _get_default_async_httpx_client, _get_default_httpx_client,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatResult
from pydantic import Field

from composer.llm.bedrock import (
    BedrockCredentialRefreshError, BedrockCredentialsError, max_request_bytes,
    to_bedrock_model_id, to_logical_model_id,
)
from composer.llm.bedrock_inline import fit_request_to_budget

# Response fields that name the model: ``model`` as Anthropic returns it, and
# ``model_name``, which langchain copies it into.
_MODEL_NAME_KEYS = ("model", "model_name")


def _normalize_model_names(metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    for key in _MODEL_NAME_KEYS:
        if isinstance(name := metadata.get(key), str):
            metadata[key] = to_logical_model_id(name)


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


class CredentialErrorMiddleware(anthropic.Middleware):
    """Turns a botocore error from SigV4 signing into a :class:`BedrockCredentialRefreshError`
    or a :class:`BedrockCredentialsError`, chained to the original.

    The Mantle clients sign each attempt before sending it, and the SDK leaves an error from
    signing as it is, so a passing failure to fetch credentials would otherwise end the
    request at once. The SDK runs middleware once per attempt, inside its retry loop, and
    retries a ``RetryableError`` as it does a dropped connection."""

    @override
    def handle(self, request: anthropic.APIRequest, call_next: anthropic.CallNext) -> anthropic.APIResponse[Any]:
        try:
            return call_next(request)
        except botocore.exceptions.BotoCoreError as exc:
            raise _credential_error(exc) from exc

    @override
    async def handle_async(
        self, request: anthropic.APIRequest, call_next: anthropic.AsyncCallNext
    ) -> anthropic.AsyncAPIResponse[Any]:
        try:
            return await call_next(request)
        except botocore.exceptions.BotoCoreError as exc:
            raise _credential_error(exc) from exc


class ChatAnthropicBedrock(ChatAnthropic):
    """``ChatAnthropic`` whose requests go to Bedrock Mantle in ``aws_region``.

    ``model`` holds the logical id; only the request payload carries the Bedrock id."""

    aws_region: str
    max_request_bytes: int = Field(default_factory=max_request_bytes)
    """The serialized request size above which inline documents are left out."""

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
            "middleware": [CredentialErrorMiddleware()],
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
        return anthropic.AnthropicBedrockMantle(
            **self._mantle_params(),
            http_client=_get_default_httpx_client(**self._http_client_params()),
        )

    @cached_property
    @override
    def _async_client(self) -> anthropic.AsyncAnthropicBedrockMantle:  # pyright: ignore[reportIncompatibleVariableOverride]
        return anthropic.AsyncAnthropicBedrockMantle(
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
        return fit_request_to_budget(payload, self.max_request_bytes)

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
            _normalize_model_names(chunk.response_metadata)
        return chunk, block_start

    @override
    def _format_output(self, data: Any, **kwargs: Any) -> ChatResult:
        result = super()._format_output(data, **kwargs)
        _normalize_model_names(result.llm_output)
        for generation in result.generations:
            _normalize_model_names(generation.message.response_metadata)
        return result
