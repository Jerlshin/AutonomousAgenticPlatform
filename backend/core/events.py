from core.models import AgentEvent
from core.enums import EventType

def create_event(
    event_type: EventType,
    source_agent: str,
    payload: dict,
) -> AgentEvent:
    return AgentEvent(
        event_type=event_type.value,
        source_agent=source_agent,
        payload=payload
    )