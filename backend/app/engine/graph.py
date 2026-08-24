"""Graph assembly (AGENTS.md §4).

The graph is cyclic, and the cycle is the point: `coder → sandbox_exec → debugger → coder`
is loop 1, the correctness loop (§6.1). A program that crashes is diagnosed and rewritten
rather than reported as a failure, which is the difference between a code generator and a
system that finishes the job.

```
START → init → planner ─┬─> researcher ⇄ researcher ─> coder
                        ├─> coder                        │
                        ├─> evaluator                    v
                        └─> reporter               sandbox_exec ─┬─> debugger ─┬─> coder        (loop 1)
                                                                 │             ├─> researcher
                                                                 ├─> mlops     ├─> planner
                                                                 │     │       └─> reporter
                                                                 └─────┴─> evaluator
                                                                             │
                       coder    <──── REFINE ────────────────────────────────┤                  (loop 2)
                       planner  <──── REPLAN ────────────────────────────────┤                  (loop 3)
                       reporter <──── ACCEPT / ABORT ────────────────────────┘

                                   reporter → finalizer → END
```

Three things bound loop 1, and all three are counters read by pure routers rather than
judgements made by a model: `max_debug_iterations` (how many times the Debugger may try),
`max_sandbox_executions` (how many containers a run may spend), and the stagnation rule —
three consecutive failures with one error fingerprint escalate to the Planner, because at
that point the *approach* is what is wrong, not the code. `researcher ⇄ researcher` has its
own bound, `RESEARCH_MAX_ROUNDS` — insufficient context is never fatal, so it converges to
the Coder rather than the terminal node once it is spent.

Loop 2 (`evaluator → coder → sandbox_exec → mlops → evaluator`) shares
`max_debug_iterations` with loop 1, counted from the `REFINE` verdicts in the history;
loop 3 (`evaluator → planner → … → evaluator`) is bounded by `max_replans`. Both bounds are
checked in `route_after_eval` before the back edge is taken.

`reporter` is `finalizer`'s sole predecessor and `finalizer` is the sole edge into `END`.
That pairing is what makes §6.4's corollary hold: every terminating path, including
cancellation and every budget diversion, passes through the node that writes a report.
Phase 4 adds `mlops`: a CLEAN `TRAIN` step routes there instead of straight to `reporter`
(`route_after_exec`). Phase 5 adds `evaluator` behind it — `mlops` still has exactly one
successor and nothing to decide, but that successor is now the node that judges what it
just logged. The topology grows; the terminal guarantee does not change, because every new
edge either ends at `reporter` or re-enters a cycle whose counter is already spent by
taking it.

`advance_step` (§5.4 rule 5) is the one node of the normative topology still unbuilt.

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
    evaluator_node,
    finalizer_node,
    init_node,
    mlops_node,
    planner_node,
    reporter_node,
    researcher_node,
    sandbox_exec_node,
)
from app.engine.routing import (
    route_after_code,
    route_after_debug,
    route_after_eval,
    route_after_exec,
    route_after_plan,
    route_after_research,
)
from app.engine.state import AgentState

logger = logging.getLogger(__name__)


def build_graph(checkpointer: Any | None = None) -> Any:
    """Assemble and compile the graph."""
    g = StateGraph(AgentState)

    # Deterministic nodes.
    g.add_node("init", init_node)
    g.add_node("sandbox_exec", sandbox_exec_node)
    g.add_node("mlops", mlops_node)
    g.add_node("finalizer", finalizer_node)

    # LLM agent nodes.
    g.add_node("planner", planner_node)
    g.add_node("researcher", researcher_node)
    g.add_node("coder", coder_node)
    g.add_node("debugger", debugger_node)
    g.add_node("evaluator", evaluator_node)
    g.add_node("reporter", reporter_node)

    g.add_edge(START, "init")
    g.add_edge("init", "planner")
    g.add_conditional_edges(
        "planner",
        route_after_plan,
        {
            "researcher": "researcher",
            "coder": "coder",
            "evaluator": "evaluator",
            "reporter": "reporter",
        },
    )
    g.add_conditional_edges(
        "researcher",
        route_after_research,
        {"researcher": "researcher", "coder": "coder"},
    )
    g.add_conditional_edges(
        "coder",
        route_after_code,
        {"sandbox_exec": "sandbox_exec", "reporter": "reporter"},
    )
    g.add_conditional_edges(
        "sandbox_exec",
        route_after_exec,
        {
            "debugger": "debugger",
            "mlops": "mlops",
            "evaluator": "evaluator",
            "reporter": "reporter",
        },
    )
    # The back edges. `debugger → coder` closes loop 1; `debugger → researcher` answers a
    # diagnosis blocked on an unknown API; `debugger → planner` is the escalation the
    # stagnation rule and `requires_replan` take when another revision of the same
    # approach cannot help.
    g.add_conditional_edges(
        "debugger",
        route_after_debug,
        {
            "coder": "coder",
            "researcher": "researcher",
            "planner": "planner",
            "reporter": "reporter",
        },
    )
    # `mlops` has no branch of its own — it has no LLM and nothing left to decide once
    # logging is done, so its single edge hands the attempt it just logged to the node that
    # judges it.
    g.add_edge("mlops", "evaluator")
    # The quality and strategy loops. `evaluator → coder` is loop 2 (§6.2): same plan,
    # better code, driven by `verdict.refine_directive`. `evaluator → planner` is loop 3
    # (§6.3): a different approach, driven by `verdict.replan_directive` and the failure
    # history. Both edges are guarded by `route_after_eval`, which spends the budget before
    # it takes them, so neither can outlive its bound.
    g.add_conditional_edges(
        "evaluator",
        route_after_eval,
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
