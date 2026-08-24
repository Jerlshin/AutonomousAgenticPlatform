"""`execute_run` — the job that owns a run from QUEUED to terminal (ARCHITECTURE.md §5).

What is worth testing here is the *envelope* around the graph, not the graph — that has
its own suite. Specifically the four guarantees the module docstring names: one worker per
run, one claim per run, resume re-enters without a seed state, and a terminal event is
emitted on every exit path including the ones nobody plans for.

The graph, the database writes and Redis are all injected, so the whole lifecycle runs in
milliseconds with no services.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.core import redis as redis_layer
from app.db.models.task import TaskStatus
from app.engine.state import RunOutcome
from app.worker import jobs
from tests.fakes import FakeRedis, run

RUN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeGraph:
    """A graph that records how it was invoked and returns a scripted final state."""

    def __init__(
        self, final: dict[str, Any] | None = None, *, raises: Exception | None = None
    ) -> None:
        self.final = final if final is not None else {"outcome": RunOutcome.SUCCEEDED}
        self.raises = raises
        self.calls: list[tuple[Any, dict]] = []

    async def ainvoke(self, state: Any, config: dict) -> dict[str, Any]:
        self.calls.append((state, config))
        if self.raises is not None:
            raise self.raises
        return {"run_id": RUN_ID, **self.final}


class SlowGraph:
    """A graph that never finishes on its own, for the cancellation paths."""

    def __init__(self) -> None:
        self.cancelled = False

    async def ainvoke(self, _state: Any, _config: dict) -> dict[str, Any]:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {}


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """A Redis fake plus stubbed database access, so no service is needed."""
    redis = FakeRedis()
    finished: list[tuple[str, TaskStatus, dict | None, str | None]] = []

    class FakeTask:
        id = RUN_ID
        prompt = "Train a classifier."
        status = TaskStatus.PENDING

    monkeypatch.setattr(jobs, "_load_task", _async_return(FakeTask()))
    monkeypatch.setattr(jobs, "_claim", _async_return(True))

    async def record(run_id, status, *, result, error):
        finished.append((run_id, status, result, error))

    monkeypatch.setattr(jobs, "_finish", record)
    return redis, finished


def _async_return(value: Any):
    async def _inner(*_a: Any, **_k: Any) -> Any:
        return value

    return _inner


def events_of(redis: FakeRedis, run_id: str = RUN_ID) -> list[tuple[str, dict]]:
    return [
        (fields["type"], json.loads(fields["payload"]))
        for _id, fields in redis.streams.get(redis_layer.events_key(run_id), [])
    ]


def test_a_successful_run_emits_started_then_completed(wired) -> None:
    redis, finished = wired
    graph = FakeGraph(
        {"outcome": RunOutcome.SUCCEEDED, "deliverables": [], "usage": None}
    )
    result = run(
        jobs.execute_run({"redis": redis, "graph": graph, "worker_id": "w1"}, RUN_ID)
    )

    assert result["outcome"] == "SUCCEEDED"
    kinds = [k for k, _p in events_of(redis)]
    assert kinds == ["run.started", "run.completed"]

    started = dict(events_of(redis))["run.started"]
    assert started["worker_id"] == "w1"
    assert started["resumed"] is False

    completed = dict(events_of(redis))["run.completed"]
    assert completed["status"] == "SUCCEEDED"
    assert completed["bundle_url"].endswith(f"/runs/{RUN_ID}/bundle")

    assert finished == [(RUN_ID, TaskStatus.COMPLETED, completed, None)]


def test_the_lock_is_released_so_the_run_can_be_resumed(wired) -> None:
    redis, _finished = wired
    run(jobs.execute_run({"redis": redis, "graph": FakeGraph()}, RUN_ID))
    assert redis_layer.lock_key(RUN_ID) not in redis.strings


def test_a_second_worker_finds_the_run_locked_and_stops(wired) -> None:
    """§5.4's first invariant: exactly one worker executes a given run at a time."""
    redis, finished = wired
    redis.strings[redis_layer.lock_key(RUN_ID)] = "worker-a"
    graph = FakeGraph()

    result = run(
        jobs.execute_run(
            {"redis": redis, "graph": graph, "worker_id": "worker-b"}, RUN_ID
        )
    )

    assert result == {"run_id": RUN_ID, "skipped": "locked", "owner": "worker-a"}
    assert graph.calls == []
    assert finished == []
    # The loser must not have released the winner's lock on its way out.
    assert redis.strings[redis_layer.lock_key(RUN_ID)] == "worker-a"


def test_a_run_that_cannot_be_claimed_is_skipped(wired, monkeypatch) -> None:
    """The sequential half of "a run is never enqueued twice" (§5.4)."""
    redis, finished = wired
    monkeypatch.setattr(jobs, "_claim", _async_return(False))
    graph = FakeGraph()

    result = run(jobs.execute_run({"redis": redis, "graph": graph}, RUN_ID))

    assert result == {"run_id": RUN_ID, "skipped": "not-claimable"}
    assert graph.calls == []
    assert redis_layer.lock_key(RUN_ID) not in redis.strings


