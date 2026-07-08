"""The tool-enabled agent turn behind the Rust backend's ``call_llm`` effect.

Deliberately **without** ``from __future__ import annotations``: ``bind_standard``
introspects the state class's ``__annotations__`` to unwrap ``result: NotRequired[T]``,
and a stringized annotation (which the future-import would produce) can't be
unwrapped — pydantic then chokes on the raw ``NotRequired[str]``. Keeping this in
its own eager-annotations module sidesteps that.
"""

import json
from typing import Any, NotRequired

from graphcore.graph import FlowInput
from langgraph.graph import MessagesState

from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.util import uniq_thread_id

# The decider owns the task-specific prompt (passed as `messages`); this is just
# the neutral framing that makes it a tool-using authoring agent.
_SYS_PROMPT = (
    "You are an authoring agent for a Rust-based AutoProver backend. Use the "
    "available tools to explore the target program's source and any reference "
    "material, then produce the requested artifact. When done, call the result "
    "tool with your complete final answer as a single string — the artifact "
    "source only, with no surrounding prose or code fences."
)


class _LlmState(MessagesState):
    result: NotRequired[str]


class _LlmInput(FlowInput):
    pass


async def run_llm_agent(env: Any, messages: Any, *, recursion_limit: int) -> str:
    """Run one bounded, tool-enabled authoring turn and return its final text.

    Binds the env's tool belt (source navigation + RAG search over the backend's
    knowledge base) and a result tool, and runs an agent to completion — so the
    decider's prompt can pull in framework docs / read the program. Must run inside
    a ``with_handler`` scope (the caller wraps it in ``run.runner``)."""
    tools = list(getattr(env, "all_tools", None) or env.rag_tools)
    content = messages if isinstance(messages, str) else json.dumps(messages)
    graph = (
        bind_standard(
            env.builder_heavy(),
            _LlmState,
            doc="Your complete final answer as a single string (e.g. the authored source file).",
        )
        .with_input(_LlmInput)
        .with_sys_prompt(_SYS_PROMPT)
        .with_initial_prompt(content)
        .with_tools(tools)
        .compile_async()
    )
    res = await run_to_completion(
        graph,
        _LlmInput(input=[]),
        thread_id=uniq_thread_id("rust-llm"),
        recursion_limit=recursion_limit,
        description="Rust backend authoring turn",
    )
    result = res.get("result")
    return result if isinstance(result, str) else json.dumps(result)
