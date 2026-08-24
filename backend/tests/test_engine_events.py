"""The event emitter: sequencing, coalescing and the sandbox line streamer (§9.1, §9.2).

The coalescing rule ("80 ms or 64 characters, whichever first") and the ordering rule
("a non-token event flushes the buffer first") are the two things that make the console
readable, and both are invisible until they break: without the first the UI drowns, and
without the second a `node.completed` renders above the tokens the node produced.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core import redis as redis_layer
from app.engine.events import (
    NullEmitter,
    RunEmitter,
    SandboxLineStreamer,
    current_emitter,
    emit,
    emit_token,
    emitter_from_config,
    reset_emitter,
    set_current_node,
    set_emitter,
    truncate_line,
)
from app.schemas.events import EventType
from tests.fakes import FakeRedis, run


def stream_of(client: FakeRedis, run_id: str = "r1") -> list[dict]:
    """The durable log as decoded envelopes, in stream order."""
    return [
        {**fields, "payload": json.loads(fields["payload"])}
        for _id, fields in client.streams.get(redis_layer.events_key(run_id), [])
    ]


def test_events_are_sequenced_from_one() -> None:
    client = FakeRedis()
    emitter = RunEmitter("r1", client)

    seqs = [
        run(emitter.emit(EventType.RUN_STARTED, {"worker_id": "w1"})),
        run(emitter.emit(EventType.NODE_STARTED, {"node": "planner"})),
    ]
    assert seqs == [1, 2]

    entries = stream_of(client)
    assert [e["type"] for e in entries] == ["run.started", "node.started"]
    assert entries[0]["v"] == "1"
    assert entries[1]["payload"] == {"node": "planner"}


def test_control_frames_are_refused() -> None:
    """§9.2: `hello` and `ping` describe a connection, so they never enter the log."""
    emitter = RunEmitter("r1", FakeRedis())
    with pytest.raises(ValueError, match="control frame"):
        run(emitter.emit(EventType.HELLO, {}))


def test_tokens_coalesce_at_the_character_threshold() -> None:
    client = FakeRedis()
    emitter = RunEmitter("r1", client, coalesce_chars=10, coalesce_ms=10_000)

    async def scenario() -> None:
        for _ in range(4):
            await emitter.emit_token("planner", "abc")  # 12 chars total
        await emitter.aclose()

    run(scenario())
    entries = stream_of(client)
    assert [e["type"] for e in entries] == ["token.delta"]
    assert entries[0]["payload"] == {"node": "planner", "text": "abcabcabcabc"}


def test_tokens_coalesce_at_the_time_threshold() -> None:
    """Below the character threshold, the 80 ms timer is what gets tokens out."""
    client = FakeRedis()
    emitter = RunEmitter("r1", client, coalesce_chars=1000, coalesce_ms=10)

    async def scenario() -> None:
        await emitter.emit_token("planner", "hi")
        assert stream_of(client) == []  # nothing published yet
        await asyncio.sleep(0.05)

    run(scenario())
    assert [e["payload"]["text"] for e in stream_of(client)] == ["hi"]


def test_a_non_token_event_flushes_buffered_tokens_first() -> None:
    """Ordering: a completion must never overtake the tokens that produced it."""
    client = FakeRedis()
    emitter = RunEmitter("r1", client, coalesce_chars=1000, coalesce_ms=10_000)

    async def scenario() -> None:
        await emitter.emit_token("planner", "partial output")
        await emitter.emit(EventType.NODE_COMPLETED, {"node": "planner"})

    run(scenario())
    assert [e["type"] for e in stream_of(client)] == ["token.delta", "node.completed"]
    assert [int(e["seq"]) for e in stream_of(client)] == [1, 2]


def test_close_flushes_a_partial_buffer() -> None:
    client = FakeRedis()
    emitter = RunEmitter("r1", client, coalesce_chars=1000, coalesce_ms=10_000)

    async def scenario() -> None:
        await emitter.emit_token("coder", "tail")
        await emitter.aclose()

    run(scenario())
    assert [e["payload"]["text"] for e in stream_of(client)] == ["tail"]


def test_an_unserialisable_payload_still_records_the_event() -> None:
    """Losing the detail beats losing the fact that the node ran."""
    client = FakeRedis()
    emitter = RunEmitter("r1", client)
    run(emitter.emit(EventType.NODE_COMPLETED, {"node": object()}))
    entries = stream_of(client)
    assert len(entries) == 1
    assert entries[0]["type"] == "node.completed"


def test_a_redis_failure_does_not_raise_into_the_graph() -> None:
    """Observability must never be able to fail the thing it observes."""

    class BrokenRedis(FakeRedis):
        async def incr(self, key: str) -> int:
            raise ConnectionError("redis is down")

    emitter = RunEmitter("r1", BrokenRedis())
    assert run(emitter.emit(EventType.NODE_STARTED, {"node": "planner"})) == 0


def test_budget_warning_computes_a_percentage_and_ignores_no_budget() -> None:
    client = FakeRedis()
    emitter = RunEmitter("r1", client)
    assert run(emitter.budget_warning("tokens", 200_000, 250_000)) == 1
    assert stream_of(client)[0]["payload"]["percent"] == 80.0
    assert run(emitter.budget_warning("tokens", 5, 0)) == 0


def test_sandbox_lines_are_truncated_to_the_frame_cap() -> None:
    client = FakeRedis()
    emitter = RunEmitter("r1", client)
    run(emitter.sandbox_line("stdout", "y" * 9000, execution_id="e1"))
    line = stream_of(client)[0]["payload"]["line"]
    assert line.endswith("…[truncated]")
    assert len(line.encode()) < 9000


def test_truncate_line_measures_bytes_not_characters() -> None:
    assert truncate_line("ok") == "ok"
    # Three bytes per character, so 10 characters exceed a 16-byte cap.
    assert truncate_line("界" * 10, limit=16).endswith("…[truncated]")


# ------------------------------------------------------------------------------------
#  SandboxLineStreamer
# ------------------------------------------------------------------------------------


def test_streamer_reassembles_chunks_into_lines() -> None:
    emitter = NullEmitter("r1")

    async def scenario() -> None:
        streamer = SandboxLineStreamer(
            emitter, asyncio.get_running_loop(), execution_id="e1", max_bytes=1_000_000
        )
        streamer("stdout", "Fitting 5 fol")
        streamer("stdout", "ds\n[CV] accuracy 0.94\npart")
        streamer.flush()
        await asyncio.sleep(0.02)

    run(scenario())
    lines = [p["line"] for _t, p in emitter.emitted]
    assert lines == ["Fitting 5 folds", "[CV] accuracy 0.94", "part"]
    assert all(p["execution_id"] == "e1" for _t, p in emitter.emitted)


def test_streamer_caps_a_stream_and_reports_what_it_dropped() -> None:
    emitter = NullEmitter("r1")

    async def scenario() -> None:
        streamer = SandboxLineStreamer(
            emitter, asyncio.get_running_loop(), execution_id="e1", max_bytes=20
        )
        for index in range(50):
            streamer("stdout", f"line {index}\n")
        streamer.flush()
        await asyncio.sleep(0.02)

    run(scenario())
    kinds = [t for t, _p in emitter.emitted]
    assert kinds.count("sandbox.truncated") == 1
    assert kinds.count("sandbox.stdout") < 50
    dropped = next(p for t, p in emitter.emitted if t == "sandbox.truncated")
    assert dropped["bytes_dropped"] > 0
    assert dropped["stream"] == "stdout"


def test_streamer_separates_stdout_from_stderr() -> None:
    emitter = NullEmitter("r1")

    async def scenario() -> None:
        streamer = SandboxLineStreamer(
            emitter, asyncio.get_running_loop(), execution_id="e1", max_bytes=10_000
        )
        streamer("stdout", "out\n")
        streamer("stderr", "err\n")
        await asyncio.sleep(0.02)

    run(scenario())
    assert [t for t, _p in emitter.emitted] == ["sandbox.stdout", "sandbox.stderr"]


# ------------------------------------------------------------------------------------
#  The ambient emitter
# ------------------------------------------------------------------------------------


def test_ambient_emit_is_a_no_op_without_an_emitter() -> None:
    assert current_emitter() is None
    assert run(emit(EventType.NODE_STARTED, {"node": "planner"})) == 0
    run(emit_token("orphaned"))  # must not raise


def test_ambient_emitter_reaches_a_nested_call_site() -> None:
    """The whole reason it is a contextvar: `structured.py` is four frames down."""
    emitter = NullEmitter("r1")

    async def deep_call_site() -> None:
        await emit_token("hello")

    async def scenario() -> None:
        token = set_emitter(emitter)
        set_current_node("coder")
        try:
            await deep_call_site()
        finally:
            reset_emitter(token)

    run(scenario())
    assert emitter.emitted == [("token.delta", {"node": "coder", "text": "hello"})]


def test_emitter_from_config_reads_the_configurable_override() -> None:
    emitter = NullEmitter("r1")
    assert emitter_from_config({"configurable": {"emitter": emitter}}) is emitter
    assert emitter_from_config({}) is None
    assert emitter_from_config(None) is None
