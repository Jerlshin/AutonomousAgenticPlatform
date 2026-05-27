import uuid
from typing import Any, Dict, Iterator

from core.config import settings
from core.enums import WorkflowStatus
from core.state import AgentState
from graph.workflow import app


def create_initial_state(user_request: str, max_retries: int | None = None) -> AgentState:
    return {
        "task_id": str(uuid.uuid4()),
        "user_request": user_request,
        "current_plan": [],
        "active_step_id": None,
        "generated_artifacts": [],
        "latest_execution": None,
        "execution_logs": [],
        "retrieved_context": [],
        "reflection_notes": [],
        "retry_count": 0,
        "iterations": 0,
        "status": WorkflowStatus.INITIALIZED,
        "events": [],
        "metrics": {},
        "evaluation": None,
        "experiment_records": [],
        "errors": [],
        "max_retries": max_retries if max_retries is not None else settings.max_retries,
        "metadata": {},
    }


def run_workflow(user_request: str, max_retries: int | None = None) -> AgentState:
    final_state = create_initial_state(user_request, max_retries=max_retries)
    for update in app.stream(final_state):
        for _, node_state in update.items():
            final_state.update(node_state)
    return final_state


def stream_workflow(user_request: str, max_retries: int | None = None) -> Iterator[Dict[str, Any]]:
    state = create_initial_state(user_request, max_retries=max_retries)
    yield {"node": "initialized", "state": state}
    for update in app.stream(state):
        for node_name, node_state in update.items():
            state.update(node_state)
            yield {"node": node_name, "state": state}
