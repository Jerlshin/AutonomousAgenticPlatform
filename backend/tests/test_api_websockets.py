"""The `pluton.v1` WebSocket endpoint (ARCHITECTURE.md §9).

Driven through `TestClient` against `FakeRedis`, which is what makes the ordering
assertions meaningful: the frames a client receives are produced by the real replay
filter, the real envelope constructor and the real cursor arithmetic over a real stream.

Runs in these tests carry a terminal event in their backlog wherever possible. That is not
incidental — it is how the endpoint is made deterministic to test. A run that has finished
drains its backlog and closes, so the assertion is over a complete, finite frame sequence
rather than over whatever happened to arrive before the test gave up waiting.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import websockets as ws_module
from app.api.v1.runs import get_run_redis
from app.core import redis as redis_layer
from app.core.db import get_db
from app.db.models.task import Task, TaskStatus
from app.main import app
from app.schemas.events import PROTOCOL, CloseCode
from tests.fakes import FakeRedis, run as run_coro

RUN_ID = "11111111-2222-3333-4444-555555555555"


def make_task(run_id: str = RUN_ID, status: TaskStatus = TaskStatus.RUNNING) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=uuid.UUID(run_id),
        title="Breast cancer classifier",
        prompt="Train a classifier and reach 95% accuracy.",
        status=status,
        created_at=now,
        updated_at=now,
    )


class FakeSession:
    """Enough `AsyncSession` for `session.get(Task, uuid)`."""

    def __init__(self, rows: dict[uuid.UUID, Task]) -> None:
        self.rows = rows

    async def get(self, _model, key):  # noqa: ANN001 - mirrors AsyncSession.get
        return self.rows.get(key)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


def append(
    client: FakeRedis, run_id: str, seq: int, event_type: str, **payload
) -> None:
    client.streams.setdefault(redis_layer.events_key(run_id), []).append(
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
    client.strings[redis_layer.seq_key(run_id)] = str(seq)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """A `TestClient` whose Redis, database and ticket store are all fakes."""
    redis = FakeRedis()
    rows = {uuid.UUID(RUN_ID): make_task()}

    def factory():
        return FakeSession(rows)

    app.dependency_overrides[get_run_redis] = lambda: redis
    app.dependency_overrides[get_db] = lambda: FakeSession(rows)
    monkeypatch.setattr(ws_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(redis_layer, "get_redis", lambda: redis)
    try:
        # Deliberately not `with TestClient(app)`: entering the context manager runs the
        # application lifespan, which probes a Postgres that no unit test has, and pays
        # that connection timeout once per test. The portal a bare client creates per
        # request is enough for both HTTP and WebSocket calls.
        yield TestClient(app), redis, rows
    finally:
        app.dependency_overrides.clear()


def drain(socket) -> list[dict]:
    """Every frame until the server closes.

    Only safe against a run whose backlog already contains a terminal event: that is what
    makes the server close rather than sit in a live tail, and `receive_json` on a closed
    socket raises instead of blocking. Tests of a *live* run read an exact frame count
    with `read` instead.
    """
    frames: list[dict] = []
    for _ in range(200):
        try:
            frames.append(socket.receive_json())
        except Exception:
            break
    return frames


def read(socket, count: int) -> list[dict]:
    """Exactly `count` frames. For live runs, where nothing will ever close the socket."""
    return [socket.receive_json() for _ in range(count)]


# ------------------------------------------------------------------------------------
#  Tickets
# ------------------------------------------------------------------------------------


def test_ticket_is_single_use_and_run_scoped(wired) -> None:
    client, redis, _rows = wired
    response = client.post("/api/v1/ws/tickets", json={"run_id": RUN_ID})
    assert response.status_code == 200

    body = response.json()
    assert body["run_id"] == RUN_ID
    assert body["expires_in"] == redis_layer.TICKET_TTL_S
    assert body["ws_url"].startswith(f"/api/v1/ws/runs/{RUN_ID}?ticket=")
    assert redis.strings[redis_layer.ticket_key(body["ticket"])] == RUN_ID


def test_ticket_for_an_unknown_run_is_404(wired) -> None:
    client, _redis, _rows = wired
    response = client.post(
        "/api/v1/ws/tickets", json={"run_id": "99999999-9999-9999-9999-999999999999"}
    )
    assert response.status_code == 404


def test_ticket_request_rejects_a_non_uuid(wired) -> None:
    client, _redis, _rows = wired
    assert (
        client.post("/api/v1/ws/tickets", json={"run_id": "not-a-uuid"}).status_code
        == 422
    )


# ------------------------------------------------------------------------------------
#  Handshake and replay
# ------------------------------------------------------------------------------------


def test_hello_then_replay_then_terminal_close(wired) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.queued", position=0)
    append(redis, RUN_ID, 2, "node.started", node="planner")
    append(redis, RUN_ID, 3, "run.completed", status="SUCCEEDED", deliverables=[])
    redis.hashes[redis_layer.summary_key(RUN_ID)] = {"status": "COMPLETED"}

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
    ) as socket:
        frames = drain(socket)

    assert [f["type"] for f in frames] == [
        "hello",
        "run.queued",
        "node.started",
        "run.completed",
        "replay.complete",
    ]
    hello = frames[0]
    assert hello["seq"] == 0  # §9.2: control frames carry seq 0
    assert hello["payload"]["protocol"] == PROTOCOL
    assert hello["payload"]["last_seq"] == 3
    assert hello["payload"]["run"] == {"status": "COMPLETED"}
    assert [f["seq"] for f in frames[1:4]] == [1, 2, 3]
    assert all(f["v"] == 1 and f["run_id"] == RUN_ID for f in frames)
    assert frames[-1]["payload"]["through_seq"] == 3


def test_after_seq_replays_only_what_the_client_is_missing(wired) -> None:
    client, redis, _rows = wired
    for seq, kind in enumerate(
        ["run.queued", "run.started", "node.started", "run.completed"], start=1
    ):
        append(redis, RUN_ID, seq, kind)

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}?after_seq=2", subprotocols=[PROTOCOL]
    ) as socket:
        frames = drain(socket)

    assert [f["type"] for f in frames] == [
        "hello",
        "node.started",
        "run.completed",
        "replay.complete",
    ]


def test_a_cursor_older_than_retention_gets_a_gap_and_a_snapshot(wired) -> None:
    """§9.3: resynchronise from authoritative state rather than silently miss history."""
    client, redis, _rows = wired
    append(redis, RUN_ID, 40, "node.started", node="coder")
    append(redis, RUN_ID, 41, "run.completed", status="SUCCEEDED")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}?after_seq=5", subprotocols=[PROTOCOL]
    ) as socket:
        frames = drain(socket)

    kinds = [f["type"] for f in frames]
    assert kinds[:4] == ["hello", "replay.gap", "run.snapshot", "node.started"]
    gap = frames[1]["payload"]
    assert gap == {"requested_after": 5, "oldest_available": 40}
    snapshot = frames[2]["payload"]
    assert snapshot["run_id"] == RUN_ID
    assert snapshot["title"] == "Breast cancer classifier"
    assert snapshot["last_seq"] == 41


def test_a_contiguous_cursor_produces_no_gap(wired) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.queued")
    append(redis, RUN_ID, 2, "run.completed", status="SUCCEEDED")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}?after_seq=1", subprotocols=[PROTOCOL]
    ) as socket:
        frames = drain(socket)

    assert "replay.gap" not in [f["type"] for f in frames]


def test_an_unknown_run_closes_4404(wired) -> None:
    client, _redis, _rows = wired
    unknown = "99999999-9999-9999-9999-999999999999"
    with pytest.raises(Exception):  # noqa: B017 - starlette raises on the close frame
        with client.websocket_connect(
            f"/api/v1/ws/runs/{unknown}", subprotocols=[PROTOCOL]
        ) as socket:
            socket.receive_json()


def test_a_bad_ticket_closes_4401(wired) -> None:
    client, _redis, _rows = wired
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/api/v1/ws/runs/{RUN_ID}?ticket=forged", subprotocols=[PROTOCOL]
        ) as socket:
            socket.receive_json()


def test_a_valid_ticket_is_consumed_at_accept(wired) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.completed", status="SUCCEEDED")
    ticket = client.post("/api/v1/ws/tickets", json={"run_id": RUN_ID}).json()["ticket"]

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}?ticket={ticket}", subprotocols=[PROTOCOL]
    ) as socket:
        drain(socket)

    assert redis_layer.ticket_key(ticket) not in redis.strings


def test_a_ticket_minted_for_another_run_is_refused(wired) -> None:
    """Scoping is enforced, not merely conventional."""
    client, redis, rows = wired
    other = uuid.uuid4()
    rows[other] = make_task(str(other))
    ticket = client.post("/api/v1/ws/tickets", json={"run_id": str(other)}).json()[
        "ticket"
    ]

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/api/v1/ws/runs/{RUN_ID}?ticket={ticket}", subprotocols=[PROTOCOL]
        ) as socket:
            socket.receive_json()


# ------------------------------------------------------------------------------------
#  Client → server
# ------------------------------------------------------------------------------------


def test_a_binary_frame_closes_4400(wired) -> None:
    """§9: this protocol is UTF-8 JSON, and a binary frame means the peer is not speaking it."""
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "node.started", node="coder")

    with pytest.raises(Exception):  # noqa: B017 - starlette raises on the close frame
        with client.websocket_connect(
            f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
        ) as socket:
            read(socket, 3)  # hello, node.started, replay.complete
            socket.send_bytes(b"\x00\x01")
            socket.receive_json()


def test_an_unknown_message_type_is_answered_and_the_socket_stays_open(wired) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "node.started", node="coder")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
    ) as socket:
        read(socket, 3)
        socket.send_json({"type": "teleport", "payload": {}})
        error = socket.receive_json()

    assert error["type"] == "error"
    assert error["payload"]["code"] == "unknown_message_type"
    assert error["payload"]["recoverable"] is True


def test_cancel_is_published_to_the_control_channel(wired) -> None:
    """The `error` reply to the second message is the barrier that makes this ordered.

    Both frames are handled by one reader task in the order they arrive, so seeing the
    answer to the second proves the first was already processed — no sleep, no polling.
    """
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "node.started", node="coder")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
    ) as socket:
        read(socket, 3)
        socket.send_json(
            {"type": "cancel", "payload": {"reason": "operator changed mind"}}
        )
        socket.send_json({"type": "barrier"})
        assert socket.receive_json()["type"] == "error"

    assert redis.channels.get(redis_layer.control_channel(RUN_ID)) == [
        {"op": "cancel", "reason": "operator changed mind"}
    ]


def test_a_pong_clears_the_missed_heartbeat_count(wired) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "node.started", node="coder")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
    ) as socket:
        read(socket, 3)
        socket.send_json({"type": "pong"})
        socket.send_json({"type": "barrier"})
        assert socket.receive_json()["type"] == "error"


# ------------------------------------------------------------------------------------
#  The client-message handler, unit-driven
# ------------------------------------------------------------------------------------


class StubSocket:
    """A `WebSocket` that only records what was sent to it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def connection(client: FakeRedis) -> ws_module.RunConnection:
    return ws_module.RunConnection(StubSocket(), RUN_ID, client)


