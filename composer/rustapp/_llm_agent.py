"""The tool-enabled agent turn behind the Rust backend's ``call_llm`` effect.

Note: ``bind_standard`` introspects the state class's ``__annotations__`` at runtime to
unwrap ``result: NotRequired[T]``, so the annotations here must stay real objects, not
strings — one concrete reason the repo bans ``from __future__ import annotations`` (see
``CLAUDE.md``): stringized annotations would break that introspection.
"""

import json
from typing import Any, NotRequired

from graphcore.graph import FlowInput
from langgraph.graph import MessagesState

from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.util import uniq_thread_id

# The decider owns the prompt. Its `messages` payload carries the task-specific
# `instruction` and MAY carry its own `system` prompt; when it doesn't, this neutral,
# backend-agnostic fallback applies. It conveys only the tool-using-agent + result-tool
# contract — no domain or language specifics (those belong in the decider's prompt).
_DEFAULT_SYS_PROMPT = (
    "You are an authoring agent. Use the available tools to explore the target "
    "program's source and any reference material, then produce the requested artifact. "
    "When done, call the `result` tool with your complete final answer as a single "
    "string — the artifact source only, with no surrounding prose or code fences."
)


def _split_prompt(messages: Any) -> tuple[str | None, str]:
    """Split the decider's ``call_llm`` payload into ``(system, instruction)``.

    The payload is a bare instruction string, or a dict carrying ``instruction`` and
    (optionally) a backend-defined ``system`` prompt. ``system`` is ``None`` when the
    backend doesn't supply one (the caller falls back to :data:`_DEFAULT_SYS_PROMPT`)."""
    if isinstance(messages, dict):
        return messages.get("system"), messages.get("instruction") or json.dumps(messages)
    return None, messages


class _LlmState(MessagesState):
    result: NotRequired[str]


class _LlmInput(FlowInput):
    pass


async def run_llm_agent(
    env: Any, messages: Any, *, recursion_limit: int, backend_name: str = "rust"
) -> str:
    """Run one bounded, tool-enabled authoring turn and return its final text.

    Binds the env's tool belt (source navigation + RAG search over the backend's
    knowledge base) and a result tool, and runs an agent to completion — so the
    decider's prompt can pull in framework docs / read the program. Must run inside
    a ``with_handler`` scope (the caller wraps it in ``run.runner``)."""
    tools = list(getattr(env, "all_tools", None) or env.rag_tools)
    system, instruction = _split_prompt(messages)
    graph = (
        bind_standard(
            env.builder_heavy(),
            _LlmState,
            doc="Your complete final answer as a single string (e.g. the authored source file).",
        )
        .with_input(_LlmInput)
        .with_sys_prompt(system or _DEFAULT_SYS_PROMPT)
        .with_initial_prompt(instruction)
        .with_tools(tools)
        .compile_async()
    )
    res = await run_to_completion(
        graph,
        _LlmInput(input=[]),
        thread_id=uniq_thread_id(f"{backend_name}-llm"),
        recursion_limit=recursion_limit,
        description=f"{backend_name} authoring turn",
    )
    result = res.get("result")
    return result if isinstance(result, str) else json.dumps(result)
