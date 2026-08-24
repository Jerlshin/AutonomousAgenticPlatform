"""The Redis layer: the keyspace, stream primitives and the run lock (ARCHITECTURE.md §7.2, §5.4).

Three properties are worth testing here and the rest is `redis-py`'s problem:

* `seq` is gapless and strictly increasing, and allocated *before* the `XADD`. Everything
  in §9 — resume, gap detection, ordering — rests on that one invariant.
* replay filters on `seq`, not on the stream id, and reports the oldest entry it still
  holds so a caller can tell a short replay from a complete one.
* the lock releases and renews on *ownership*, not on possession of the key name. That is
  the difference between "the reaper handed my run to someone else" being a no-op and it
  being two workers on one run.
"""

from __future__ import annotations

import json

import pytest

from app.core import redis as redis_layer
from tests.fakes import FakeRedis, run


def test_keyspace_matches_the_specification() -> None:
    """§7.2's table, verbatim. These strings are a contract with `redis-cli`."""
    assert redis_layer.events_key("r1") == "run:r1:events"
    assert redis_layer.seq_key("r1") == "run:r1:seq"
    assert redis_layer.summary_key("r1") == "run:r1:summary"
    assert redis_layer.control_channel("r1") == "run:r1:control"
    assert redis_layer.lock_key("r1") == "lock:run:r1"
    assert redis_layer.conns_key("r1") == "ws:conns:r1"
    assert redis_layer.idem_key("k") == "idem:k"
    assert redis_layer.ticket_key("t") == "ws:ticket:t"


def test_seq_starts_at_one_and_increments() -> None:
    client = FakeRedis()
    values = [run(redis_layer.next_seq(client, "r1")) for _ in range(3)]
    assert values == [1, 2, 3]
    # §7.2: per-run keys carry a 24 h TTL, set once when the allocator is created.
    assert client.expiries[redis_layer.seq_key("r1")] == redis_layer.RUN_KEY_TTL_S


def test_seq_is_per_run() -> None:
    client = FakeRedis()
    assert run(redis_layer.next_seq(client, "a")) == 1
    assert run(redis_layer.next_seq(client, "b")) == 1


def _append(
    client: FakeRedis, run_id: str, seq: int, event_type: str, **payload
) -> None:
    run(
        redis_layer.append_event(
            client,
            run_id,
            seq=seq,
            event_type=event_type,
            ts="2026-08-24T10:00:00.000Z",
            payload_json=json.dumps(payload),
        )
    )


def test_backlog_filters_on_seq_and_reports_the_oldest_entry() -> None:
    client = FakeRedis()
    for seq in range(1, 6):
        _append(client, "r1", seq, "node.started", node=f"n{seq}")

    events, cursor, oldest = run(redis_layer.read_backlog(client, "r1", after_seq=3))
    assert [e["seq"] for e in events] == [4, 5]
    assert oldest == 1
    # The cursor is the *stream's* last id, not the last forwarded one: the live tail must
    # resume after everything already read, including the entries the filter dropped.
    assert cursor == "5-0"


def test_backlog_of_an_empty_stream_is_not_an_error() -> None:
    events, cursor, oldest = run(redis_layer.read_backlog(FakeRedis(), "r1"))
    assert events == []
    assert oldest is None
    assert cursor == "0-0"


def test_trimmed_history_is_detectable_from_the_oldest_seq() -> None:
    """A cursor older than retention is what `replay.gap` is for (§9.3)."""
    client = FakeRedis()
    for seq in range(40, 45):
        _append(client, "r1", seq, "token.delta", text="x")

    _events, _cursor, oldest = run(redis_layer.read_backlog(client, "r1", after_seq=10))
    assert oldest == 40
    assert oldest > 10 + 1  # the condition the WebSocket layer tests


