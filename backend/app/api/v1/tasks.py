"""Task CRUD, plus the run dispatch endpoint (ARCHITECTURE.md §8.2, §5.2).

`POST /tasks/{task_id}/runs` is the dispatch half of the dispatch–execute split: it
validates, transitions the row and enqueues, then answers `202` with a `ws_url`. It never
touches LangGraph — the whole point of §5.1 is that a request handler is the wrong place
for a twenty-minute job.
"""

import contextlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.runs import build_run_detail, get_run_redis, ws_url
from app.core import redis as redis_layer
from app.core.db import get_db
from app.core.security import require_token
from app.db.models.task import Task, TaskStatus
from app.schemas.common import StandardResponse
from app.schemas.run import RunAccepted, RunListResponse
from app.schemas.task import TaskCreate, TaskListResponse, TaskRead, TaskUpdate

logger = logging.getLogger(__name__)

# §13.2: every non-health endpoint requires `Authorization: Bearer {PLATFORM_API_TOKEN}`.
# Declared on the router rather than per-endpoint so a route added later inherits it —
# authentication that has to be remembered on each handler is authentication that will
# eventually be forgotten on one.
router = APIRouter(dependencies=[Depends(require_token)])

# Statuses that mean "a run is already in flight for this task", which §8.2 answers with
# 409 rather than starting a second one. `PENDING` counts: the job is queued even if no
# worker has picked it up.
ACTIVE_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING)


# creating a new task
@router.post(
    "",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit Task",
)
async def create_task(
    payload: TaskCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Submit a new multi-agent research task."""
    task = Task(
        title=payload.title,
        prompt=payload.prompt,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@router.get("", response_model=TaskListResponse, summary="List Tasks")
async def list_tasks(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Fetch paginated list of submitted tasks."""
    total_query = await db.execute(select(func.count(Task.id)))
    total = total_query.scalar_one()

    query = select(Task).order_by(Task.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    tasks = result.scalars().all()

    return TaskListResponse(
        total=total, tasks=[TaskRead.model_validate(task) for task in tasks]
    )


@router.get("/{task_id}", response_model=TaskRead, summary="Get Task Details")
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Get status and result payload for a specific task."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )
    return task


@router.patch("/{task_id}", response_model=TaskRead, summary="Update Task Status")
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Update task lifecycle state, final result JSON, or error message."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(task, key, value)

    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/{task_id}", response_model=StandardResponse, summary="Delete Task")
async def delete_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Delete a task record along with all associated logs and artifacts."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )

    await db.delete(task)
    await db.commit()
    return StandardResponse(message=f"Task {task_id} deleted successfully.")


# ------------------------------------------------------------------------------------
#  Run dispatch  (§5.2)
# ------------------------------------------------------------------------------------


@router.post(
    "/{task_id}/runs",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a run",
)
async def create_run(
    task_id: uuid.UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    """Enqueue a run for `task_id` and return where to watch it.

    Three independent things stop this from starting the same run twice, and they cover
    different windows: `Idempotency-Key` catches a retried HTTP request, the `ACTIVE_STATUSES`
    check catches a second deliberate dispatch, and arq's job id catches a race between two
    API processes. The run lock in the worker is the last line, for the case where all
    three are somehow passed.
    """
    from app.worker.queue import enqueue_run

    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )

    if idempotency_key:
        cached = await _replay_idempotent(client, idempotency_key)
        if cached is not None:
            return cached

    if task.status in ACTIVE_STATUSES and task.status is TaskStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task {task_id} already has a run in progress.",
        )

    task.status = TaskStatus.PENDING
    task.error = None
    task.result = None
    await db.commit()

    # A run id equal to the task id, per the "a Task is a Run" convention documented in
    # `schemas/run.py`. When the `runs` table lands this becomes a fresh uuid and nothing
    # above this line changes.
    run_id = task.id
    await _announce_queued(client, run_id)

    job_id = await enqueue_run(str(run_id))
    response = RunAccepted(
        run_id=run_id,
        task_id=task.id,
        job_id=job_id,
        ws_url=ws_url(run_id),
        already_running=job_id is None,
    )
    if idempotency_key:
        await _store_idempotent(client, idempotency_key, response)
    return response


async def _announce_queued(client: Redis, run_id: uuid.UUID) -> None:
    """Emit `run.queued` as `seq 1` before the worker exists (§5.2).

    Emitted from the API rather than the worker on purpose: a browser that opens the
    socket immediately after the 202 would otherwise see nothing at all until a worker
    picked the job up, which on a busy box can be minutes.
    """
    from app.engine.events import RunEmitter
    from app.schemas.events import EventType
    from app.worker.queue import queue_depth

    emitter = RunEmitter(str(run_id), client)
    try:
        await emitter.summary(status="QUEUED", phase="INIT")
        await emitter.emit(EventType.RUN_QUEUED, {"position": await queue_depth()})
    except Exception as exc:  # noqa: BLE001 - a dispatch must not fail on telemetry
        logger.warning("Could not announce run %s as queued: %s", run_id, exc)
    finally:
        await emitter.aclose()


async def _replay_idempotent(client: Redis, key: str) -> RunAccepted | None:
    """The original response for an `Idempotency-Key`, if this key has been seen (§8.1)."""
    try:
        cached = await client.get(redis_layer.idem_key(key))
    except Exception as exc:  # noqa: BLE001 - an unreadable cache just means no replay
        logger.warning("Idempotency lookup failed: %s", exc)
        return None
    if not cached:
        return None
    try:
        return RunAccepted.model_validate_json(cached)
    except Exception:  # noqa: BLE001 - a stale shape is not worth failing the request over
        return None


async def _store_idempotent(client: Redis, key: str, response: RunAccepted) -> None:
    with contextlib.suppress(Exception):
        await client.set(
            redis_layer.idem_key(key),
            response.model_dump_json(),
            ex=redis_layer.RUN_KEY_TTL_S,
        )


@router.get(
    "/{task_id}/runs", response_model=RunListResponse, summary="List runs for a task"
)
async def list_task_runs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    client: Redis = Depends(get_run_redis),
) -> Any:
    """Every run of a task.

    Exactly one today, by the "a Task is a Run" convention — the endpoint exists in its
    plural form because that convention is temporary and the clients written against it
    should not have to change when it ends.
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )
    summary = await redis_layer.read_summary(client, str(task.id))
    last = await redis_layer.last_seq(client, str(task.id))
    return RunListResponse(total=1, runs=[build_run_detail(task, summary, last)])
