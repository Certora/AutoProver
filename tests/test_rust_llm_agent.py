"""The tool-enabled authoring turn's prompt handling (no wheel / LLM needed).

The decider owns the prompt: its ``call_llm`` payload carries the ``instruction`` and
may define its own ``system`` prompt; otherwise a neutral, backend-agnostic default
applies (no language/domain leak — the trigger for this was the old prompt hardcoding
"Rust-based").
"""

from composer.rustapp._llm_agent import _DEFAULT_SYS_PROMPT, _split_prompt


def test_bare_string_is_the_instruction_with_default_system():
    assert _split_prompt("do the thing") == (None, "do the thing")


def test_dict_instruction_extracted_cleanly():
    # Not JSON-wrapped (the old behavior dumped the whole dict as the prompt).
    assert _split_prompt({"instruction": "author X"}) == (None, "author X")


def test_backend_may_define_its_own_system_prompt():
    assert _split_prompt({"system": "you are a fuzz author", "instruction": "author X"}) == (
        "you are a fuzz author",
        "author X",
    )


def test_default_system_prompt_is_backend_agnostic():
    # No language/domain leak; still conveys the result-tool contract.
    assert "Rust" not in _DEFAULT_SYS_PROMPT
    assert "result" in _DEFAULT_SYS_PROMPT
