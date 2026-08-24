"""The `/runs` endpoints and run dispatch (ARCHITECTURE.md §8.2, §5.2).

Two behaviours here are worth more than the rest. First, a run body is a *merge* of two
stores — the durable `tasks` row and the volatile `run:{id}:summary` hash — and the merge
has to be the same one the WebSocket's `run.snapshot` uses, or a client that resynchronises
after a gap ends up disagreeing with a client that polls. Second, cancel is a *signal*: it
writes the row only when nobody is executing the run, because the worker that is executing
it owns that write and racing it would mean two writers for one row.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.v1.runs import get_run_redis
from app.core import redis as redis_layer
from app.core.db import get_db
from app.db.models.task import Task, TaskStatus
from app.main import app
from tests.fakes import FakeRedis

RUN_ID = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"


def make_task(status: TaskStatus = TaskStatus.RUNNING) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=uuid.UUID(RUN_ID),
        title="Breast cancer classifier",
        prompt="Train a classifier and reach 95% accuracy.",
        status=status,
        created_at=now,
        updated_at=now,
    )


class FakeSession:
    def __init__(self, rows: dict[uuid.UUID, Task]) -> None:
        self.rows = rows
        self.commits = 0

    async def get(self, _model, key):  # noqa: ANN001 - mirrors AsyncSession.get
        return self.rows.get(key)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    redis = FakeRedis()
    rows = {uuid.UUID(RUN_ID): make_task()}
    session = FakeSession(rows)

    app.dependency_overrides[get_run_redis] = lambda: redis
    app.dependency_overrides[get_db] = lambda: session
    monkeypatch.setattr(redis_layer, "get_redis", lambda: redis)
    try:
        yield TestClient(app), redis, session
    finally:
        app.dependency_overrides.clear()


def append(redis: FakeRedis, seq: int, event_type: str, **payload) -> None:
    redis.streams.setdefault(redis_layer.events_key(RUN_ID), []).append(
        (
            f"{seq}-0",
            {
                "v": "1",
                "seq": str(seq),
                "type": event_type,
                "ts": "2026-08-24T10:00:00.000Z",
                "payload": json.dumps(payload),
            },
        )
    )
    redis.strings[redis_layer.seq_key(RUN_ID)] = str(seq)


# ------------------------------------------------------------------------------------
#  Run detail
# ------------------------------------------------------------------------------------


def test_run_detail_merges_postgres_with_the_redis_summary(wired) -> None:
    client, redis, _session = wired
    redis.hashes[redis_layer.summary_key(RUN_ID)] = {
        "status": "RUNNING",
        "phase": "EXECUTE",
        "current_node": "sandbox_exec",
        "percent": "62",
        "worker_id": "worker-1",
        "tokens_in": "31000",
        "tokens_out": "10000",
    }
    append(redis, 44, "node.started", node="sandbox_exec")

    body = client.get(f"/api/v1/runs/{RUN_ID}").json()

    assert body["status"] == "RUNNING"  # the durable column
    assert body["phase"] == "EXECUTE"  # the volatile hash
    assert body["current_node"] == "sandbox_exec"
    assert body["percent"] == 62.0
    assert body["tokens_in"] == 31000
    assert body["last_seq"] == 44
    assert body["ws_url"] == f"/api/v1/ws/runs/{RUN_ID}"
    assert body["title"] == "Breast cancer classifier"


def test_run_detail_works_with_no_summary_at_all(wired) -> None:
    """A run enqueued a millisecond ago has a row and nothing in Redis yet."""
    client, _redis, _session = wired
    body = client.get(f"/api/v1/runs/{RUN_ID}").json()
    assert body["phase"] is None
    assert body["last_seq"] == 0
    assert body["tokens_in"] == 0


def test_an_unknown_run_is_404(wired) -> None:
    client, _redis, _session = wired
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/runs/{missing}").status_code == 404


# ------------------------------------------------------------------------------------
#  Event backlog
# ------------------------------------------------------------------------------------


def test_the_event_backlog_is_the_same_history_the_socket_replays(wired) -> None:
    client, redis, _session = wired
    append(redis, 1, "run.queued", position=0)
    append(redis, 2, "node.started", node="planner")
    append(redis, 3, "node.completed", node="planner", duration_ms=6120)

    body = client.get(f"/api/v1/runs/{RUN_ID}/events?after_seq=1").json()

    assert body["total"] == 2
    assert [e["seq"] for e in body["events"]] == [2, 3]
    assert body["events"][1]["payload"]["duration_ms"] == 6120
    assert body["last_seq"] == 3
    assert body["gap"] is False


def test_a_backlog_older_than_the_cursor_reports_a_gap(wired) -> None:
    client, redis, _session = wired
    append(redis, 40, "node.started", node="coder")

    body = client.get(f"/api/v1/runs/{RUN_ID}/events?after_seq=5").json()

    assert body["gap"] is True
    assert body["oldest_available"] == 40


# ------------------------------------------------------------------------------------
#  Cancel
# ------------------------------------------------------------------------------------


def test_cancel_signals_the_worker_and_does_not_write_the_row(wired) -> None:
    client, redis, session = wired
    redis.subscribers[redis_layer.control_channel(RUN_ID)] = 1

    response = client.post(
        f"/api/v1/runs/{RUN_ID}/cancel", json={"reason": "wrong dataset"}
    )

    assert response.status_code == 202
    assert response.json()["data"]["delivered_to"] == 1
    assert redis.channels[redis_layer.control_channel(RUN_ID)] == [
        {"op": "cancel", "reason": "wrong dataset"}
    ]
    # The worker owns the terminal write; the endpoint must not race it.
    assert session.rows[uuid.UUID(RUN_ID)].status is TaskStatus.RUNNING
    assert session.commits == 0


def test_cancelling_a_run_nobody_holds_writes_the_row_and_emits_the_event(
    wired,
) -> None:
    client, redis, session = wired

    response = client.post(f"/api/v1/runs/{RUN_ID}/cancel", json={})

    assert response.status_code == 202
    assert response.json()["data"]["delivered_to"] == 0
    assert session.rows[uuid.UUID(RUN_ID)].status is TaskStatus.CANCELLED
    entries = redis.streams[redis_layer.events_key(RUN_ID)]
    assert [f["type"] for _id, f in entries] == ["run.cancelled"]


def test_cancelling_a_terminal_run_is_409(wired) -> None:
    client, _redis, session = wired
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.COMPLETED
    assert client.post(f"/api/v1/runs/{RUN_ID}/cancel", json={}).status_code == 409


# ------------------------------------------------------------------------------------
#  Resume
# ------------------------------------------------------------------------------------


def test_resume_refuses_a_run_that_is_not_resumable(wired) -> None:
    client, _redis, session = wired
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.COMPLETED
    response = client.post(f"/api/v1/runs/{RUN_ID}/resume")
    assert response.status_code == 409
    assert "only" in response.json()["detail"]


def test_resume_refuses_while_a_live_lock_is_held(wired) -> None:
    """The reaper and a slow worker can disagree for one tick; the lock is more current."""
    client, redis, session = wired
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.INTERRUPTED
    redis.strings[redis_layer.lock_key(RUN_ID)] = "worker-a"

    response = client.post(f"/api/v1/runs/{RUN_ID}/resume")

    assert response.status_code == 409
    assert "execution lock" in response.json()["detail"]


def test_resume_re_enqueues_an_interrupted_run(wired, monkeypatch) -> None:
    client, _redis, session = wired
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.INTERRUPTED

    enqueued: list[tuple[str, bool]] = []

    async def fake_enqueue(run_id: str, *, resume: bool = False, **_kw):
        enqueued.append((run_id, resume))
        return "job-1"

    from app.worker import queue

    monkeypatch.setattr(queue, "enqueue_run", fake_enqueue)

    response = client.post(f"/api/v1/runs/{RUN_ID}/resume")

    assert response.status_code == 202
    assert enqueued == [(RUN_ID, True)]
    assert session.rows[uuid.UUID(RUN_ID)].status is TaskStatus.PENDING


# ------------------------------------------------------------------------------------
#  Approve
# ------------------------------------------------------------------------------------


def test_approve_reports_plainly_when_no_worker_is_waiting(wired) -> None:
    """A 202 that means nothing is worse than a 409 that tells the operator the truth."""
    client, _redis, _session = wired
    response = client.post(
        f"/api/v1/runs/{RUN_ID}/approve",
        json={"gate": "before_train", "decision": "approve"},
    )
    assert response.status_code == 409


def test_approve_delivers_the_decision_to_a_waiting_worker(wired) -> None:
    client, redis, _session = wired
    redis.subscribers[redis_layer.control_channel(RUN_ID)] = 1

    response = client.post(
        f"/api/v1/runs/{RUN_ID}/approve",
        json={"gate": "before_train", "decision": "reject", "notes": "wrong split"},
    )

    assert response.status_code == 202
    assert redis.channels[redis_layer.control_channel(RUN_ID)] == [
        {
            "op": "approve",
            "gate": "before_train",
            "decision": "reject",
            "notes": "wrong split",
        }
    ]


def test_approve_validates_the_decision_vocabulary(wired) -> None:
    client, _redis, _session = wired
    response = client.post(
        f"/api/v1/runs/{RUN_ID}/approve", json={"gate": "g", "decision": "maybe"}
    )
    assert response.status_code == 422


# ------------------------------------------------------------------------------------
#  Dispatch
# ------------------------------------------------------------------------------------


@pytest.fixture
def dispatch(wired, monkeypatch):
    """`wired`, plus a stubbed arq enqueue so no queue is needed."""
    client, redis, session = wired
    enqueued: list[str] = []

    async def fake_enqueue(run_id: str, **_kw):
        enqueued.append(run_id)
        return f"job-{len(enqueued)}"

    async def fake_depth(*_a, **_k):
        return 3

    from app.worker import queue

    monkeypatch.setattr(queue, "enqueue_run", fake_enqueue)
    monkeypatch.setattr(queue, "queue_depth", fake_depth)
    return client, redis, session, enqueued


def test_dispatch_enqueues_and_announces_the_run_as_queued(dispatch) -> None:
    client, redis, session, enqueued = dispatch
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.COMPLETED

    response = client.post(f"/api/v1/tasks/{RUN_ID}/runs")

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == RUN_ID
    assert body["job_id"] == "job-1"
    assert body["ws_url"] == f"/api/v1/ws/runs/{RUN_ID}"
    assert enqueued == [RUN_ID]
    assert session.rows[uuid.UUID(RUN_ID)].status is TaskStatus.PENDING

    # §5.2: `run.queued` is seq 1, emitted by the API so a browser that connects
    # immediately after the 202 sees something before a worker exists.
    entries = redis.streams[redis_layer.events_key(RUN_ID)]
    assert [f["type"] for _id, f in entries] == ["run.queued"]
    assert json.loads(entries[0][1]["payload"]) == {"position": 3}


def test_dispatching_a_task_with_a_run_in_progress_is_409(dispatch) -> None:
    client, _redis, _session, enqueued = dispatch
    response = client.post(f"/api/v1/tasks/{RUN_ID}/runs")
    assert response.status_code == 409
    assert enqueued == []


def test_an_idempotency_key_replays_the_original_response(dispatch) -> None:
    """§8.1: a retried request must not start a second run."""
    client, _redis, session, enqueued = dispatch
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.COMPLETED
    headers = {"Idempotency-Key": "abc-123"}

    first = client.post(f"/api/v1/tasks/{RUN_ID}/runs", headers=headers).json()
    session.rows[uuid.UUID(RUN_ID)].status = TaskStatus.COMPLETED
    second = client.post(f"/api/v1/tasks/{RUN_ID}/runs", headers=headers).json()

    assert first == second
    assert enqueued == [RUN_ID]


def test_dispatching_an_unknown_task_is_404(dispatch) -> None:
    client, _redis, _session, _enqueued = dispatch
    assert client.post(f"/api/v1/tasks/{uuid.uuid4()}/runs").status_code == 404


def test_listing_runs_for_a_task_returns_the_same_projection(dispatch) -> None:
    client, redis, _session, _enqueued = dispatch
    redis.hashes[redis_layer.summary_key(RUN_ID)] = {"phase": "REPORT"}

    body = client.get(f"/api/v1/tasks/{RUN_ID}/runs").json()

    assert body["total"] == 1
    assert body["runs"][0]["phase"] == "REPORT"
    assert body["runs"][0]["run_id"] == RUN_ID
