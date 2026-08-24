"""Run lifecycle: detail, event backlog, cancel, resume, approve (ARCHITECTURE.md §8.2).

The dispatch endpoint itself lives in `tasks.py`, because its path is
`POST /tasks/{task_id}/runs` and a router is mounted at one prefix. Everything addressed
as `/runs/{run_id}` is here.

**Reads compose two stores, and that is deliberate.** Postgres holds what has to survive a
restart — status, error, the terminal result — and Redis holds what changes every few
seconds while a run executes: phase, current node, token spend, the `seq` cursor. Writing
the fast-moving half to Postgres would mean a transaction per node boundary for data whose
only consumer is a dashboard that is about to be told again anyway.

**Control is a signal, not a write.** `cancel` publishes on `run:{id}:control`; the worker
holding the run stops the graph cooperatively at the next checkpoint and writes the
terminal state itself. The endpoint writing `status = CANCELLED` directly would race the
worker that is still running the graph, and the loser would overwrite the winner. The one
case that *is* a direct write is a run nobody is executing — no subscribers on the channel
— where there is no worker to do it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_layer
from app.core.config import settings
from app.core.db import get_db
from app.core.security import require_token
from app.db.models.task import Task, TaskStatus
from app.schemas.common import StandardResponse
from app.schemas.run import (
    RunApproveRequest,
    RunCancelRequest,
    RunEvent,
    RunEventsResponse,
    RunRead,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_token)])

# States a run can be resumed from (§8.2's 409 on `/resume`). `INTERRUPTED` is the reaper's
# verdict; `RUNNING` is deliberately absent — a run with a live worker does not need one.
RESUMABLE = (TaskStatus.INTERRUPTED, TaskStatus.FAILED)


def get_run_redis() -> Redis:
    """The Redis client these endpoints read. A dependency so tests can override it."""
    return redis_layer.get_redis()


def ws_url(run_id: Any) -> str:
    """The WebSocket path for a run, assembled in one place (§9)."""
    return f"{settings.API_V1_STR}/ws/runs/{run_id}"


async def load_run(run_id: uuid.UUID, db: AsyncSession) -> Task:
    """The `tasks` row standing in for the run, or a 404."""
    task = await db.get(Task, run_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found."
        )
    return task


def _as_int(value: str | None) -> int:
    try:
        return int(value) if value else 0
    except (TypeError, ValueError):
        return 0


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def build_run_detail(task: Task, summary: dict[str, str], last_seq: int = 0) -> RunRead:
    """Project a `tasks` row plus its Redis summary into the §8.3 run body.

    The one place the two stores are merged. `run.snapshot` on the WebSocket carries this
    exact object, so a client that resynchronises after a gap and a client that polls the
    REST endpoint cannot end up disagreeing about the run.
    """
    return RunRead(
        run_id=task.id,
        task_id=task.id,
        title=task.title,
        prompt=task.prompt,
        status=task.status,
        # The summary knows QUEUED / PARTIAL / AWAITING_INPUT; the column does not.
        status_detail=summary.get("status") or None,
        phase=summary.get("phase") or None,
        current_node=summary.get("current_node") or None,
        percent=_as_float(summary.get("percent")),
        outcome=summary.get("outcome") or None,
        last_seq=last_seq,
        worker_id=summary.get("worker_id") or None,
        tokens_in=_as_int(summary.get("tokens_in")),
        tokens_out=_as_int(summary.get("tokens_out")),
        node_visits=_as_int(summary.get("node_visits")),
        debug_iterations=_as_int(summary.get("debug_iterations")),
        replan_count=_as_int(summary.get("replan_count")),
        error=task.error,
        result=task.result,
        created_at=task.created_at,
        updated_at=task.updated_at,
        ws_url=ws_url(task.id),
    )


async def run_snapshot(
    run_id: uuid.UUID, db: AsyncSession, client: Redis
) -> dict[str, Any]:
    """The `run.snapshot` payload — the same body `GET /runs/{run_id}` returns."""
    task = await load_run(run_id, db)
    summary = await redis_layer.read_summary(client, str(run_id))
    last = await redis_layer.last_seq(client, str(run_id))
    return build_run_detail(task, summary, last).model_dump(mode="json")


@router.get("/{run_id}", response_model=RunRead, summary="Run detail")
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    task = await load_run(run_id, db)
    summary = await redis_layer.read_summary(client, str(run_id))
    last = await redis_layer.last_seq(client, str(run_id))
    return build_run_detail(task, summary, last)


@router.get(
    "/{run_id}/events", response_model=RunEventsResponse, summary="Event backlog"
)
async def get_run_events(
    run_id: uuid.UUID,
    after_seq: int = Query(0, ge=0, description="Return events with seq > this value."),
    limit: int = Query(1000, ge=1, le=10000),
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    """The retained event log, read from the same stream the WebSocket replays."""
    await load_run(run_id, db)
    events, _cursor, oldest = await redis_layer.read_backlog(
        client, str(run_id), after_seq=after_seq, count=limit
    )
    last = await redis_layer.last_seq(client, str(run_id))
    return RunEventsResponse(
        run_id=run_id,
        total=len(events),
        after_seq=after_seq,
        last_seq=last,
        oldest_available=oldest,
        gap=bool(oldest is not None and after_seq and oldest > after_seq + 1),
        events=[
            RunEvent(
                v=e["v"],
                seq=e["seq"],
                run_id=str(run_id),
                ts=e["ts"],
                type=e["type"],
                payload=e["payload"],
            )
            for e in events
        ],
    )


@router.post(
    "/{run_id}/cancel",
    response_model=StandardResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a run",
)
async def cancel_run(
    run_id: uuid.UUID,
    payload: RunCancelRequest | None = None,
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    """Signal a cooperative cancel (§9.5's `cancel`, §5.3's `RUNNING → CANCELLED`)."""
    task = await load_run(run_id, db)
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is already terminal ({task.status.value}).",
        )

    reason = (payload.reason if payload else None) or "cancelled by operator"
    delivered = await redis_layer.publish_control(
        client, str(run_id), {"op": "cancel", "reason": reason}
    )

    if delivered:
        # A worker heard it. It owns the terminal write, and racing it here would mean
        # two writers for one row.
        return StandardResponse(
            message=f"Cancel signalled to the worker executing run {run_id}.",
            data={"delivered_to": delivered, "reason": reason},
        )

    # Nobody is executing it, so nobody else will ever write the terminal state.
    task.status = TaskStatus.CANCELLED
    task.error = reason
    task.updated_at = datetime.now(UTC)
    await db.commit()
    await _announce_cancelled(client, run_id, reason)
    return StandardResponse(
        message=f"Run {run_id} was not executing; marked CANCELLED.",
        data={"delivered_to": 0, "reason": reason},
    )


async def _announce_cancelled(client: Redis, run_id: uuid.UUID, reason: str) -> None:
    """Emit the terminal frame for a run cancelled before any worker picked it up."""
    from app.engine.events import RunEmitter
    from app.schemas.events import EventType

    emitter = RunEmitter(str(run_id), client)
    try:
        await emitter.summary(status="CANCELLED", outcome="CANCELLED", phase="COMPLETE")
        await emitter.emit(
            EventType.RUN_CANCELLED, {"reason": reason, "cancelled_by": "operator"}
        )
    finally:
        await emitter.aclose()


@router.post(
    "/{run_id}/resume",
    response_model=StandardResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume from the last checkpoint",
)
async def resume_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    """Re-enqueue an interrupted run (§5.3).

    Refuses while the lock is held even if the row says `INTERRUPTED`: the reaper and a
    slow worker can disagree for one tick, and the lock is the more current of the two.
    """
    from app.worker.queue import enqueue_run

    task = await load_run(run_id, db)
    if task.status not in RESUMABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {run_id} has status {task.status.value}; only "
                f"{' or '.join(s.value for s in RESUMABLE)} runs may be resumed."
            ),
        )
    if await client.exists(redis_layer.lock_key(str(run_id))):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} still has a live execution lock; it is not interrupted.",
        )

    task.status = TaskStatus.PENDING
    task.error = None
    task.updated_at = datetime.now(UTC)
    await db.commit()

    job_id = await enqueue_run(str(run_id), resume=True)
    return StandardResponse(
        message=f"Run {run_id} re-enqueued from its last checkpoint.",
        data={"job_id": job_id},
    )


@router.post(
    "/{run_id}/approve",
    response_model=StandardResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Release a human-approval gate",
)
async def approve_run(
    run_id: uuid.UUID,
    payload: RunApproveRequest,
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    """Deliver a HITL decision to the worker waiting on the gate (§9.5's `approve`).

    The gate itself is not built yet — §21 marks it outstanding — so today this delivers
    the signal and reports whether anything was listening. That is the honest behaviour
    for a half-built feature: the transport is real and testable, and an operator gets
    told plainly when no worker is waiting rather than a 202 that means nothing.
    """
    task = await load_run(run_id, db)
    if task.status is not TaskStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is not awaiting input (status {task.status.value}).",
        )

    delivered = await redis_layer.publish_control(
        client,
        str(run_id),
        {
            "op": "approve",
            "gate": payload.gate,
            "decision": payload.decision,
            "notes": payload.notes,
        },
    )
    if not delivered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No worker is waiting on a gate for run {run_id}.",
        )
    return StandardResponse(
        message=f"Gate '{payload.gate}' released with decision '{payload.decision}'.",
        data={"delivered_to": delivered},
    )


__all__ = [
    "build_run_detail",
    "get_run_redis",
    "load_run",
    "router",
    "run_snapshot",
    "ws_url",
]
