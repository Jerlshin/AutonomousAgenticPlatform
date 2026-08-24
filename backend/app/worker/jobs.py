"""`execute_run` — the job that owns a run from QUEUED to terminal (ARCHITECTURE.md §5).

This is the execute half of the dispatch–execute split. Everything expensive happens here:
the LangGraph invocation, the sandbox containers, the MLflow logging. The API's only
involvement after enqueueing is reading the event stream this job writes.

Four guarantees are implemented here and are worth naming, because each one is a specific
failure this design refuses to have:

* **Exactly one worker per run.** `RunLock` is `SET NX EX`, renewed every 60 s. A second
  worker that picks the same job up finds the lock held and returns without touching the
  run. The lock's *expiry* is the crash detector: a worker that dies stops renewing, the
  key lapses, and `reap_interrupted_runs` marks the run `INTERRUPTED`.
* **A run is never enqueued twice.** The lock covers the concurrent case; the conditional
  status transition covers the sequential one — `UPDATE ... WHERE status = PENDING`
  affecting zero rows means the run was already claimed.
* **A crashed worker loses at most one node.** The graph is compiled with
  `AsyncPostgresSaver` and `thread_id == run_id`, so `resume=True` re-enters with
  `ainvoke(None, config)` and replays from the last checkpoint.
* **A terminal event is always emitted.** The graph raising, the wall clock expiring and a
  cancel all converge on one `finally` that writes the run's outcome to Postgres and one
  terminal frame to the stream. A UI that never receives a terminal event shows a spinner
  forever, which is a worse failure than reporting the error.

**Cancellation is cooperative through the checkpointer, not through thread killing.** A
`cancel` on `run:{id}:control` cancels the asyncio task driving the graph. Because every
node boundary is checkpointed, the run stops at the last completed node with its state
intact — so a cancelled run is resumable, which is not true of a killed process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from app.core import metrics, redis as redis_layer
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.logging import bind_run_context, clear_run_context
from app.db.models.task import Task, TaskStatus
from app.engine.events import RunEmitter, reset_emitter, set_emitter
from app.engine.state import RunOutcome
from app.schemas.events import EventType

logger = logging.getLogger(__name__)

# Which durable `tasks.status` each engine outcome lands on. `PARTIAL` has no column of
# its own until the `runs` table exists (§7.1), so it is recorded as COMPLETED with the
# real outcome in `result` — a partial run *did* produce deliverables, and calling it
# FAILED would hide them from every list view in the UI.
_OUTCOME_STATUS: dict[RunOutcome, TaskStatus] = {
    RunOutcome.SUCCEEDED: TaskStatus.COMPLETED,
    RunOutcome.PARTIAL: TaskStatus.COMPLETED,
    RunOutcome.FAILED: TaskStatus.FAILED,
    RunOutcome.CANCELLED: TaskStatus.CANCELLED,
}

# Which terminal event announces each outcome (§9.4).
_OUTCOME_EVENT: dict[RunOutcome, EventType] = {
    RunOutcome.SUCCEEDED: EventType.RUN_COMPLETED,
    RunOutcome.PARTIAL: EventType.RUN_COMPLETED,
    RunOutcome.FAILED: EventType.RUN_FAILED,
    RunOutcome.CANCELLED: EventType.RUN_CANCELLED,
}


def worker_identity() -> str:
    """A stable-enough name for this worker process.

    Host plus pid rather than a uuid: the value ends up in the lock, in `run.started` and
    in the interrupted-run log line, and "which container was that" is the question being
    asked at that point.
    """
    return f"{socket.gethostname()}-{os.getpid()}"


class ControlListener:
    """Subscribes to `run:{id}:control` and turns signals into asyncio primitives.

    A cancel arriving as a network message and a cancel arriving as a wall-clock timeout
    should stop the run the same way, so both are funnelled into one `asyncio.Event` that
    the driver waits on alongside the graph.
    """

    def __init__(self, client: Any, run_id: str) -> None:
        self.client = client
        self.run_id = str(run_id)
        self.cancelled = asyncio.Event()
        self.cancel_reason: str | None = None
        self.approvals: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pubsub: Any | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            self._pubsub = self.client.pubsub()
            await self._pubsub.subscribe(redis_layer.control_channel(self.run_id))
        except Exception as exc:  # noqa: BLE001 - a run without cancel is still a run
            logger.warning(
                "Could not subscribe to the control channel for run %s: %s",
                self.run_id,
                exc,
            )
            self._pubsub = None
            return
        self._task = asyncio.create_task(self._listen(), name=f"control-{self.run_id}")

    async def _listen(self) -> None:
        # A guard, not an assertion: `start()` only creates this task after the subscribe
        # succeeded, but `python -O` strips an `assert` and would leave the `async for`
        # below to raise AttributeError inside a task nobody awaits — a control channel
        # that silently stopped listening, which is the failure mode a cancel depends on
        # not having.
        pubsub = self._pubsub
        if pubsub is None:
            return
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    body = json.loads(message.get("data") or "{}")
                except (TypeError, ValueError):
                    continue
                op = body.get("op")
                if op == "cancel":
                    self.cancel_reason = body.get("reason") or "cancelled by operator"
                    self.cancelled.set()
                elif op == "approve":
                    await self.approvals.put(body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Control listener for run %s stopped: %s", self.run_id, exc)

    def request_cancel(self, reason: str) -> None:
        """Cancel from inside the worker — the wall-clock deadline uses this."""
        self.cancel_reason = reason
        self.cancelled.set()

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None


async def _load_task(run_id: str) -> Task | None:
    """The `tasks` row standing in for the run (see `schemas/run.py`)."""
    try:
        key = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        return None
    async with AsyncSessionLocal() as session:
        return await session.get(Task, key)


async def _claim(run_id: str) -> bool:
    """Conditional `PENDING → RUNNING`. False means another worker already claimed it.

    The `WHERE status = PENDING` clause is the whole point: two workers issuing this
    concurrently produce one row updated and one row not, and the loser stops. A
    read-then-write would let both read PENDING and both proceed.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(Task)
            .where(Task.id == uuid.UUID(str(run_id)), Task.status == TaskStatus.PENDING)
            .values(status=TaskStatus.RUNNING, updated_at=datetime.now(UTC))
        )
        await session.commit()
        # `session.execute` is typed as returning `Result`; an UPDATE always
        # produces a `CursorResult`, which is where `rowcount` lives.
        return bool(cast(CursorResult[Any], result).rowcount)


