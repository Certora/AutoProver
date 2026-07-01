from dataclasses import dataclass
from typing import NotRequired
from pydantic import BaseModel, Field

from langgraph.graph import MessagesState

from graphcore.tools.vfs import VFSState, VFSAccessor
from graphcore.tools.schemas import WithAsyncDependencies

class MungerStateExtra(VFSState):
    ...

class MungeDescription(
    BaseModel
):
    """A holistic description of your changes"""

    executive_summary: str = Field(description="An executive summary of your changes, fully covering each part of the diff with the existing source code")

    how_to_apply: str | None = Field(description="What changes might need to be made by the upstream verification author to make effective use of your changes")

    why_sound: str = Field(description="A precise, reasoned argument why your changes are *either* sound, OR an acceptable over-approximation")


class MungerAgent(MungerStateExtra):
    result: NotRequired[MungeDescription]

