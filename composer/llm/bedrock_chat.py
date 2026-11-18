"""langchain-aws's ``ChatAnthropicBedrock``, which sends ``ChatAnthropic`` requests to
Claude on Amazon Bedrock through InvokeModel, with what a run needs on top.

InvokeModel takes the Anthropic Messages request body, so everything ``ChatAnthropic`` does to
build requests and parse responses applies unchanged, and the langchain-aws class supplies
the SDK's Bedrock clients, AWS credentials and the usage Bedrock reports in a stream. This
subclass changes six things:

- the request carries the inference profile id, mapped from the logical model id;
- inline documents are dropped, latest first, until the request fits the request budget,
  and a request that refers to a Files API upload fails before it is sent (see
  :func:`composer.llm.bedrock_inline.fit_request_to_budget`);
- the model name a response reports is replaced by the chat model's own logical id, so usage
  records, pricing and logs see the same name the run was configured with;
- a failure to fetch the AWS credentials that sign a request is raised as an error that says
  whether retrying can help (see :class:`CredentialErrorMiddleware`);
- an exception Bedrock sends inside an open stream is raised as a
  :class:`composer.llm.bedrock.BedrockStreamError` that names it;
- the SDK's retries back off for longer (see :func:`composer.llm.bedrock.retry_delay_seconds`),
  and a request Bedrock rejects as too large is sent again with fewer inline documents.
"""

import asyncio
import json
import logging
import random
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Self, override

import anthropic
import boto3
import httpx
import botocore.exceptions
from anthropic._models import FinalRequestOptions
from anthropic._streaming import ServerSentEvent
from anthropic.lib.bedrock._stream_decoder import AWSEventStreamDecoder
from botocore.eventstream import EventStreamMessage
from langchain_aws import ChatAnthropicBedrock as _LangchainChatAnthropicBedrock
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatResult
from langchain_core.runnables.config import ensure_config
from pydantic import PrivateAttr, model_validator

