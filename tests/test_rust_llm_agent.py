"""The tool-enabled authoring turn's prompt handling (no wheel / LLM needed).

The backend owns the prompt: its author/judge prompt payload carries the ``instruction`` and may
define its own ``system`` prompt; otherwise a neutral, backend-agnostic default applies — one that
names no language or domain, since the host serves wheels for any of them.

The payload is a :class:`composer.rustapp.wire.Prompt`, parsed at the seam, so the shape is checked
where it crosses rather than read key-by-key later: a wheel that sends no ``instruction`` fails here
with the field named, rather than prompting the agent with a JSON dump of whatever it did send.
"""

import json
import pytest
from pydantic import ValidationError

from composer.rustapp.adapter import _DEFAULT_SYS_PROMPT
from composer.rustapp.wire import Prompt, parse_prompt
from tests.conftest import wire_prompt


def test_instruction_is_taken_as_sent():
    assert parse_prompt(json.dumps(wire_prompt("author X"))).instruction == "author X"


def test_no_system_prompt_declared_means_the_host_default_applies():
    # ``None`` is the signal the turn falls back to _DEFAULT_SYS_PROMPT.
    assert parse_prompt(json.dumps(wire_prompt("author X"))).system is None


def test_backend_may_define_its_own_system_prompt():
    prompt = parse_prompt(json.dumps(wire_prompt("author X", "you are a fuzz author")))
    assert prompt == Prompt(system="you are a fuzz author", instruction="author X")


def test_a_payload_with_no_instruction_is_rejected_at_the_seam():
    with pytest.raises(ValidationError, match="instruction"):
        parse_prompt('{"system": "you are a fuzz author"}')


def test_default_system_prompt_is_backend_agnostic():
    # No language/domain leak; still conveys the result-tool contract.
    assert "Rust" not in _DEFAULT_SYS_PROMPT
    assert "result" in _DEFAULT_SYS_PROMPT