async def _finish(
    run_id: str, status: TaskStatus, *, result: dict[str, Any] | None, error: str | None
) -> None:
    """Write the run's terminal state to Postgres. Never raises into the caller."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Task)
                .where(Task.id == uuid.UUID(str(run_id)))
                .values(
                    status=status,
                    result=result,
                    error=error,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - the event stream still carries the outcome
        logger.error("Could not persist the terminal state of run %s: %s", run_id, exc)


def _deliverables_payload(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        d.model_dump(mode="json") if hasattr(d, "model_dump") else dict(d)
        for d in (state.get("deliverables") or [])
    ]


def _record_run_metrics(state: dict[str, Any], outcome: RunOutcome) -> None:
    """Publish the §12.1 run-level series from the state the run finished with.

    Called from `_emit_terminal` for the same reason the durable write lives there: it is
    the one path every outcome converges on, so a run that failed, was cancelled or blew
    its wall clock is counted exactly as reliably as one that succeeded. Metrics recorded
    on the success path only are metrics that lie about the success rate.
    """
    task_kind = state.get("task_kind")
    plan = state.get("plan")
    if not task_kind and plan is not None:
        task_kind = getattr(plan, "task_kind", None)

    usage = state.get("usage")
    duration = getattr(usage, "elapsed_seconds", None) if usage is not None else None
    metrics.record_run(outcome.value, task_kind, duration)
    metrics.observe_debug_iterations(task_kind, state.get("debug_iterations") or 0)

    verdict = state.get("verdict")
    required = [r for r in getattr(verdict, "criteria_results", []) if r.required]
    if required:
        metrics.observe_criteria_satisfaction(
            task_kind, sum(1 for r in required if r.passed) / len(required)
        )


def _terminal_payload(state: dict[str, Any], outcome: RunOutcome) -> dict[str, Any]:
    """The body of `run.completed` / `run.failed` / `run.cancelled` (§9.4)."""
    verdict = state.get("verdict")
    mlflow = state.get("mlflow")
    usage = state.get("usage")
    payload: dict[str, Any] = {
        "status": outcome.value,
        "deliverables": _deliverables_payload(state),
        "bundle_url": f"{settings.API_V1_STR}/runs/{state.get('run_id')}/bundle",
    }
    if verdict is not None:
        payload["evaluation"] = verdict.model_dump(mode="json")
    if mlflow is not None:
        payload["mlflow"] = mlflow.model_dump(mode="json")
    if usage is not None:
        payload["usage"] = usage.model_dump(mode="json")
    return payload


async def _drive_graph(
    graph: Any, run_id: str, seed_state: dict[str, Any] | None, config: dict[str, Any]
) -> dict[str, Any]:
    """One `ainvoke`. `seed_state is None` is the resume form (§5.2)."""
    return await graph.ainvoke(seed_state, config)


async def execute_run(
    ctx: dict[str, Any] | None = None,
    run_id: str | None = None,
    *,
    resume: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    """Execute one run end to end. The arq job body.

    `ctx` is arq's job context. Tests inject `graph`, `emitter` and `redis` through it the
    same way the engine nodes accept `configurable` overrides, so this whole function runs
    against fakes with no Redis, no Postgres and no models.
    """
    ctx = ctx or {}
    if run_id is None:
        raise ValueError("execute_run requires a run_id")
    run_id = str(run_id)

    client = ctx.get("redis") or redis_layer.get_redis()
    worker_id = ctx.get("worker_id") or worker_identity()
    emitter: RunEmitter = ctx.get("emitter") or RunEmitter(run_id, client)

    lock = redis_layer.RunLock(client, run_id, worker_id)
    if not await lock.acquire():
        owner = await lock.owner()
        logger.warning("Run %s is already held by %s; skipping", run_id, owner)
        return {"run_id": run_id, "skipped": "locked", "owner": owner}
    lock.start_renewer()

    control = ControlListener(client, run_id)
    await control.start()
    emitter_token = set_emitter(emitter)

    # Entered only after the lock is held, so `pluton_active_runs` counts runs this worker
    # is actually executing rather than jobs it looked at. An ExitStack rather than a
    # `with` block because the gauge has to come down on every path out of the `finally`
    # below — including the `raise` that arq's shutdown path takes.
    in_flight = contextlib.ExitStack()
    in_flight.enter_context(metrics.run_in_flight(worker_id))
    # §12.3: bound once here, so every line this run produces — in this module, in the
    # engine nodes, in the sandbox driver three frames down — carries the run's identity
    # without a single call site having to pass it.
    bind_run_context(run_id=run_id, worker_id=worker_id)

    outcome = RunOutcome.FAILED
    error: str | None = None
    final_state: dict[str, Any] = {"run_id": run_id}

    try:
        task = await _load_task(run_id)
        if task is None and not overrides.get("prompt"):
            raise RunNotFound(run_id)

        if not resume and task is not None and not await _claim(run_id):
            # Somebody else moved it out of PENDING between the enqueue and here. The
            # lock says they are not executing it *now*, so this is a stale queue entry
            # rather than a race worth fighting.
            logger.info(
                "Run %s was not claimable (status is not PENDING); skipping", run_id
            )
            return {"run_id": run_id, "skipped": "not-claimable"}

        await emitter.summary(
            status="RUNNING",
            worker_id=worker_id,
            phase="INIT",
            started_at=datetime.now(UTC).isoformat(),
        )
        await emitter.emit(
            EventType.RUN_STARTED,
            {
                "worker_id": worker_id,
                "resumed": resume,
                "model_routing": _model_routing(),
            },
        )

        final_state = await _run_graph(
            ctx, run_id, task, emitter, control, resume=resume, overrides=overrides
        )
        outcome = final_state.get("outcome") or RunOutcome.FAILED
        if isinstance(outcome, str):
            outcome = RunOutcome(outcome)

    except asyncio.CancelledError:
        # The job itself was cancelled (worker shutting down), as distinct from the run
        # being cancelled. Re-raise so arq can requeue, but leave the lock released by
        # the `finally` so whoever picks it up next can take it.
        outcome = RunOutcome.CANCELLED
        error = "worker shutdown"
        raise
    except RunCancelled as exc:
        outcome = RunOutcome.CANCELLED
        error = str(exc)
        final_state = exc.state or final_state
    except Exception as exc:  # noqa: BLE001 - every failure still gets a terminal event
        outcome = RunOutcome.FAILED
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Run %s failed", run_id)
    finally:
        reset_emitter(emitter_token)
        with contextlib.suppress(Exception):
            await _emit_terminal(emitter, run_id, outcome, final_state, error, control)
        await control.aclose()
        with contextlib.suppress(Exception):
            await emitter.aclose()
        await lock.release()
        in_flight.close()
        clear_run_context()

    return {"run_id": run_id, "outcome": outcome.value, "error": error}


async def _run_graph(
    ctx: dict[str, Any],
    run_id: str,
    task: Task | None,
    emitter: RunEmitter,
    control: ControlListener,
    *,
    resume: bool,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Drive the graph, racing it against cancellation and the wall-clock budget."""
    from app.engine.graph import compiled_graph, run_config

    # `run_config` already writes `run_id` and `thread_id` into `configurable`, so passing
    # a `run_id` key here would collide with its positional argument.
    configurable: dict[str, Any] = {
        "task_id": str(task.id) if task is not None else run_id,
        "prompt": overrides.get("prompt") or (task.prompt if task else ""),
        "emitter": emitter,
        **(ctx.get("configurable") or {}),
    }
    config = run_config(run_id, **configurable)

    seed_state: dict[str, Any] | None = None
    if not resume:
        seed_state = {
            "run_id": run_id,
            "task_id": configurable["task_id"],
            "thread_id": run_id,
            "prompt": configurable["prompt"],
            "task_kind": overrides.get("task_kind") or "",
        }

    injected = ctx.get("graph")
    if injected is not None:
        return await _await_with_control(
            _drive_graph(injected, run_id, seed_state, config), control, run_id
        )

    async with compiled_graph() as graph:
        return await _await_with_control(
            _drive_graph(graph, run_id, seed_state, config), control, run_id
        )


