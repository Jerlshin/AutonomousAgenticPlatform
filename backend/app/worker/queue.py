"""Job dispatch from the API side (ARCHITECTURE.md §5.1, §5.2).

The API validates, persists and enqueues; the worker pool executes. This module is the
enqueue half, and it deliberately imports none of `worker.jobs`: the API process should
not import LangGraph, Docker or MLflow just to push a job id onto a Redis sorted set.
arq's `enqueue_job` takes the function *name* as a string, which is what makes that
separation possible.

**Job ids are the deduplication mechanism.** arq refuses to enqueue a job whose id is
already queued or in progress, so passing `_job_id=f"run:{run_id}"` makes a double
`POST /tasks/{id}/runs` — a double-clicked button, a retried request — a no-op rather than
two workers racing for the same run lock. That is the cheap half of §5.4's "a run is never
enqueued twice"; the run lock and the conditional status transition are the half that
holds when the queue entry has already been consumed.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# arq's own default queue name. Named here so the API, the worker and any `redis-cli`
# inspection all agree on what to look at.
QUEUE_NAME = "arq:queue"

_pool: Any | None = None


def redis_settings() -> Any:
    """arq's `RedisSettings` for the operational database.

    Built from `REDIS_URL` rather than declared separately so the queue can never end up
    pointed at a different Redis — or a different logical database — from the streams and
    locks the same job touches.
    """
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(settings.REDIS_URL)


async def get_arq_pool() -> Any:
    """The process-wide arq pool, created on first use.

    Lazily imported and lazily connected: `import app.api.v1.tasks` must not require arq
    to be installed or Redis to be up, or the whole test suite acquires a service
    dependency to exercise request validation.
    """
    global _pool
    if _pool is None:
        from arq import create_pool

        _pool = await create_pool(redis_settings())
    return _pool


async def close_arq_pool() -> None:
    """Close the pool. Called from the API's shutdown hook."""
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Closing the arq pool failed: %s", exc)
        _pool = None


def run_job_id(run_id: str) -> str:
    """The deduplicating job id for a run's execution."""
    return f"run:{run_id}"


async def enqueue_run(
    run_id: str, *, resume: bool = False, pool: Any | None = None, **kwargs: Any
) -> str | None:
    """Enqueue `execute_run` for `run_id`. Returns the arq job id, or None if deduplicated.

    A `None` return is not an error: it means a job with this id is already queued or
    running, which is precisely the outcome an idempotent dispatch endpoint wants.
    """
    queue = pool if pool is not None else await get_arq_pool()
    job = await queue.enqueue_job(
        "execute_run",
        str(run_id),
        _job_id=run_job_id(run_id),
        resume=resume,
        **kwargs,
    )
    if job is None:
        logger.info("Run %s is already queued or executing; not enqueued twice", run_id)
        return None
    return job.job_id


async def queue_depth(pool: Any | None = None) -> int:
    """How many jobs are waiting. Surfaced on the dashboard and in `run.queued`."""
    queue = pool if pool is not None else await get_arq_pool()
    try:
        return int(await queue.zcard(QUEUE_NAME))
    except Exception as exc:  # noqa: BLE001 - a depth we cannot read is not a failure
        logger.debug("Reading the queue depth failed: %s", exc)
        return 0


__all__ = [
    "QUEUE_NAME",
    "close_arq_pool",
    "enqueue_run",
    "get_arq_pool",
    "queue_depth",
    "redis_settings",
    "run_job_id",
]