def test_decode_entry_survives_a_malformed_payload() -> None:
    """A bad entry degrades to an empty payload rather than failing the whole replay."""
    decoded = redis_layer.decode_entry(
        "7-0", {"v": "1", "seq": "nope", "type": "node.started", "payload": "{oops"}
    )
    assert decoded["seq"] == 0
    assert decoded["payload"] == {}
    assert decoded["type"] == "node.started"


def test_live_read_returns_only_entries_after_the_cursor() -> None:
    client = FakeRedis()
    _append(client, "r1", 1, "run.started")
    _append(client, "r1", 2, "node.started", node="planner")

    events, cursor = run(redis_layer.read_live(client, "r1", last_id="1-0"))
    assert [e["seq"] for e in events] == [2]
    assert cursor == "2-0"

    idle, unchanged = run(redis_layer.read_live(client, "r1", last_id=cursor))
    assert idle == []
    assert unchanged == cursor


def test_summary_round_trips_and_merges() -> None:
    client = FakeRedis()
    run(redis_layer.write_summary(client, "r1", {"status": "RUNNING"}))
    run(redis_layer.write_summary(client, "r1", {"phase": "EXECUTE"}))
    assert run(redis_layer.read_summary(client, "r1")) == {
        "status": "RUNNING",
        "phase": "EXECUTE",
    }


def test_publish_control_reports_the_subscriber_count() -> None:
    """Zero subscribers is the signal the cancel endpoint branches on."""
    client = FakeRedis()
    assert run(redis_layer.publish_control(client, "r1", {"op": "cancel"})) == 0
    client.subscribers[redis_layer.control_channel("r1")] = 1
    assert run(redis_layer.publish_control(client, "r1", {"op": "cancel"})) == 1
    assert client.channels[redis_layer.control_channel("r1")][-1] == {"op": "cancel"}


# ------------------------------------------------------------------------------------
#  RunLock
# ------------------------------------------------------------------------------------


def test_only_one_worker_acquires_a_run() -> None:
    client = FakeRedis()
    first = redis_layer.RunLock(client, "r1", "worker-a")
    second = redis_layer.RunLock(client, "r1", "worker-b")

    assert run(first.acquire()) is True
    assert run(second.acquire()) is False
    assert run(second.owner()) == "worker-a"


def test_release_is_compare_and_delete() -> None:
    """A worker whose lease lapsed must not delete the new owner's lock (§5.4)."""
    client = FakeRedis()
    stale = redis_layer.RunLock(client, "r1", "worker-a")
    run(stale.acquire())

    # The lease expires and another worker legitimately takes the run.
    client.strings[redis_layer.lock_key("r1")] = "worker-b"

    assert run(stale.release()) is False
    assert client.strings[redis_layer.lock_key("r1")] == "worker-b"


def test_release_by_the_owner_frees_the_run() -> None:
    client = FakeRedis()
    lock = redis_layer.RunLock(client, "r1", "worker-a")
    run(lock.acquire())
    assert run(lock.release()) is True
    assert redis_layer.lock_key("r1") not in client.strings
    assert run(redis_layer.RunLock(client, "r1", "worker-b").acquire()) is True


def test_renew_extends_only_the_owner_s_lease() -> None:
    client = FakeRedis()
    lock = redis_layer.RunLock(client, "r1", "worker-a", ttl_s=900)
    run(lock.acquire())
    assert run(lock.renew()) is True
    assert client.expiries[redis_layer.lock_key("r1")] == 900

    client.strings[redis_layer.lock_key("r1")] = "worker-b"
    assert run(lock.renew()) is False


def test_context_manager_raises_when_the_run_is_taken() -> None:
    client = FakeRedis()
    run(redis_layer.RunLock(client, "r1", "worker-a").acquire())

    async def attempt() -> None:
        async with redis_layer.RunLock(client, "r1", "worker-b"):
            pass

    with pytest.raises(redis_layer.LockNotAcquired) as excinfo:
        run(attempt())
    assert excinfo.value.owner == "worker-a"