async def _await_with_control(
    coro: Any, control: ControlListener, run_id: str
) -> dict[str, Any]:
    """Await the graph while a cancel or the run deadline can stop it.

    The deadline is enforced here rather than by arq's `job_timeout`, which kills the job
    without giving the run a chance to write a terminal event or leave a resumable
    checkpoint. `WORKER_JOB_TIMEOUT_S` stays as the outer backstop for a worker wedged
    somewhere this race cannot see.
    """
    graph_task = asyncio.ensure_future(coro)
    cancel_task = asyncio.ensure_future(control.cancelled.wait())
    deadline = settings.RUN_WALLCLOCK_SECONDS

    done, pending = await asyncio.wait(
        {graph_task, cancel_task},
        timeout=deadline if deadline > 0 else None,
        return_when=asyncio.FIRST_COMPLETED,
    )

    if graph_task in done:
        cancel_task.cancel()
        return graph_task.result()

    # Either a cancel arrived or the wall clock ran out. Both stop the graph the same way.
    reason = (
        control.cancel_reason or "cancelled by the operator"
        if control.cancelled.is_set()
        else f"run exceeded its {deadline}s wall-clock budget"
    )
    for task in pending:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await graph_task
    logger.info("Run %s stopped: %s", run_id, reason)
    raise RunCancelled(reason)


