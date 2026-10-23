"""Structured (schema-shaped) output from a chat model.

``with_structured_output`` defaults to ``method="function_calling"``, which binds the
schema as a tool and forces the call (``tool_choice: "tool"``) whenever thinking is off.
The Claude 5.5 family (Opus 5.5, Sonnet 5.5, Fable 5.1) rejects forced tool choice with a
400, so every structured call goes through ``method="json_schema"`` instead: Anthropic
structured outputs (``output_config.format``), which every current Claude model accepts
with or without thinking. On ``ChatOpenAI`` (OpenAI, OpenRouter) ``json_schema`` is
already the default.

A reply that does not validate against the schema raises ``OutputParserException``.
"""

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.runnables import Runnable
from pydantic import BaseModel


def with_structured_output(
    llm: BaseChatModel, schema: type[BaseModel]
) -> Runnable[LanguageModelInput, dict | BaseModel]:
    return llm.with_structured_output(schema, method="json_schema")
