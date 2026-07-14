
from typing import override, Literal, Sequence, Protocol
import re
from difflib import SequenceMatcher
from pydantic import Field

from langgraph.types import interrupt, Command

from langchain_core.messages import AnyMessage, SystemMessage, AIMessage, ToolMessage

from graphcore.tools.schemas import WithImplementation

from composer.spec.types import PropertyFormulation
from composer.spec.service_host import ServiceHost
from composer.io.conversation import ConversationContextProvider
from composer.spec.refinement import refinement_loop, EndConversation, SyncStateUpdateTool
from composer.templates.loader import load_jinja_template
from composer.ui.tool_display import tool_display

from rich.markdown import Markdown
from rich.console import Group
from rich.text import Text

class AgenticAttempt(Protocol):
    @property
    def final_history(self) -> Sequence[AnyMessage]:
        ...

    @property
    def items(self) -> list[PropertyFormulation]:
        ...


@tool_display("Ending conversation...", None)
class Exit(WithImplementation[str]):
    """
    Call this when the user has indicated they are happy with the properties you have generated
    """
    @override
    def run(self) -> str:
        return interrupt(EndConversation())

@tool_display("Updating requirements", None)
class SetRequirements(SyncStateUpdateTool[list[PropertyFormulation]]):
    """
    Called with the new properties as requested by the user
    """

    new_requirements: list[PropertyFormulation] = Field(description="The new requirements after taking into account user feedback.")

    @override
    def run(self) -> Command:
        return self._update(self.new_requirements)


# GitHub-ish dark theme
LINE_DEL = "red on #3a1d1d"
LINE_ADD = "green on #1d3a1d"
WORD_DEL = "bold white on #802020"
WORD_ADD = "bold white on #206020"
DIM      = "grey50"


def _word_diff(a: str, b: str) -> tuple[Text, Text]:
    """Return (minus, plus) Text with word-level highlights."""
    a_toks = re.findall(r"\S+|\s+", a)
    b_toks = re.findall(r"\S+|\s+", b)
    sm = SequenceMatcher(None, a_toks, b_toks)

    minus, plus = Text(), Text()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_chunk = "".join(a_toks[i1:i2])
        b_chunk = "".join(b_toks[j1:j2])
        if tag == "equal":
            minus.append(a_chunk, style=LINE_DEL)
            plus.append(b_chunk, style=LINE_ADD)
        elif tag == "delete":
            minus.append(a_chunk, style=WORD_DEL)
        elif tag == "insert":
            plus.append(b_chunk, style=WORD_ADD)
        elif tag == "replace":
            minus.append(a_chunk, style=WORD_DEL)
            plus.append(b_chunk, style=WORD_ADD)
    return minus, plus


def _diff_replace_block(a_block: list[str], b_block: list[str]) -> list[Text]:
    """Nested line-level diff inside an outer 'replace' block.

    Only inner 'replace' pairs get word-diffed; inner insert/delete become
    whole-line adds/removes. This avoids noisy word-diffs when lines shift.
    """
    out: list[Text] = []
    sm = SequenceMatcher(None, a_block, b_block)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in a_block[i1:i2]:
                out.append(Text(f"  {line}", style=DIM))
        elif tag == "delete":
            for line in a_block[i1:i2]:
                out.append(Text(f"- {line}", style=LINE_DEL))
        elif tag == "insert":
            for line in b_block[j1:j2]:
                out.append(Text(f"+ {line}", style=LINE_ADD))
        elif tag == "replace":
            a_lines, b_lines = a_block[i1:i2], b_block[j1:j2]
            n = min(len(a_lines), len(b_lines))
            for k in range(n):
                m, p = _word_diff(a_lines[k], b_lines[k])
                out.append(Text("- ", style=LINE_DEL) + m)
                out.append(Text("+ ", style=LINE_ADD) + p)
            for line in a_lines[n:]:
                out.append(Text(f"- {line}", style=LINE_DEL))
            for line in b_lines[n:]:
                out.append(Text(f"+ {line}", style=LINE_ADD))
    return out


def diff_states(state_a: list[str], state_b: list[str]) -> Group:
    sm = SequenceMatcher(None, state_a, state_b)
    out: list[Text] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in state_a[i1:i2]:
                out.append(Text(f"  {line}", style=DIM))
        elif tag == "delete":
            for line in state_a[i1:i2]:
                out.append(Text(f"- {line}", style=LINE_DEL))
        elif tag == "insert":
            for line in state_b[j1:j2]:
                out.append(Text(f"+ {line}", style=LINE_ADD))
        elif tag == "replace":
            out.extend(_diff_replace_block(state_a[i1:i2], state_b[j1:j2]))

    return Group(*out)

def sort_to_string(
    s: Literal["attack_vector", "invariant", "safety_property"]
) -> str:
    match s:
        case "attack_vector":
            return "Attack Vector"
        case "invariant":
            return "Invariant"
        case "safety_property":
            return "Safety Property"

def property_as_text(
    prop: PropertyFormulation
) -> str:
    return f"* [{sort_to_string(prop.sort)}] {prop.description}"

def property_as_md(
    prop: PropertyFormulation
) -> str:
    sort_str = sort_to_string(prop.sort)
    return f"* \\[{sort_str}\\] {prop.description}"

def properties_as_text(
    l: list[PropertyFormulation]
) -> list[str]:
    return [ property_as_text(p) for p in l ]

def properties_as_md(
    l: list[PropertyFormulation]
) -> list[str]:
    return [ property_as_md(p) for p in l ]

def render_properties_as_md(
    l: list[PropertyFormulation]
) -> Markdown:
    md = "## Current Properties\n"
    return Markdown(md + "\n".join(properties_as_md(l)))


async def user_property_refinement(
    env: ServiceHost,
    agent_attempt: AgenticAttempt,
    refinement: ConversationContextProvider
) -> list[PropertyFormulation]:
    msg_history = agent_attempt.final_history
    assert isinstance(msg_history[0], SystemMessage) and isinstance(msg_history[-1], ToolMessage)
    import uuid
    edited_history = [
        SystemMessage(load_jinja_template("bug_refinement_chat_system_prompt.j2", sort=env.sort)),
        *msg_history[1:],
        AIMessage("<task-complete>", id=uuid.uuid4().hex)
    ]

    async with refinement(render_properties_as_md(agent_attempt.items)) as client:
        res = await refinement_loop(
            llm=env.llm_heavy(),
            client=client,
            init_messages=edited_history,
            init_data=agent_attempt.items,
            tools=[*env.analysis_tools, Exit.as_tool("finalize_properties"), SetRequirements.as_tool("update_requirements")],
            state_renderer=render_properties_as_md,
            diff_renderer=lambda a, b: \
                Group(
                    Text("Properties changed"),
                    diff_states(properties_as_text(a), properties_as_text(b))
                )
        )
    to_ret = res["extra_data"]
    return to_ret