def test_subscribe_installs_a_prefix_filter() -> None:
    """§9.5's server-side filter, and §18.3's reason for it: a pane per event family."""
    conn = connection(FakeRedis())
    run_coro(
        ws_module._handle_client_message(
            conn,
            {"type": "subscribe", "payload": {"types": ["node.", "run.completed"]}},
        )
    )
    assert conn.filter == {"node.", "run.completed"}

    run_coro(conn.send_event("node.started", {"node": "planner"}))
    run_coro(conn.send_event("run.completed", {"status": "SUCCEEDED"}))
    run_coro(conn.send_event("token.delta", {"text": "x"}))
    assert [f["type"] for f in conn.ws.sent] == ["node.started", "run.completed"]


def test_subscribe_without_a_list_clears_the_filter() -> None:
    conn = connection(FakeRedis())
    conn.filter = {"node."}
    run_coro(
        ws_module._handle_client_message(conn, {"type": "subscribe", "payload": {}})
    )
    assert conn.filter is None


def test_pong_resets_the_missed_count() -> None:
    conn = connection(FakeRedis())
    conn.missed_pongs = 2
    run_coro(ws_module._handle_client_message(conn, {"type": "pong"}))
    assert conn.missed_pongs == 0


def test_resync_arms_a_snapshot() -> None:
    conn = connection(FakeRedis())
    run_coro(ws_module._handle_client_message(conn, {"type": "resync"}))
    assert conn.resync.is_set()


