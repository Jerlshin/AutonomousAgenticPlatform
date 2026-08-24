"""`init` — the deterministic graph entry point (AGENTS.md §7.9).

Seeds the identity fields, starts the usage clock, resolves the budget envelope and
records which model each role was routed to. Everything downstream assumes these exist,
which is why the failure policy is `FAIL_RUN`: a run with no identity has nowhere to
write its results and no way to be resumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.engine.llm import model_routing_snapshot
from app.engine.nodes.base import FailurePolicy, node
from app.engine.state import AgentState, Budgets, RunPhase, Usage

DEFAULT_SEED = 42


def default_budgets() -> Budgets:
    """The configured budget envelope (ARCHITECTURE.md §14.5)."""
    return Budgets(
        max_debug_iterations=settings.MAX_DEBUG_ITERATIONS,
        max_replans=settings.MAX_REPLANS,
        max_node_visits=settings.MAX_NODE_VISITS,
        max_sandbox_executions=settings.MAX_SANDBOX_EXECUTIONS,
        wallclock_seconds=settings.RUN_WALLCLOCK_SECONDS,
        max_tokens=settings.RUN_MAX_TOKENS,
    )


@node(name="init", phase=RunPhase.INIT, policy=FailurePolicy.FAIL_RUN)
async def init_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    configurable = (config or {}).get("configurable") or {}

    # thread_id is the checkpointer's key and the run's identity everywhere else; the two
    # being the same string is what makes `ainvoke(None, config)` resume a crashed run.
    thread_id = configurable.get("thread_id")
    run_id = state.get("run_id") or thread_id or str(uuid.uuid4())

    budgets = state.get("budgets") or configurable.get("budgets") or default_budgets()
    metadata = dict(state.get("metadata") or {})
    metadata.setdefault("seed", int(configurable.get("seed", DEFAULT_SEED)))
    metadata.setdefault("started_at", datetime.now(UTC).isoformat())

    return {
        "run_id": str(run_id),
        "task_id": str(state.get("task_id") or configurable.get("task_id") or run_id),
        "thread_id": str(thread_id or run_id),
        "prompt": state.get("prompt") or configurable.get("prompt") or "",
        "task_kind": state.get("task_kind") or "",
        "budgets": budgets,
        "usage": Usage(started_at=datetime.now(UTC)),
        "debug_iterations": 0,
        "replan_count": 0,
        "cancel_requested": bool(state.get("cancel_requested", False)),
        "hitl_gates": list(
            state.get("hitl_gates") or configurable.get("hitl_gates") or []
        ),
        "pending_gate": None,
        "model_routing": model_routing_snapshot(),
        "metadata": metadata,
    }
