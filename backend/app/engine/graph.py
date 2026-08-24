"""Graph assembly (AGENTS.md §4).

The graph is cyclic, and the cycle is the point: `coder → sandbox_exec → debugger → coder`
is loop 1, the correctness loop (§6.1). A program that crashes is diagnosed and rewritten
rather than reported as a failure, which is the difference between a code generator and a
system that finishes the job.

```
START → init → planner ─┬─> coder ─┬─> sandbox_exec ─┬─> debugger ─┬─> coder  (loop 1)
                  ↑     │          │                 │             ├─> planner (replan)
                  │     │          └─> reporter      └─> reporter  └─> reporter
                  └─────┴──────────────────────────────────────────┘
                                     reporter → finalizer → END
```

Three things bound the cycle, and all three are counters read by pure routers rather than
judgements made by a model: `max_debug_iterations` (how many times the Debugger may try),
`max_sandbox_executions` (how many containers a run may spend), and the stagnation rule —
three consecutive failures with one error fingerprint escalate to the Planner, because at
that point the *approach* is what is wrong, not the code.

`reporter` is `finalizer`'s sole predecessor and `finalizer` is the sole edge into `END`.
That pairing is what makes §6.4's corollary hold: every terminating path, including
cancellation and every budget diversion, passes through the node that writes a report.
Phases 3–5 add `researcher`, `mlops`, `evaluator` and `advance_step` behind the routers in
`engine/routing.py`; the topology grows, the terminal guarantee does not change.

Checkpointing uses `AsyncPostgresSaver` with `thread_id = run_id`, so a worker killed
mid-run resumes from the last completed node with `ainvoke(None, config)` rather than
regenerating code or re-running a 15-minute training job.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.engine.nodes import (
    coder_node,
    debugger_node,
    finalizer_node,
    init_node,
    planner_node,
    reporter_node,
    sandbox_exec_node,
)
from app.engine.routing import (
    route_after_code,
    route_after_debug,
    route_after_exec,
    route_after_plan,
)
from app.engine.state import AgentState

logger = logging.getLogger(__name__)


def build_graph(checkpointer: Any | None = None) -> Any:
    """Assemble and compile the graph."""
    g = StateGraph(AgentState)

    # Deterministic nodes.
    g.add_node("init", init_node)
    g.add_node("sandbox_exec", sandbox_exec_node)
    g.add_node("finalizer", finalizer_node)

    # LLM agent nodes.
    g.add_node("planner", planner_node)
    g.add_node("coder", coder_node)
    g.add_node("debugger", debugger_node)
    g.add_node("reporter", reporter_node)

    g.add_edge(START, "init")
    g.add_edge("init", "planner")
    g.add_conditional_edges(
        "planner", route_after_plan, {"coder": "coder", "reporter": "reporter"}
    )
    g.add_conditional_edges(
        "coder",
        route_after_code,
        {"sandbox_exec": "sandbox_exec", "reporter": "reporter"},
    )
    g.add_conditional_edges(
        "sandbox_exec",
        route_after_exec,
        {"debugger": "debugger", "reporter": "reporter"},
    )
    # The back edges. `debugger → coder` closes loop 1; `debugger → planner` is the
    # escalation the stagnation rule and `requires_replan` take when another revision of
    # the same approach cannot help.
    g.add_conditional_edges(
        "debugger",
        route_after_debug,
        {"coder": "coder", "planner": "planner", "reporter": "reporter"},
    )
    g.add_edge("reporter", "finalizer")  # reporter is finalizer's sole predecessor
    g.add_edge("finalizer", END)  # the only edge into END

    return g.compile(checkpointer=checkpointer)


def checkpointer_dsn() -> str:
    """The application DSN in the driver form the checkpointer needs.

    The app talks to Postgres over asyncpg; `AsyncPostgresSaver` uses psycopg. Same
    database, same credentials, different driver prefix — converting here keeps
    `DATABASE_URL` a single source of truth instead of adding a second connection string
    that can drift out of sync with it.
    """
    return settings.async_database_url.replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


@asynccontextmanager
async def postgres_checkpointer(dsn: str | None = None) -> AsyncIterator[Any]:
    """An `AsyncPostgresSaver` bound to the application database.

    Imported lazily: the graph is assembled, inspected and tested without a database, and
    an import-time dependency on the Postgres checkpointer would make that impossible.
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "langgraph-checkpoint-postgres is not installed. Install it to persist run "
            "state: pip install langgraph-checkpoint-postgres"
        ) from exc

    async with AsyncPostgresSaver.from_conn_string(dsn or checkpointer_dsn()) as saver:
        # Idempotent; creates the checkpoints, checkpoint_blobs and checkpoint_writes
        # tables. Alembic deliberately excludes them (defect D-006) — LangGraph owns
        # their schema and migrates it itself.
        await saver.setup()
        yield saver


@asynccontextmanager
async def compiled_graph(dsn: str | None = None) -> AsyncIterator[Any]:
    """The compiled graph with a live Postgres checkpointer, for the worker to drive."""
    async with postgres_checkpointer(dsn) as checkpointer:
        yield build_graph(checkpointer)


def run_config(run_id: str, **configurable: Any) -> dict[str, Any]:
    """The `RunnableConfig` for one run.

    `thread_id == run_id` is what makes resume work: `ainvoke(None, run_config(run_id))`
    picks the run up from its last checkpoint.
    """
    return {
        "configurable": {
            "thread_id": str(run_id),
            "run_id": str(run_id),
            **configurable,
        }
    }


__all__ = [
    "build_graph",
    "checkpointer_dsn",
    "compiled_graph",
    "postgres_checkpointer",
    "run_config",
]