def test_approve_publishes_the_gate_decision() -> None:
    redis = FakeRedis()
    conn = connection(redis)
    run_coro(
        ws_module._handle_client_message(
            conn,
            {
                "type": "approve",
                "payload": {
                    "gate": "before_train",
                    "decision": "approve",
                    "notes": "ok",
                },
            },
        )
    )
    assert redis.channels[redis_layer.control_channel(RUN_ID)] == [
        {"op": "approve", "gate": "before_train", "decision": "approve", "notes": "ok"}
    ]


# ------------------------------------------------------------------------------------
#  Quota
# ------------------------------------------------------------------------------------


def test_the_ninth_connection_to_one_run_is_refused(wired, monkeypatch) -> None:
    """§9.7's `4429`. Driven at a cap of one so the test does not open eight sockets."""
    client, redis, _rows = wired
    monkeypatch.setattr(ws_module, "MAX_CONNECTIONS_PER_RUN", 1)
    redis.sets[redis_layer.conns_key(RUN_ID)] = {"someone-else"}

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
        ) as socket:
            socket.receive_json()

    # The refused connection must not leave its own id behind against the quota.
    assert redis.sets[redis_layer.conns_key(RUN_ID)] == {"someone-else"}


def test_a_finished_connection_releases_its_slot(wired) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.completed", status="SUCCEEDED")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
    ) as socket:
        drain(socket)

    assert redis.sets.get(redis_layer.conns_key(RUN_ID), set()) == set()


