import uuid
from typing import Any, Dict, AsyncIterator

from core.config import settings
from core.enums import WorkflowStatus
from core.state import AgentState
import os
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.memory import MemorySaver
from graph.workflow import workflow

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ai_user:ai_password@localhost:5432/platform_db")


def create_initial_state(user_request: str, max_retries: int | None = None) -> AgentState:
    return {
        "task_id": str(uuid.uuid4()),
        "user_request": user_request,
        "current_plan": [],
        "active_step_id": None,
        "human_query": None,
        "human_response": None,
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


async def run_workflow(user_request: str, max_retries: int | None = None) -> AgentState:
    initial_state = create_initial_state(user_request, max_retries=max_retries)
    config = {"configurable": {"thread_id": initial_state["task_id"]}}
    
    try:
        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            app = workflow.compile(checkpointer=checkpointer)
            async for _ in app.astream(initial_state, config=config):
                pass
            full_state = await app.aget_state(config)
            return full_state.values
    except Exception as e:
        print(f"Warning: Postgres connection failed ({e}). Falling back to MemorySaver.")
        checkpointer = MemorySaver()
        app = workflow.compile(checkpointer=checkpointer)
        async for _ in app.astream(initial_state, config=config):
            pass
        full_state = app.get_state(config)
        return full_state.values


async def stream_workflow(user_request: str, max_retries: int | None = None, input_queue: Any = None) -> AsyncIterator[Dict[str, Any]]:
    state = create_initial_state(user_request, max_retries=max_retries)
    config = {"configurable": {"thread_id": state["task_id"], "input_queue": input_queue}}
    yield {"node": "initialized", "state": state}
    try:
        async with AsyncPostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            app = workflow.compile(checkpointer=checkpointer)
            async for update in app.astream(state, config=config):
                for node_name in update.keys():
                    full_state = await app.aget_state(config)
                    yield {"node": node_name, "state": full_state.values}
    except Exception as e:
        print(f"Warning: Postgres connection failed ({e}). Falling back to MemorySaver for stream.")
        checkpointer = MemorySaver()
        app = workflow.compile(checkpointer=checkpointer)
        async for update in app.astream(state, config=config):
            for node_name in update.keys():
                full_state = app.get_state(config)
                yield {"node": node_name, "state": full_state.values}
