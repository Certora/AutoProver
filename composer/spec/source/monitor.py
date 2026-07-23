from typing import TYPE_CHECKING

from langgraph.config import get_config
from langchain_core.messages import HumanMessage
from graphcore.graph import MonitorReturn
from composer.prover.ptypes import StatusCodes

if TYPE_CHECKING:
    # Runtime import would be circular: author.py imports this module.
    from .author import SourceCVLGenerationState

def monitor(
    curr_state: "SourceCVLGenerationState"
) -> MonitorReturn:
    if not curr_state["reminders_channel"]:
        return None, None
    
    return [HumanMessage(f"<system-reminder>{'\n'.join(curr_state["reminders_channel"])}</system-reminder>")], {
        "reminders_channel": None
    }
