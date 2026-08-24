"""Scheduled background work: the reapers and `mlflow_backfill`.

Four jobs, registered on `WorkerSettings.cron_jobs` in `worker/main.py`. They exist
because three things in this system fail *silently* — an outage that leaves no error
anywhere, only a record that quietly stops being true — and a periodic sweep is the only
way to notice.

| Job | Every | The silent failure it catches |
|---|---|---|
| `reap_interrupted_runs` | 60 s | A worker died. `tasks.status` still says RUNNING and nothing will ever change it. |
| `reap_sandbox_containers` | 5 min | A container outlived the run that launched it and is holding CPU, memory and a bind mount. |
| `trim_event_streams` | 15 min | `XADD`'s approximate trimming let a stream drift past its cap. |
| `mlflow_backfill` | 10 min | MLflow was down when a run finished; the metrics exist on disk but not in the tracking server. |

**Nothing here fails a run.** Every job catches its own exceptions per item and continues:
a reaper that raises on the first unreachable Docker daemon leaves every later run
unswept, which is a worse outcome than one noisy log line per tick.

`mlflow_backfill` is the other half of a guarantee `mlops` (`engine/nodes/mlops.py`) makes:
MLflow being unreachable at run time must never fail a run, so that node writes the
`experiments` row with `mlflow_run_id = NULL` and lets the run proceed. This module is what
finds those rows and retries them, so an MLflow outage is lossy for at most one backfill
interval rather than permanently.

The 7-day `pluton_runs` retention (`ARCHITECTURE.md` §4.3) is exactly what makes that
possible: a row is only recoverable while `/runs/{run_id}/rev-{n}/artifacts/metrics.json`
and its siblings still exist on disk. Once the volume is swept, the row is marked
`unrecoverable` instead of being retried forever.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_layer
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.db.models.experiment import Experiment
from app.db.models.task import Task, TaskStatus
from app.engine.events import RunEmitter
from app.schemas.events import EventType
from app.services.mlflow_client import MLflowService

logger = logging.getLogger(__name__)

# MLOPS.md §11's reference implementation retries 20 rows per tick — enough to drain a
# short outage within a couple of intervals without one tick running unboundedly long.
BACKFILL_BATCH_LIMIT = 20

# How many runs one reaper tick will sweep. Bounded for the same reason the backfill is:
# a tick that scans an unbounded set can run longer than the interval between ticks and
# start overlapping itself.
REAP_BATCH_LIMIT = 100

# Docker label every sandbox container carries (`services/sandbox.py`), and the only
# handle the container reaper has on "which run launched this".
RUN_LABEL = "pluton.run_id"

# Terminal states. A container labelled for a run in one of these has outlived its run.
TERMINAL_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)


def _resolve_service(ctx: dict[str, Any] | None) -> MLflowService:
    """The `MLflowService` for this tick.

    `ctx` is arq's job context; an injected `ctx["mlflow_service"]` is what a test uses
    instead of a live MLflow server, mirroring the `configurable` overrides the engine
    nodes accept (`get_mlflow_service`, `engine/nodes/base.py`).
    """
    if ctx and ctx.get("mlflow_service") is not None:
        return ctx["mlflow_service"]
    return MLflowService()


async def _list_missing_mlflow_run(
    session: AsyncSession, *, limit: int
) -> list[Experiment]:
    """`experiments` rows still missing their MLflow run, oldest first."""
    result = await session.execute(
        select(Experiment)
        .where(Experiment.mlflow_run_id.is_(None), Experiment.unrecoverable.is_(False))
        .order_by(Experiment.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mlflow_backfill(ctx: dict[str, Any] | None = None) -> int:
    """Retry MLflow logging for attempts that were logged here while MLflow was down.

    Returns the number of rows healed this tick, matching MLOPS.md §11's reference
    implementation, so both `make backfill-mlflow` and the arq cron registration can report
    progress the same way.
    """
    service = _resolve_service(ctx)
    healed = 0

    async with AsyncSessionLocal() as session:
        rows = await _list_missing_mlflow_run(session, limit=BACKFILL_BATCH_LIMIT)
        for row in rows:
            run_dir = Path(settings.RUNS_ROOT) / str(row.run_id)
            if not run_dir.exists():
                row.unrecoverable = True
                row.metadata_json = {
                    **(row.metadata_json or {}),
                    "reason": "run directory pruned",
                }
                await session.commit()
                logger.warning(
                    "mlflow_backfill: run %s directory pruned, marking unrecoverable",
                    row.run_id,
                )
                continue

            try:
                ref = await asyncio.to_thread(
                    service.log_from_disk,
                    row.run_id,
                    run_dir,
                    task_kind=row.task_kind,
                    revision=row.revision,
                )
            except Exception as exc:  # noqa: BLE001 - still failing just means retry next tick
                logger.warning(
                    "mlflow_backfill: still failing for run %s revision %d: %s",
                    row.run_id,
                    row.revision,
                    exc,
                )
                continue

            row.mlflow_experiment_id = ref.experiment_id
            row.mlflow_run_id = ref.run_id
            row.mlflow_parent_run_id = ref.parent_run_id
            row.artifact_uri = ref.artifact_uri
            row.params = ref.logged_params
            row.metrics = ref.logged_metrics
            row.registered_model_name = ref.registered_model
            row.registered_model_version = ref.model_version
            row.metadata_json = None
            await session.commit()
            healed += 1
            logger.info(
                "mlflow_backfill: healed run %s revision %d", row.run_id, row.revision
            )

    return healed


# ------------------------------------------------------------------------------------
#  Reapers  (ARCHITECTURE.md §5.3, §5.4)
# ------------------------------------------------------------------------------------


def _redis(ctx: dict[str, Any] | None) -> Any:
    """The Redis client for this tick, injectable the same way `_resolve_service` is."""
    if ctx and ctx.get("redis") is not None:
        return ctx["redis"]
    return redis_layer.get_redis()


async def _running_runs(session: AsyncSession, *, limit: int) -> list[Task]:
    result = await session.execute(
        select(Task)
        .where(Task.status == TaskStatus.RUNNING)
        .order_by(Task.updated_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def reap_interrupted_runs(ctx: dict[str, Any] | None = None) -> int:
    """Mark runs whose worker died as `INTERRUPTED` (ARCHITECTURE.md §5.3).

    The detection rule is one line and deliberately has no timeout of its own:
    `status = RUNNING` with no `lock:run:{run_id}` in Redis. The lock *is* the liveness
    signal — a live worker renews it every 60 s against a 1800 s lease — so a missing lock
    means either the process is gone or it has been unable to reach Redis for half an
    hour, and both of those are conditions an operator needs to see.

    Deciding this from a timestamp instead would have to guess how long a legitimate node
    can take, and a 15-minute training step is a legitimate node.

    Returns the number of runs transitioned. Emits `run.failed` on each so a browser
    watching the stream stops spinning rather than waiting for a worker that is gone.
    """
    client = _redis(ctx)
    reaped = 0

    async with AsyncSessionLocal() as session:
        for row in await _running_runs(session, limit=REAP_BATCH_LIMIT):
            run_id = str(row.id)
            try:
                if await client.exists(redis_layer.lock_key(run_id)):
                    continue
            except Exception as exc:  # noqa: BLE001 - see the module docstring
                logger.warning("Could not read the lock for run %s: %s", run_id, exc)
                continue

            # Conditional, for the same reason `_claim` is: a worker may have picked the
            # run back up between the lock read and this write.
            result = await session.execute(
                update(Task)
                .where(Task.id == row.id, Task.status == TaskStatus.RUNNING)
                .values(
                    status=TaskStatus.INTERRUPTED,
                    error="the worker executing this run stopped without finishing it",
                )
            )
            await session.commit()
            # `session.execute` is typed as returning `Result`; an UPDATE always
            # produces a `CursorResult`, which is where `rowcount` lives.
            if not cast(CursorResult[Any], result).rowcount:
                continue

            reaped += 1
            logger.warning(
                "reap: run %s had no execution lock; marked INTERRUPTED", run_id
            )
            await _announce_interrupted(client, run_id)

    return reaped


async def _announce_interrupted(client: Any, run_id: str) -> None:
    """Tell anyone watching that this run is not coming back on its own."""
    emitter = RunEmitter(run_id, client)
    try:
        await emitter.summary(status="INTERRUPTED", phase="COMPLETE", resumable="1")
        await emitter.emit(
            EventType.RUN_FAILED,
            {
                "status": "INTERRUPTED",
                "error": "the worker executing this run stopped without finishing it",
                "resumable": True,
            },
        )
    finally:
        await emitter.aclose()


async def reap_sandbox_containers(ctx: dict[str, Any] | None = None) -> int:
    """`docker rm -f` every sandbox container whose run has already finished (§5.4).

    "Sandbox containers never outlive their run" is an invariant the driver enforces on
    the happy path — `execute` removes the container in a `finally`. This job covers the
    path where the driver never got to run that `finally`: the worker was SIGKILLed, the
    machine lost power, the daemon restarted mid-execution.

    A container whose run is *not* terminal is left alone. Killing one of those would
    abort a running training job on the strength of a race between this scan and the
    driver that started it.
    """
    client = ctx.get("docker") if ctx else None
    if client is None:
        try:
            import docker

            client = docker.DockerClient(base_url=settings.DOCKER_HOST)
        except Exception as exc:  # noqa: BLE001 - no daemon is not this job's problem
            logger.debug("Container reaper: no Docker client (%s)", exc)
            return 0

    try:
        containers = await asyncio.to_thread(
            client.containers.list, all=True, filters={"label": RUN_LABEL}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Container reaper: listing containers failed: %s", exc)
        return 0

    by_run: dict[str, list[Any]] = {}
    for container in containers:
        run_id = (getattr(container, "labels", None) or {}).get(RUN_LABEL)
        if run_id:
            by_run.setdefault(str(run_id), []).append(container)
    if not by_run:
        return 0

    removed = 0
    for run_id in await _terminal_run_ids(list(by_run)):
        for container in by_run[run_id]:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Container reaper: could not remove %s for run %s: %s",
                    getattr(container, "id", "?"),
                    run_id,
                    exc,
                )
                continue
            removed += 1
            logger.warning(
                "reap: removed orphaned sandbox container for terminal run %s", run_id
            )
    return removed


async def _terminal_run_ids(run_ids: list[str]) -> list[str]:
    """Which of `run_ids` name a run that has already reached a terminal state.

    A label that is not a UUID, or names no row at all, is treated as terminal: it is a
    container this platform started for a run that no longer exists in the database, which
    is exactly the orphan this job is for.
    """
    import uuid as _uuid

    parsed: dict[_uuid.UUID, str] = {}
    unknown: list[str] = []
    for run_id in run_ids:
        try:
            parsed[_uuid.UUID(run_id)] = run_id
        except (TypeError, ValueError):
            unknown.append(run_id)

    if not parsed:
        return unknown

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task.id, Task.status).where(Task.id.in_(list(parsed)))
        )
        rows = {row[0]: row[1] for row in result.all()}

    terminal = list(unknown)
    for key, run_id in parsed.items():
        status = rows.get(key)
        if status is None or status in TERMINAL_STATUSES:
            terminal.append(run_id)
    return terminal


async def trim_event_streams(ctx: dict[str, Any] | None = None) -> int:
    """`XTRIM` every run event stream back to its cap. Returns entries removed.

    `XADD MAXLEN ~` trims to *at least* the cap, not exactly it, so a stream can sit
    meaningfully above `EVENT_STREAM_MAXLEN` between macro-node boundaries. Redis expires
    the keys after 24 h anyway; this job bounds the memory a burst of long-running
    concurrent runs can hold in the meantime.

    `SCAN` rather than `KEYS`: this runs against the same Redis the event path writes to,
    and `KEYS` on a database with several thousand keys blocks it for every one of them.
    """
    client = _redis(ctx)
    removed = 0
    try:
        async for key in client.scan_iter(match="run:*:events", count=200):
            try:
                removed += int(
                    await client.xtrim(
                        key, maxlen=redis_layer.EVENT_STREAM_MAXLEN, approximate=True
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Trimming %s failed: %s", key, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scanning for event streams failed: %s", exc)
    return removed


__all__ = [
    "BACKFILL_BATCH_LIMIT",
    "REAP_BATCH_LIMIT",
    "RUN_LABEL",
    "TERMINAL_STATUSES",
    "mlflow_backfill",
    "reap_interrupted_runs",
    "reap_sandbox_containers",
    "trim_event_streams",
]
