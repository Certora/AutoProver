"""`composer.llm.structured.with_structured_output` asks Anthropic for structured outputs
(`output_config.format`), never a forced tool call: the Claude 5.5 family 400s on
`tool_choice: "tool"`/`"any"`. Checked on the request payload, no network."""

import re
from pathlib import Path

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableBinding, RunnableSequence
from pydantic import BaseModel

import composer
from composer.llm.structured import with_structured_output


class _Answer(BaseModel):
    value: int


def _payload(llm: ChatAnthropic) -> dict:
    bound = with_structured_output(llm, _Answer)
    assert isinstance(bound, RunnableSequence)
    binding = bound.first
    assert isinstance(binding, RunnableBinding)
    return llm._get_request_payload([HumanMessage("hi")], **binding.kwargs)


@pytest.mark.parametrize("thinking", [None, {"type": "adaptive"}])
@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"])
def test_structured_output_uses_output_config_not_forced_tool_choice(model, thinking):
    llm = ChatAnthropic(model_name=model, api_key="unused", thinking=thinking)  # type: ignore[call-arg]

    payload = _payload(llm)

    assert "tool_choice" not in payload
    assert "tools" not in payload
    fmt = payload["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["value"]["type"] == "integer"


def test_no_caller_binds_a_schema_except_through_the_helper():
    """A direct ``llm.with_structured_output(schema)`` defaults to a forced tool call, which the
    Claude 5.5 family rejects with a 400; property hoisting shipped one and died on every
    comprehensive multi-unit run. Fakes in ``composer/testing`` define the method, not call it."""
    root = Path(composer.__file__).parent
    helper = root / "llm" / "structured.py"
    callers = [
        f"{path.relative_to(root)}:{n}"
        for path in root.rglob("*.py")
        if path != helper
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _DIRECT_CALL.search(line) and not line.lstrip().startswith("#")
    ]
    assert callers == []


_DIRECT_CALL = re.compile(r"\.with_structured_output\(")