def test_close_codes_match_the_specification() -> None:
    """§9.7. Asserted as a table because these numbers are a client contract."""
    assert (CloseCode.NORMAL, CloseCode.GOING_AWAY, CloseCode.INTERNAL_ERROR) == (
        1000,
        1001,
        1011,
    )
    assert (
        CloseCode.PROTOCOL_ERROR,
        CloseCode.UNAUTHENTICATED,
        CloseCode.FORBIDDEN,
        CloseCode.NOT_FOUND,
        CloseCode.QUOTA_EXCEEDED,
    ) == (4400, 4401, 4403, 4404, 4429)


# ------------------------------------------------------------------------------------
#  First-frame authentication  (§9.3's second mechanism)
# ------------------------------------------------------------------------------------


@pytest.fixture
def with_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """A configured `PLATFORM_API_TOKEN`, which is what turns auth on at all."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "PLATFORM_API_TOKEN", "s3cret", raising=False)
    return "s3cret"


def test_first_frame_auth_admits_a_correct_token(wired, with_token) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.completed", status="SUCCEEDED")

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
    ) as socket:
        socket.send_json({"type": "auth", "payload": {"token": with_token}})
        frames = drain(socket)

    assert [f["type"] for f in frames] == ["hello", "run.completed", "replay.complete"]


def test_first_frame_auth_rejects_a_wrong_token(wired, with_token) -> None:
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.completed", status="SUCCEEDED")

    with pytest.raises(Exception):  # noqa: B017 - starlette raises on the close frame
        with client.websocket_connect(
            f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
        ) as socket:
            socket.send_json({"type": "auth", "payload": {"token": "wrong"}})
            socket.receive_json()


def test_a_non_auth_first_frame_is_rejected(wired, with_token) -> None:
    """The first frame must be `auth` — anything else means the client skipped the step."""
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.completed", status="SUCCEEDED")

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/api/v1/ws/runs/{RUN_ID}", subprotocols=[PROTOCOL]
        ) as socket:
            socket.send_json({"type": "subscribe", "payload": {"types": []}})
            socket.receive_json()


def test_a_ticket_works_even_when_a_token_is_configured(wired, with_token) -> None:
    """The ticket is the whole point: a browser cannot send an `Authorization` header."""
    client, redis, _rows = wired
    append(redis, RUN_ID, 1, "run.completed", status="SUCCEEDED")
    ticket = client.post(
        "/api/v1/ws/tickets",
        json={"run_id": RUN_ID},
        headers={"Authorization": f"Bearer {with_token}"},
    ).json()["ticket"]

    with client.websocket_connect(
        f"/api/v1/ws/runs/{RUN_ID}?ticket={ticket}", subprotocols=[PROTOCOL]
    ) as socket:
        assert socket.receive_json()["type"] == "hello"


def test_minting_a_ticket_requires_the_bearer_token(wired, with_token) -> None:
    client, _redis, _rows = wired
    assert client.post("/api/v1/ws/tickets", json={"run_id": RUN_ID}).status_code == 401
