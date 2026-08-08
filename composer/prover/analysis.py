from typing import Sequence

from langchain_core.messages import ToolMessage, HumanMessage, AIMessage, BaseMessage, AnyMessage
from langchain_core.runnables import Runnable
from langchain_core.language_models.base import LanguageModelInput

from graphcore.utils import ainvoke

from composer.prover.ptypes import RuleResult
from composer.templates.loader import load_jinja_template

# Stand-in result for a tool the model called alongside ``verify_spec``. That call's real
# result belongs to the agent's own thread; this one-shot analysis only needs it present,
# because the API rejects a ``tool_use`` block with no ``tool_result`` after it.
_SIBLING_TOOL_PLACEHOLDER = (
    "This tool ran alongside the prover invocation. Its result is not part of the "
    "counter-example analysis; disregard this call."
)


def _unanswered_tool_call_ids(messages: Sequence[AnyMessage]) -> list[str]:
    """Ids of tool calls in the last assistant turn that no ``ToolMessage`` answers.

    The history handed to :func:`analyze_cex_raw` is the agent's live state, snapshotted
    while the tools node is still executing: the trailing ``AIMessage`` opened every tool
    call of that node, and none of them has a result appended yet. The model is free to
    batch tool calls, so ``verify_spec`` is not necessarily the only one.
    """
    answered = {msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)}
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return [
                call_id
                for call in msg.tool_calls
                if (call_id := call.get("id")) is not None and call_id not in answered
            ]
    return []


async def analyze_cex_raw(
        llm: Runnable[LanguageModelInput, BaseMessage],
        m: list[AnyMessage],
        rule: RuleResult,
        tool_call_id: str,
) -> str | None:
    if rule.status != "VIOLATED":
        return None

    new_messages = m.copy()

    new_messages.append(
        ToolMessage(
            tool_call_id=tool_call_id,
            content=f"""
The Certora Prover found a violation for the rule {rule.name}, with the following counter example:
{rule.cex_dump}
"""
        )
    )
    new_messages.extend(
        ToolMessage(tool_call_id=sibling_id, content=_SIBLING_TOOL_PLACEHOLDER)
        for sibling_id in _unanswered_tool_call_ids(m)
        if sibling_id != tool_call_id
    )
    new_messages.append(
        HumanMessage(
            content=load_jinja_template("cex_instructions.j2", rule_name=rule.name)
        )
    )

    res = await ainvoke(llm, new_messages)
    if not isinstance(res, AIMessage):
        return None
    return res.text
