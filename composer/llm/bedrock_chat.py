"""``ChatAnthropic`` pointed at Claude on Amazon Bedrock through the Mantle endpoint.

Mantle serves the Anthropic Messages API itself, so everything ``ChatAnthropic`` does to
build requests and parse responses applies unchanged. This subclass changes three things:

- the SDK clients are the Bedrock Mantle ones, which sign requests with SigV4 from the
  default AWS credential chain and need no Anthropic API key;
- the request carries the Bedrock model id, mapped from the logical one;
- the model name a response reports is mapped back to the logical id, so usage records,
  pricing and logs see the same name the run was configured with.
"""

from functools import cached_property
from typing import Any, override

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_anthropic._client_utils import (
    _get_default_async_httpx_client, _get_default_httpx_client,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatResult

from composer.llm.bedrock import to_bedrock_model_id, to_logical_model_id

# Response fields that name the model: ``model`` as Anthropic returns it, and
# ``model_name``, which langchain copies it into.
_MODEL_NAME_KEYS = ("model", "model_name")


def _normalize_model_names(metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    for key in _MODEL_NAME_KEYS:
        if isinstance(name := metadata.get(key), str):
            metadata[key] = to_logical_model_id(name)


class ChatAnthropicBedrock(ChatAnthropic):
    """``ChatAnthropic`` whose requests go to Bedrock Mantle in ``aws_region``.

    ``model`` holds the logical id; only the request payload carries the Bedrock id."""

    aws_region: str

    def _mantle_params(self) -> dict[str, Any]:
        """The parent's client settings (timeout, retries, default headers) minus the
        Anthropic API key and base URL, which belong to the first-party endpoint. With no
        key given, the Mantle client signs with SigV4."""
        params = self._client_params
        mantle: dict[str, Any] = {
            "aws_region": self.aws_region,
            "max_retries": params["max_retries"],
            "default_headers": params["default_headers"],
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
        return payload

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