from composer.llm.bedrock import (
    TRANSIENT_CREDENTIAL_ERROR_CODES, BedrockCredentialRefreshError, BedrockCredentialsError,
    BedrockStreamError, MAX_REQUEST_BYTES, retry_delay_seconds, to_bedrock_model_id,
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


def _no_credentials_error(
    exc: RuntimeError, aws_region: str | None
) -> anthropic.AnthropicError | None:
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

    The Bedrock clients sign each attempt before sending it, and the SDK leaves an error from
    signing as it is, so a passing failure to fetch credentials would otherwise end the
    request at once. The SDK runs middleware once per attempt, inside its retry loop, and
    retries a ``RetryableError`` as it does a dropped connection."""

    def __init__(self, aws_region: str | None) -> None:
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
    client: anthropic.AnthropicBedrock | anthropic.AsyncAnthropicBedrock,
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


def _stream_exception(event: EventStreamMessage) -> BedrockStreamError:
    """The error for a frame of the AWS event stream that reports a failure. A modeled
    exception names itself in ``:exception-type`` and carries a JSON body with a
    ``message``; any other error names itself in ``:error-code`` and ``:error-message``."""
    headers = event.headers
    if headers.get(":message-type") == "exception":
        try:
            body = json.loads(event.payload)
        except ValueError:
            body = None
        message = body.get("message") if isinstance(body, dict) else None
        return BedrockStreamError(
            headers.get(":exception-type"),
            str(message or event.payload.decode(errors="replace")),
        )
    return BedrockStreamError(headers.get(":error-code"), str(headers.get(":error-message", "")))


class _EventStreamDecoder(AWSEventStreamDecoder):
    """The SDK's decoder of InvokeModel streams, raising a :class:`BedrockStreamError` for a
    frame that reports a failure. The SDK's own decoder raises a plain ``ValueError`` there,
    which loses the exception name that tells a throttled stream from a bad request."""

    @override
    def _parse_message_from_event(self, event: EventStreamMessage) -> ServerSentEvent | None:
        if event.headers.get(":message-type") in ("exception", "error"):
            raise _stream_exception(event)
        return super()._parse_message_from_event(event)


# The SDK's Bedrock clients raise a 413 as the base APIStatusError, unlike its first-party
# client; these raise RequestTooLargeError for it, which the chat model recovers from.
class _InvokeModelClient(anthropic.AnthropicBedrock):
    @override
    def _calculate_retry_timeout(
        self,
        remaining_retries: int,
        options: FinalRequestOptions,
        response_headers: httpx.Headers | None = None,
    ) -> float:
        return _bedrock_retry_timeout(self, remaining_retries, options, response_headers)

    @override
    def _make_sse_decoder(self) -> AWSEventStreamDecoder:
        return _EventStreamDecoder()

    @override
    def _make_status_error(
        self, err_msg: str, *, body: object, response: httpx.Response
    ) -> anthropic.APIStatusError:
        if response.status_code == 413:
            return anthropic.RequestTooLargeError(err_msg, response=response, body=body)
        return super()._make_status_error(err_msg, body=body, response=response)


class _AsyncInvokeModelClient(anthropic.AsyncAnthropicBedrock):
    @override
    def _calculate_retry_timeout(
        self,
        remaining_retries: int,
        options: FinalRequestOptions,
        response_headers: httpx.Headers | None = None,
    ) -> float:
        return _bedrock_retry_timeout(self, remaining_retries, options, response_headers)

    @override
    def _make_sse_decoder(self) -> AWSEventStreamDecoder:
        return _EventStreamDecoder()

    @override
    def _make_status_error(
        self, err_msg: str, *, body: object, response: httpx.Response
    ) -> anthropic.APIStatusError:
        if response.status_code == 413:
            return anthropic.RequestTooLargeError(err_msg, response=response, body=body)
        return super()._make_status_error(err_msg, body=body, response=response)


_logger = logging.getLogger(__name__)

# How many times a request Bedrock rejects as too large is sent again with a lower request
# budget. Each try leaves out more inline documents; a request that holds none is not retried.
_TOO_LARGE_RETRIES = 3


@dataclass
class _ConversationState:
    """What a chat model learns about one conversation from its requests. Every request of a
    conversation carries the whole history, so it applies to the later requests too."""

    omitted_documents: set[DocumentKey] = field(default_factory=set)
    """The inline documents already warned about, so a document left out of every request
    is warned about once."""
    lowered_request_bytes: int | None = None
    """The request budget after Bedrock rejected a request as too large; ``None`` until
    then."""


_MAX_CONVERSATIONS = 1024
"""How many conversations a chat model keeps state for. Past that, the one used least
recently is forgotten: a later request of it then warns again about a document it leaves
out, and learns its lowered budget again from another 413."""

# Guards every chat model's conversation table, which sync calls can reach from several
# threads at once.
_conversations_lock = threading.Lock()


class ChatAnthropicBedrock(_LangchainChatAnthropicBedrock):
    """langchain-aws's ``ChatAnthropicBedrock`` whose requests go to the
    ``inference_profile`` routing of the model in ``region_name``.

    ``model`` holds the logical id; only the request payload carries the Bedrock id."""

    inference_profile: str
    """The routing prefix of the inference profile, such as ``global`` or ``eu``."""
    max_request_bytes: int = MAX_REQUEST_BYTES
    """The serialized request size above which inline documents are left out."""

    # One model can serve several conversations at once, such as the parallel threads of a
    # graph built from one builder, so the state is kept per conversation: per LangGraph
    # thread id, read from the runnable config of the current call. Calls made outside a
    # graph have no thread id and share one state.
    # Bounded by _MAX_CONVERSATIONS, least recently used first out.
    _conversations: OrderedDict[str | None, _ConversationState] = PrivateAttr(
        default_factory=OrderedDict
    )

    def _conversation(self) -> _ConversationState:
        thread_id = ensure_config().get("configurable", {}).get("thread_id")
        key = None if thread_id is None else str(thread_id)
        with _conversations_lock:
            state = self._conversations.get(key)
            if state is None:
                state = self._conversations[key] = _ConversationState()
                if len(self._conversations) > _MAX_CONVERSATIONS:
                    self._conversations.popitem(last=False)
            else:
                self._conversations.move_to_end(key)
            return state

    @model_validator(mode="after")
    def _static_keys_come_in_pairs(self) -> Self:
        """langchain-aws reads ``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY`` and hands
        them to the SDK client, which signs with whichever is set. One without the other
        would sign every request with a missing key, so the model refuses to be built."""
        if (self.aws_access_key_id is None) != (self.aws_secret_access_key is None):
            raise ValueError(
                "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set together; unset both "
                "to sign Bedrock requests with the default AWS credential chain"
            )
        return self

    def _bedrock_client_params(self) -> dict[str, Any]:
        """The parent's client settings (region, AWS credentials, timeout, retries, default
        headers) plus :class:`CredentialErrorMiddleware`, which classifies a failure to fetch
        the credentials the client signs with."""
        params = self._client_params
        return {**params, "middleware": [CredentialErrorMiddleware(params.get("aws_region"))]}

    @cached_property
    @override
    def _client(self) -> anthropic.AnthropicBedrock:
        return _InvokeModelClient(**self._bedrock_client_params())

    @cached_property
    @override
    def _async_client(self) -> anthropic.AsyncAnthropicBedrock:
        return _AsyncInvokeModelClient(**self._bedrock_client_params())

    @override
    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: dict,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload["model"] = to_bedrock_model_id(payload["model"], self.inference_profile)
        conversation = self._conversation()
        return fit_request_to_budget(
            payload, self._request_budget(conversation), conversation.omitted_documents
        )

    def _request_budget(self, conversation: _ConversationState) -> int:
        return conversation.lowered_request_bytes or self.max_request_bytes

    def _refit_after_too_large(
        self, payload: dict, exc: anthropic.RequestTooLargeError
    ) -> dict | None:
        """``payload`` refitted to a lower request budget after Bedrock rejected it as too
        large, or ``None`` when it holds no inline document left to drop. The budget becomes
        three quarters of the smaller of the current budget and the rejected request's size,
        so at least one more document is left out. It stays lowered for the later requests
        of the same conversation, which carry the same history."""
        conversation = self._conversation()
        budget = min(
            self._request_budget(conversation), serialized_request_size(payload)
        ) * 3 // 4
        refitted = fit_request_to_budget(payload, budget, conversation.omitted_documents)
        if refitted is payload:
            return None
        _logger.warning(
            "Bedrock rejected a request as too large (%s); lowering the request budget for "
            "inline documents to %d bytes and sending it again", exc, budget,
        )
        conversation.lowered_request_bytes = budget
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
