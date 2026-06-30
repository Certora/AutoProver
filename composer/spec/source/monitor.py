from langgraph.config import get_config
from langchain_core.messages import HumanMessage
from .author import SourceCVLGenerationState
from graphcore.graph import MonitorReturn
from composer.prover.ptypes import StatusCodes

def monitor(
    curr_state: SourceCVLGenerationState
) -> MonitorReturn:
    if not curr_state["reminders_channel"]:
        return None, None
    
    return [HumanMessage(f"<system-reminder>{'\n'.join(curr_state["reminders_channel"])}</system-reminder>")], {
        "reminders_channel": None
    }