async def _emit_terminal(
    emitter: RunEmitter,
    run_id: str,
    outcome: RunOutcome,
    state: dict[str, Any],
    error: str | None,
    control: ControlListener,
) -> None:
    """The one place a run's terminal state reaches Postgres and the event stream."""
    _record_run_metrics(state, outcome)
    payload = _terminal_payload(state, outcome)
    if outcome is RunOutcome.FAILED:
        last_error = state.get("last_error")
        payload["error"] = error or (
            last_error.message if last_error else "unknown failure"
        )
        payload["last_node"] = (state.get("phase") or "").lower() or None
    if outcome is RunOutcome.CANCELLED:
        payload["reason"] = control.cancel_reason or error or "cancelled"
        payload["cancelled_by"] = "operator" if control.cancelled.is_set() else "system"

    await _finish(
        run_id,
        _OUTCOME_STATUS[outcome],
        result=payload if outcome is not RunOutcome.FAILED else None,
        error=payload.get("error") if outcome is RunOutcome.FAILED else None,
    )
    await emitter.summary(
        status=_OUTCOME_STATUS[outcome].value,
        outcome=outcome.value,
        phase="COMPLETE",
        finished_at=datetime.now(UTC).isoformat(),
    )
    await emitter.emit(_OUTCOME_EVENT[outcome], payload)


def _model_routing() -> dict[str, str]:
    """The role→model table, for `run.started`. Never fatal — it is display detail."""
    try:
        from app.engine.llm import model_routing_snapshot

        return model_routing_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not snapshot the model routing: %s", exc)
        return {}


class RunNotFound(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"no task row for run {run_id}")
        self.run_id = run_id


class RunCancelled(RuntimeError):
    """The run was stopped cooperatively — by an operator or by the wall clock."""

    def __init__(self, reason: str, state: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.state = state


async def resume_run(
    ctx: dict[str, Any] | None = None, run_id: str | None = None
) -> dict[str, Any]:
    """Re-enter a run from its last checkpoint (§5.3's `INTERRUPTED → RUNNING`).

    A thin alias rather than a second implementation: resume differs from a fresh start in
    exactly one way — `ainvoke(None, config)` instead of `ainvoke(seed_state, config)` —
    and duplicating four hundred lines of lock, control and terminal handling to express
    that would guarantee the two paths eventually diverge.
    """
    return await execute_run(ctx, run_id, resume=True)


__all__ = [
    "ControlListener",
    "RunCancelled",
    "RunNotFound",
    "execute_run",
    "resume_run",
    "worker_identity",
]
