"""The reapers (ARCHITECTURE.md §5.3, §5.4).

Each of these catches a failure that leaves no error anywhere — a row that quietly stops
being true — so the assertions are about *detection*, not about handling. The detection
rule for an interrupted run is one line and has no timeout in it: `status = RUNNING` with
no `lock:run:{id}` in Redis. That is what these tests pin down, because the tempting wrong
version — "RUNNING and not updated for N minutes" — has to guess how long a legitimate
15-minute training node may take.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core import redis as redis_layer
from app.db.models.task import Task, TaskStatus
from app.worker import cron
from tests.fakes import FakeRedis, run


def make_task(status: TaskStatus = TaskStatus.RUNNING) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=uuid.uuid4(),
        title="t",
        prompt="p",
        status=status,
        created_at=now,
        updated_at=now,
    )


class FakeResult:
    def __init__(self, rows: list[Any], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Enough `AsyncSession` for the reapers' one `select` and one conditional `update`."""

    def __init__(self, rows: list[Task], *, rowcount: int = 1) -> None:
        self.rows = rows
        self.rowcount = rowcount
        self.updates: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> FakeResult:
        name = type(statement).__name__
        if name == "Update":
            self.updates.append(statement)
            return FakeResult([], rowcount=self.rowcount)
        return FakeResult(self.rows)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.fixture
def session_of(monkeypatch: pytest.MonkeyPatch):
    def install(rows: list[Task], *, rowcount: int = 1) -> FakeSession:
        session = FakeSession(rows, rowcount=rowcount)
        monkeypatch.setattr(cron, "AsyncSessionLocal", lambda: session)
        return session

    return install


def events_of(redis: FakeRedis, run_id: str) -> list[tuple[str, dict]]:
    return [
        (fields["type"], json.loads(fields["payload"]))
        for _id, fields in redis.streams.get(redis_layer.events_key(run_id), [])
    ]


# ------------------------------------------------------------------------------------
#  reap_interrupted_runs
# ------------------------------------------------------------------------------------


def test_a_running_run_with_no_lock_is_marked_interrupted(session_of) -> None:
    redis = FakeRedis()
    task = make_task()
    session = session_of([task])

    assert run(cron.reap_interrupted_runs({"redis": redis})) == 1
    assert session.commits == 1
    # A UI watching the stream is told, rather than left spinning on a worker that is gone.
    kinds = [k for k, _p in events_of(redis, str(task.id))]
    assert kinds == ["run.failed"]
    payload = dict(events_of(redis, str(task.id)))["run.failed"]
    assert payload["status"] == "INTERRUPTED"
    assert payload["resumable"] is True
    assert (
        redis.hashes[redis_layer.summary_key(str(task.id))]["status"] == "INTERRUPTED"
    )


def test_a_running_run_that_still_holds_its_lock_is_left_alone(session_of) -> None:
    """The lock is the liveness signal; a live worker renews it every 60 s."""
    redis = FakeRedis()
    task = make_task()
    redis.strings[redis_layer.lock_key(str(task.id))] = "worker-a"
    session = session_of([task])

    assert run(cron.reap_interrupted_runs({"redis": redis})) == 0
    assert session.updates == []
    assert events_of(redis, str(task.id)) == []


def test_a_run_reclaimed_between_the_lock_read_and_the_write_is_not_reaped(
    session_of,
) -> None:
    """The conditional `UPDATE` affecting zero rows means a worker got there first."""
    redis = FakeRedis()
    task = make_task()
    session_of([task], rowcount=0)

    assert run(cron.reap_interrupted_runs({"redis": redis})) == 0
    assert events_of(redis, str(task.id)) == []


def test_an_unreadable_lock_skips_the_run_rather_than_reaping_it(session_of) -> None:
    """A Redis blip must not be read as "the worker died"."""

    class BrokenRedis(FakeRedis):
        async def exists(self, key: str) -> int:
            raise ConnectionError("redis is down")

    task = make_task()
    session = session_of([task])
    assert run(cron.reap_interrupted_runs({"redis": BrokenRedis()})) == 0
    assert session.updates == []


# ------------------------------------------------------------------------------------
#  reap_sandbox_containers
# ------------------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, run_id: str | None, container_id: str = "c1") -> None:
        self.labels = {cron.RUN_LABEL: run_id} if run_id else {}
        self.id = container_id
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = force or True


class FakeDocker:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self.containers = _Containers(containers)


class _Containers:
    def __init__(self, items: list[FakeContainer]) -> None:
        self.items = items

    def list(
        self, all: bool = False, filters: dict | None = None
    ) -> list[FakeContainer]:
        return self.items


def test_a_container_for_a_terminal_run_is_removed(monkeypatch) -> None:
    task = make_task(TaskStatus.COMPLETED)
    container = FakeContainer(str(task.id))
    monkeypatch.setattr(
        cron, "AsyncSessionLocal", lambda: _RowsSession({task.id: task.status})
    )

    assert run(cron.reap_sandbox_containers({"docker": FakeDocker([container])})) == 1
    assert container.removed is True


def test_a_container_for_a_live_run_is_left_alone(monkeypatch) -> None:
    """Killing one of these would abort a training job on the strength of a scan race."""
    task = make_task(TaskStatus.RUNNING)
    container = FakeContainer(str(task.id))
    monkeypatch.setattr(
        cron, "AsyncSessionLocal", lambda: _RowsSession({task.id: task.status})
    )

    assert run(cron.reap_sandbox_containers({"docker": FakeDocker([container])})) == 0
    assert container.removed is False


def test_a_container_whose_run_no_longer_exists_is_an_orphan(monkeypatch) -> None:
    container = FakeContainer(str(uuid.uuid4()))
    monkeypatch.setattr(cron, "AsyncSessionLocal", lambda: _RowsSession({}))

    assert run(cron.reap_sandbox_containers({"docker": FakeDocker([container])})) == 1
    assert container.removed is True


def test_an_unlabelled_container_is_ignored(monkeypatch) -> None:
    container = FakeContainer(None)
    monkeypatch.setattr(cron, "AsyncSessionLocal", lambda: _RowsSession({}))
    assert run(cron.reap_sandbox_containers({"docker": FakeDocker([container])})) == 0


class _RowsSession:
    """Answers the reaper's `select(Task.id, Task.status).where(id.in_(...))`."""

    def __init__(self, rows: dict[uuid.UUID, TaskStatus]) -> None:
        self.rows = rows

    async def execute(self, _statement: Any) -> Any:
        pairs = list(self.rows.items())

        class Result:
            def all(self) -> list[tuple[uuid.UUID, TaskStatus]]:
                return pairs

        return Result()

    async def __aenter__(self) -> _RowsSession:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


# ------------------------------------------------------------------------------------
#  trim_event_streams
# ------------------------------------------------------------------------------------


def test_streams_are_trimmed_back_to_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(redis_layer, "EVENT_STREAM_MAXLEN", 3)
    redis = FakeRedis()
    for seq in range(10):
        redis.streams.setdefault(redis_layer.events_key("r1"), []).append(
            (f"{seq}-0", {"seq": str(seq), "type": "token.delta", "payload": "{}"})
        )
    redis.strings["unrelated:key"] = "x"

    assert run(cron.trim_event_streams({"redis": redis})) == 7
    assert len(redis.streams[redis_layer.events_key("r1")]) == 3
    assert "unrelated:key" in redis.strings


def test_trimming_an_empty_database_is_a_no_op() -> None:
    assert run(cron.trim_event_streams({"redis": FakeRedis()})) == 0
