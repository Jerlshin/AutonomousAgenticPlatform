"""`arq` worker settings — the pool that executes runs (ARCHITECTURE.md §5, §14.6).

Started with `arq app.worker.main.WorkerSettings`, which is what `make worker` and the
compose `worker` service both run.

**Concurrency is 2 and that is a hardware fact, not a placeholder.** Ollama serialises
inference across requests, so a third concurrent run does not get a third of the GPU — it
queues behind the other two while still holding a sandbox container, a Postgres session
and a checkpointer connection. `WORKER_MAX_JOBS` is the knob; raising it on a box with a
second GPU is reasonable, raising it on a laptop is not.

**`job_timeout` is the backstop, not the deadline.** The run's real wall clock is
`RUN_WALLCLOCK_SECONDS`, enforced inside `execute_run` where the run can still write a
terminal event and leave a resumable checkpoint. `WORKER_JOB_TIMEOUT_S` is set higher so it
only fires when the job is wedged somewhere that race cannot see — and when it does, the
run is left `RUNNING` with an expiring lock, which is exactly the state
`reap_interrupted_runs` is designed to find.

**`max_tries = 1`.** arq's automatic retry is wrong for this workload: a run that failed
halfway has already spent tokens, written checkpoints and possibly registered a model, and
replaying the job from the top would redo all of it. Recovery is `POST /runs/{id}/resume`,
which re-enters from the checkpoint — an operator decision, because whether a failed run
is worth resuming is a judgement about the failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.core import metrics, redis as redis_layer
from app.core.config import settings
from app.core.logging import configure_logging
from app.worker.cron import (
    mlflow_backfill,
    reap_interrupted_runs,
    reap_sandbox_containers,
    trim_event_streams,
)
from app.worker.jobs import execute_run, resume_run, worker_identity
from app.worker.queue import QUEUE_NAME, redis_settings as _redis_settings

logger = logging.getLogger(__name__)

# How often the queue-depth gauge is refreshed. Faster than the 15 s Prometheus scrape so
# a scrape never reads a value more than one interval stale, and slow enough that the
# sampler is invisible next to the jobs it is measuring.
QUEUE_SAMPLE_INTERVAL_S = 5.0


async def _sample_queue_depth_forever(ctx: dict[str, Any]) -> None:
    """Publish `pluton_queue_depth` on a timer for as long as the worker runs.

    A sampler rather than a `prometheus_client` collector because the exposition server
    runs in a thread with no event loop, and the Redis client here is async. See
    `metrics.sample_queue_depth`.
    """
    client = ctx["redis"]
    while True:
        await metrics.sample_queue_depth(client, QUEUE_NAME)
        await asyncio.sleep(QUEUE_SAMPLE_INTERVAL_S)


async def startup(ctx: dict[str, Any]) -> None:
    """Bind this worker's identity and its Redis client into the job context.

    Every job reads `ctx["redis"]` rather than calling `get_redis()` itself, which is the
    same injection seam the tests use — and it means one pool per worker process instead
    of one per module that happens to need Redis.
    """
    configure_logging()
    ctx["worker_id"] = worker_identity()
    ctx["redis"] = redis_layer.get_redis()

    # arq has no ASGI surface, so §12.1's worker metrics need their own listener. Failure
    # to bind is logged and tolerated: a worker that cannot be scraped can still run jobs,
    # and refusing to start over a monitoring port would be the wrong trade.
    metrics.start_worker_metrics_server(settings.WORKER_HEALTH_PORT)
    ctx["queue_sampler"] = asyncio.create_task(
        _sample_queue_depth_forever(ctx), name="pluton-queue-depth-sampler"
    )

    logger.info(
        "worker.started",
        extra={
            "worker_id": ctx["worker_id"],
            "max_jobs": settings.WORKER_MAX_JOBS,
            "queue": settings.REDIS_URL,
            "metrics_port": settings.WORKER_HEALTH_PORT,
        },
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Close the pools this worker opened. Runs are already released by their own locks."""
    logger.info("worker.stopping", extra={"worker_id": ctx.get("worker_id")})

    sampler = ctx.get("queue_sampler")
    if sampler is not None:
        sampler.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sampler

    await redis_layer.close_redis()


def _cron() -> list[Any]:
    """The scheduled jobs, with their intervals from `cron.py`'s table.

    `run_at_startup=False` throughout: a worker restart should not fire four sweeps at
    once, and every one of these jobs is a periodic correction rather than an
    initialisation step.

    `max_tries=1` and `timeout` on each: a reaper that hangs must not still be running
    when the next tick fires.
    """
    from arq import cron

    return [
        # Every minute, offset off the top of the minute so the sweep does not land in the
        # same instant as whatever else the system does on a round number.
        cron(
            reap_interrupted_runs,
            second={7},
            run_at_startup=False,
            max_tries=1,
            timeout=60,
        ),
        cron(
            reap_sandbox_containers,
            minute={2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57},
            run_at_startup=False,
            max_tries=1,
            timeout=120,
        ),
        cron(
            trim_event_streams,
            minute={4, 19, 34, 49},
            run_at_startup=False,
            max_tries=1,
            timeout=120,
        ),
        cron(
            mlflow_backfill,
            minute={9, 19, 29, 39, 49, 59},
            run_at_startup=False,
            max_tries=1,
            timeout=300,
        ),
    ]


class WorkerSettings:
    """arq's entrypoint. `arq app.worker.main.WorkerSettings`."""

    functions = [execute_run, resume_run]
    cron_jobs = _cron()
    on_startup = startup
    on_shutdown = shutdown

    max_jobs = settings.WORKER_MAX_JOBS
    job_timeout = settings.WORKER_JOB_TIMEOUT_S
    # See the module docstring: recovery is an explicit resume, never an automatic replay.
    max_tries = 1
    # Keep finished job results long enough for the API to report on a job that completed
    # between two polls, and no longer — the durable record is Postgres and the event
    # stream, not arq's result hash.
    keep_result = 3600
    health_check_interval = 30

    # arq reads this as a value, not a callable. Imported under an alias so the class
    # attribute and the factory that produces it can share the obvious name.
    redis_settings = _redis_settings()


__all__ = ["WorkerSettings", "shutdown", "startup"]
