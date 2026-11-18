from langchain_core.messages import ToolMessage, HumanMessage, AIMessage, BaseMessage, AnyMessage
from langchain_core.runnables import Runnable
from langchain_core.language_models.base import LanguageModelInput

from graphcore.utils import ainvoke

from composer.prover.ptypes import RuleResult
from composer.templates.loader import load_jinja_template


async def analyze_cex_raw(
        llm: Runnable[LanguageModelInput, BaseMessage],
        m: list[AnyMessage],
        rule: RuleResult,
        tool_call_id: str,
) -> str | None:
    if rule.status != "VIOLATED":
        return None

    new_messages = m.copy()

    # The API rejects a request in which any tool call of the last turn has no result. The prover's
    # call is answered below; one the author made beside it (a ``memory`` write, say) is still
    # unanswered in the history this request is built from, so it can only say so.
    new_messages.extend(
        ToolMessage(
            tool_call_id=sibling,
            content="(This call's result is not part of this request.)",
        )
        for sibling in _unanswered_calls(m)
        if sibling != tool_call_id
    )
    new_messages.append(
        ToolMessage(
            tool_call_id=tool_call_id,
            content=f"""
The Certora Prover found a violation for the rule {rule.name}, with the following counter example:
{rule.cex_dump}
"""
        )
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


def _unanswered_calls(m: list[AnyMessage]) -> list[str]:
    """The ids of the last assistant turn's tool calls that no later message answers."""
    for i in range(len(m) - 1, -1, -1):
        turn = m[i]
        if isinstance(turn, AIMessage):
            answered = {r.tool_call_id for r in m[i + 1:] if isinstance(r, ToolMessage)}
            return [c["id"] for c in turn.tool_calls if c["id"] is not None and c["id"] not in answered]
    return []