def test_a_partial_run_is_recorded_as_completed_with_its_real_outcome(wired) -> None:
    """PARTIAL produced deliverables; filing it as FAILED would hide them (§5.3)."""
    redis, finished = wired
    run(
        jobs.execute_run(
            {"redis": redis, "graph": FakeGraph({"outcome": RunOutcome.PARTIAL})},
            RUN_ID,
        )
    )
    assert [k for k, _p in events_of(redis)][-1] == "run.completed"
    assert dict(events_of(redis))["run.completed"]["status"] == "PARTIAL"
    assert finished[-1][1] is TaskStatus.COMPLETED


def test_a_graph_that_raises_still_produces_a_terminal_event(wired) -> None:
    """A UI that never receives a terminal event spins forever; that is the worse failure."""
    redis, finished = wired
    graph = FakeGraph(raises=RuntimeError("ollama went away"))

    result = run(jobs.execute_run({"redis": redis, "graph": graph}, RUN_ID))

    assert result["outcome"] == "FAILED"
    assert [k for k, _p in events_of(redis)] == ["run.started", "run.failed"]
    failed = dict(events_of(redis))["run.failed"]
    assert "ollama went away" in failed["error"]
    assert finished[-1][1] is TaskStatus.FAILED
    assert redis_layer.lock_key(RUN_ID) not in redis.strings


def test_a_control_cancel_stops_the_graph_and_files_it_cancelled(wired) -> None:
    redis, finished = wired
    graph = SlowGraph()

    async def scenario() -> dict:
        task = asyncio.ensure_future(
            jobs.execute_run({"redis": redis, "graph": graph}, RUN_ID)
        )
        # Wait until the worker has subscribed, then signal through the real channel.
        for _ in range(200):
            if redis.subscribers.get(redis_layer.control_channel(RUN_ID)):
                break
            await asyncio.sleep(0.005)
        await redis_layer.publish_control(
            redis, RUN_ID, {"op": "cancel", "reason": "operator changed mind"}
        )
        return await task

    result = run(scenario())

    assert result["outcome"] == "CANCELLED"
    assert graph.cancelled is True
    cancelled = dict(events_of(redis))["run.cancelled"]
    assert cancelled["reason"] == "operator changed mind"
    assert cancelled["cancelled_by"] == "operator"
    assert finished[-1][1] is TaskStatus.CANCELLED


def test_the_wallclock_budget_cancels_a_run_that_will_not_finish(
    wired, monkeypatch
) -> None:
    """§14.5's `RUN_WALLCLOCK_SECONDS`, enforced where a terminal event is still possible."""
    from app.core.config import settings

    redis, finished = wired
    monkeypatch.setattr(settings, "RUN_WALLCLOCK_SECONDS", 0.05, raising=False)
    graph = SlowGraph()

    result = run(jobs.execute_run({"redis": redis, "graph": graph}, RUN_ID))

    assert result["outcome"] == "CANCELLED"
    assert graph.cancelled is True
    cancelled = dict(events_of(redis))["run.cancelled"]
    assert "wall-clock budget" in cancelled["reason"]
    assert cancelled["cancelled_by"] == "system"


def test_resume_re_enters_without_a_seed_state(wired) -> None:
    """§5.2: `ainvoke(None, config)` is what picks a run up from its checkpoint."""
    redis, _finished = wired
    graph = FakeGraph()
    run(jobs.resume_run({"redis": redis, "graph": graph}, RUN_ID))

    seed, config = graph.calls[0]
    assert seed is None
    assert config["configurable"]["thread_id"] == RUN_ID
    assert dict(events_of(redis))["run.started"]["resumed"] is True


def test_a_fresh_run_seeds_the_state_and_binds_the_emitter(wired) -> None:
    redis, _finished = wired
    graph = FakeGraph()
    run(jobs.execute_run({"redis": redis, "graph": graph}, RUN_ID))

    seed, config = graph.calls[0]
    assert seed["run_id"] == RUN_ID
    assert seed["thread_id"] == RUN_ID
    assert seed["prompt"] == "Train a classifier."
    # The nodes resolve their emitter from here (`engine/events.emitter_from_config`).
    assert config["configurable"]["emitter"].run_id == RUN_ID


def test_the_summary_hash_tracks_the_run(wired) -> None:
    """The hot snapshot a newly connected WebSocket reads before any replay (§9.3)."""
    redis, _finished = wired
    run(jobs.execute_run({"redis": redis, "graph": FakeGraph()}, RUN_ID))
    summary = redis.hashes[redis_layer.summary_key(RUN_ID)]
    assert summary["worker_id"]
    assert summary["status"] == "COMPLETED"
    assert summary["outcome"] == "SUCCEEDED"
    assert summary["phase"] == "COMPLETE"


def test_execute_run_requires_a_run_id() -> None:
    with pytest.raises(ValueError, match="requires a run_id"):
        run(jobs.execute_run({}, None))


def test_worker_identity_names_the_host_and_process() -> None:
    identity = jobs.worker_identity()
    assert "-" in identity and identity.rsplit("-", 1)[1].isdigit()
