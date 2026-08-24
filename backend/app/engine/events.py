"""The typed event emitter: engine → Redis Streams (ARCHITECTURE.md §9, §7.2).

The worker writes only to Redis and never to a socket (§9.1). That is the single rule
this module exists to enforce: a browser on hotel wifi cannot slow a training run down,
because the two are separated by a durable log neither of them blocks on.

**Sequence allocation.** `INCR run:{id}:seq` happens *before* the `XADD`, so `seq` is
gapless and strictly increasing regardless of what Redis assigns as a stream id. Both
operations happen under `self._lock`, which makes the pair atomic against concurrent
emitters inside one process — nodes emit from several tasks (the log pump, the token
stream, the node envelope), and an interleaving that let `seq 42` land in the stream after
`seq 43` would put the replay path permanently out of order for that run.

**Token coalescing.** A model emitting 40 tok/s would otherwise produce 40 stream entries
and 40 frames per client per second, which is both a Redis write amplification problem and
a React reconciliation problem. `emit_token` buffers per node and flushes on whichever
comes first: `COALESCE_CHARS` (64) characters or `COALESCE_MS` (80) milliseconds. Every
non-token emit flushes the buffer first, because a `node.completed` that overtook the
tokens the node produced would render the console out of order.

**Emission is best-effort by design.** A Redis blip during a 15-minute training run must
cost a few events, not the run. Every publish is wrapped: failures are logged at WARNING
and swallowed. The inverse policy — letting an event failure raise into the graph — would
mean the observability layer could fail the thing it observes.

`NullEmitter` is the no-op used when no Redis is configured, which is what makes the whole
engine test suite run without a Redis server while the emission call sites stay in the
node bodies rather than behind `if emitter is not None` at every use.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any

from redis.asyncio import Redis

from app.core import redis as redis_layer
from app.schemas.events import CONTROL_EVENTS, EventType, now_rfc3339

logger = logging.getLogger(__name__)

# §9.1: "buffer 80 ms or 64 characters, whichever first".
COALESCE_MS = 80
COALESCE_CHARS = 64

# §9.1: "line-oriented, truncated at 4 KiB per frame". A single line longer than this is
# a program printing a serialised array, and no human reads past the first 4 KiB of it.
MAX_LINE_BYTES = 4096


class RunEmitter:
    """Publishes `pluton.v1` events for one run to `run:{run_id}:events`."""

    def __init__(
        self,
        run_id: str,
        client: Redis | None = None,
        *,
        coalesce_ms: int = COALESCE_MS,
        coalesce_chars: int = COALESCE_CHARS,
        maxlen: int = redis_layer.EVENT_STREAM_MAXLEN,
    ) -> None:
        self.run_id = str(run_id)
        self.client = client if client is not None else redis_layer.get_redis()
        self.coalesce_ms = coalesce_ms
        self.coalesce_chars = coalesce_chars
        self.maxlen = maxlen
        self._lock = asyncio.Lock()
        self._buffers: dict[str, list[str]] = {}
        self._flusher: asyncio.Task[None] | None = None

    # -- core ---------------------------------------------------------------------

    async def emit(
        self, event_type: EventType | str, payload: dict[str, Any] | None = None
    ) -> int:
        """Publish one event. Returns its `seq`, or 0 if the publish failed.

        Control frames are refused rather than silently dropped: `hello` and `ping`
        describe a connection, and a connection frame in the durable log would be
        replayed to a client that never opened that connection.
        """
        if event_type in CONTROL_EVENTS:
            raise ValueError(
                f"{event_type} is a control frame (§9.2) and is never written to the "
                "durable log — the WebSocket layer sends it directly."
            )

        await self.flush_tokens()
        return await self._publish(str(event_type), payload or {})

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> int:
        try:
            body = json.dumps(payload, default=str)
        except (TypeError, ValueError) as exc:
            # A payload that will not serialise is a call-site bug. Record that the event
            # happened rather than losing it entirely — the type is usually the part that
            # matters and the detail is recoverable from the logs.
            logger.warning(
                "Event %s for run %s is not JSON-serialisable: %s",
                event_type,
                self.run_id,
                exc,
            )
            body = json.dumps({"_unserialisable": True})

        try:
            async with self._lock:
                seq = await redis_layer.next_seq(self.client, self.run_id)
                await redis_layer.append_event(
                    self.client,
                    self.run_id,
                    seq=seq,
                    event_type=event_type,
                    ts=now_rfc3339(),
                    payload_json=body,
                    maxlen=self.maxlen,
                )
            return seq
        except Exception as exc:  # noqa: BLE001 - observability never fails the run
            logger.warning(
                "Publishing %s for run %s failed: %s", event_type, self.run_id, exc
            )
            return 0

    async def summary(self, **values: Any) -> None:
        """Merge fields into `run:{id}:summary`, the hot snapshot the `hello` frame reads.

        Kept separate from `emit` rather than derived from it: the summary is a projection
        the WebSocket layer needs *before* it has replayed anything, and reconstructing it
        by folding ten thousand stream entries on every connect is exactly the cost the
        hash exists to avoid.
        """
        try:
            await redis_layer.write_summary(
                self.client,
                self.run_id,
                {k: ("" if v is None else str(v)) for k, v in values.items()},
            )
        except Exception as exc:  # noqa: BLE001 - as above
            logger.warning(
                "Updating the summary for run %s failed: %s", self.run_id, exc
            )

    # -- token coalescing ---------------------------------------------------------

    async def emit_token(self, node: str, text: str) -> None:
        """Buffer an LLM token delta, flushing on the §9.1 thresholds."""
        if not text:
            return
        buffer = self._buffers.setdefault(node, [])
        buffer.append(text)
        if sum(len(chunk) for chunk in buffer) >= self.coalesce_chars:
            await self._flush_node(node)
        else:
            self._arm_flusher()

    def _arm_flusher(self) -> None:
        """Start the time-based half of the coalescing rule.

        One timer for all nodes, not one per node: only one node runs at a time in this
        graph, and a task per node would be a task per buffer to cancel on shutdown for
        no behavioural gain.
        """
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(
                self._flush_after_delay(), name=f"token-flush-{self.run_id}"
            )

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.coalesce_ms / 1000)
            await self.flush_tokens()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Flushing tokens for run %s failed: %s", self.run_id, exc)

    async def _flush_node(self, node: str) -> None:
        text = "".join(self._buffers.pop(node, []))
        if text:
            await self._publish(
                str(EventType.TOKEN_DELTA), {"node": node, "text": text}
            )

    async def flush_tokens(self) -> None:
        """Emit every buffered token delta. Idempotent, and cheap when nothing is buffered."""
        if not self._buffers:
            return
        for node in list(self._buffers):
            await self._flush_node(node)

    async def aclose(self) -> None:
        """Flush and stop the timer. Called once per run, from the worker's `finally`."""
        if self._flusher is not None:
            self._flusher.cancel()
            self._flusher = None
        await self.flush_tokens()

    # -- named helpers used by more than one call site ----------------------------

    async def node_started(self, node: str, **payload: Any) -> int:
        return await self.emit(EventType.NODE_STARTED, {"node": node, **payload})

    async def node_completed(self, node: str, **payload: Any) -> int:
        return await self.emit(EventType.NODE_COMPLETED, {"node": node, **payload})

    async def node_failed(self, node: str, **payload: Any) -> int:
        return await self.emit(EventType.NODE_FAILED, {"node": node, **payload})

    async def sandbox_line(self, stream: str, line: str, **payload: Any) -> int:
        """One stdout/stderr line, truncated to the §9.1 per-frame ceiling."""
        event = (
            EventType.SANDBOX_STDERR if stream == "stderr" else EventType.SANDBOX_STDOUT
        )
        return await self.emit(
            event, {"line": truncate_line(line), "ts": now_rfc3339(), **payload}
        )

    async def budget_warning(self, resource: str, used: float, limit: float) -> int:
        """§9.4's 80%-of-budget warning. `limit == 0` means unbounded, so nothing to warn."""
        if limit <= 0:
            return 0
        return await self.emit(
            EventType.BUDGET_WARNING,
            {
                "resource": resource,
                "used": used,
                "limit": limit,
                "percent": round(100 * used / limit, 1),
            },
        )


class NullEmitter(RunEmitter):
    """An emitter that publishes nothing.

    Subclasses `RunEmitter` rather than being a bare object so that a node holding one is
    holding the same type either way — a `Protocol` would work too, but a subclass keeps
    `isinstance` checks and type annotations in the nodes down to one name.
    """

    def __init__(self, run_id: str = "null") -> None:  # noqa: D107 - see class docstring
        self.run_id = str(run_id)
        self.client = None  # type: ignore[assignment]
        self.coalesce_ms = COALESCE_MS
        self.coalesce_chars = COALESCE_CHARS
        self.maxlen = 0
        self._lock = asyncio.Lock()
        self._buffers = {}
        self._flusher = None
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(
        self, event_type: EventType | str, payload: dict[str, Any] | None = None
    ) -> int:
        self.emitted.append((str(event_type), dict(payload or {})))
        return 0

    async def emit_token(self, node: str, text: str) -> None:
        if text:
            self.emitted.append(
                (str(EventType.TOKEN_DELTA), {"node": node, "text": text})
            )

    async def summary(self, **values: Any) -> None:
        return None

    async def flush_tokens(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class SandboxLineStreamer:
    """Turns the sandbox driver's byte chunks into `sandbox.stdout` / `sandbox.stderr`.

    Three translations happen here, and each one is a §9.1 constraint:

    * **Chunks become lines.** `docker logs` hands over whatever arrived on the socket,
      which splits mid-line as often as not. A console that renders half a line and then
      completes it two frames later is unreadable, so partial tails are held until the
      newline arrives.
    * **Threads become tasks.** The driver's log pump is a blocking generator running in
      `asyncio.to_thread`, so it cannot await. `run_coroutine_threadsafe` hands each line
      back to the loop that owns the emitter. Nothing waits on the result: blocking the
      pump on the event loop would let a slow Redis throttle a training run, which is
      precisely the coupling the durable log exists to prevent.
    * **Unbounded output becomes a cap plus a `sandbox.truncated`.** A program printing in
      a loop must not be able to fill the stream. Past `max_bytes` per stream, lines are
      dropped and one `sandbox.truncated` event records how much was lost.
    """

    def __init__(
        self,
        emitter: RunEmitter,
        loop: asyncio.AbstractEventLoop,
        *,
        execution_id: str,
        max_bytes: int,
    ) -> None:
        self.emitter = emitter
        self.loop = loop
        self.execution_id = str(execution_id)
        self.max_bytes = max_bytes
        self._partial: dict[str, str] = {"stdout": "", "stderr": ""}
        self._bytes: dict[str, int] = {"stdout": 0, "stderr": 0}
        self._dropped: dict[str, int] = {"stdout": 0, "stderr": 0}
        self._announced: set[str] = set()

    def __call__(self, stream: str, chunk: str) -> None:
        """The `OutputCallback` the driver invokes. Runs on the pump thread."""
        buffered = self._partial.get(stream, "") + chunk
        *lines, self._partial[stream] = buffered.split("\n")
        for line in lines:
            self._offer(stream, line)

    def flush(self) -> None:
        """Emit any line the container never terminated. Called once, after exit.

        A program killed by a timeout or an OOM frequently dies mid-`print`, and that last
        partial line is often the most informative thing it produced.
        """
        for stream, tail in list(self._partial.items()):
            if tail:
                self._partial[stream] = ""
                self._offer(stream, tail)
        for stream, dropped in self._dropped.items():
            if dropped and stream not in self._announced:
                self._announced.add(stream)
                self._schedule(
                    self.emitter.emit(
                        EventType.SANDBOX_TRUNCATED,
                        {
                            "execution_id": self.execution_id,
                            "stream": stream,
                            "bytes_dropped": dropped,
                        },
                    )
                )

    def _offer(self, stream: str, line: str) -> None:
        size = len(line.encode("utf-8", errors="replace")) + 1
        if self._bytes.get(stream, 0) + size > self.max_bytes:
            self._dropped[stream] = self._dropped.get(stream, 0) + size
            return
        self._bytes[stream] = self._bytes.get(stream, 0) + size
        self._schedule(
            self.emitter.sandbox_line(
                stream, line.rstrip("\r"), execution_id=self.execution_id
            )
        )

    def _schedule(self, coro: Any) -> None:
        """Hand a coroutine to the emitter's loop from whatever thread we are on."""
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:  # pragma: no cover - loop already closed
            coro.close()
            return
        # Consume the result so a failed publish does not surface as "exception was never
        # retrieved" noise on garbage collection.
        future.add_done_callback(lambda f: f.exception())


def truncate_line(line: str, limit: int = MAX_LINE_BYTES) -> str:
    """Cut a line to `limit` bytes, marking that it was cut.

    Measured in bytes rather than characters because the cap protects the frame size, and
    a line of CJK text is three times longer in bytes than in characters.
    """
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return line
    return encoded[:limit].decode("utf-8", errors="ignore") + "…[truncated]"


# ------------------------------------------------------------------------------------
#  Ambient emitter
# ------------------------------------------------------------------------------------

# The emitter is ambient rather than threaded through every node signature. Two reasons,
# and neither is convenience: LangGraph fixes the node signature at `(state, config)`, so
# an extra parameter would have to travel inside `config` and be unpacked by every node
# that needs it; and the deepest call sites that want it — `structured.call_text`
# streaming tokens, the sandbox driver's log pump emitting stdout lines — are three and
# four frames below the node body, so threading it would mean adding a parameter to every
# function in between whose own job has nothing to do with events.
#
# `contextvars` is the right mechanism because it follows `asyncio.create_task` and
# `asyncio.to_thread`, which is exactly where those two call sites live.
_current_emitter: contextvars.ContextVar[RunEmitter | None] = contextvars.ContextVar(
    "pluton_current_emitter", default=None
)
_current_node: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pluton_current_node", default=""
)


def set_emitter(emitter: RunEmitter | None) -> contextvars.Token[RunEmitter | None]:
    return _current_emitter.set(emitter)


def reset_emitter(token: contextvars.Token[RunEmitter | None]) -> None:
    _current_emitter.reset(token)


def current_emitter() -> RunEmitter | None:
    return _current_emitter.get()


def set_current_node(name: str) -> contextvars.Token[str]:
    return _current_node.set(name)


def reset_current_node(token: contextvars.Token[str]) -> None:
    _current_node.reset(token)


def current_node() -> str:
    return _current_node.get()


def emitter_from_config(config: Any) -> RunEmitter | None:
    """The emitter for this run, from the `RunnableConfig` the worker built.

    Mirrors `nodes.base.get_sandbox` and friends: an explicit `emitter` in `configurable`
    wins, so the worker injects a live one and a test injects a `NullEmitter` — or
    nothing, in which case events are simply not emitted.
    """
    configurable = (config or {}).get("configurable") or {}
    return configurable.get("emitter")


async def emit(
    event_type: EventType | str, payload: dict[str, Any] | None = None
) -> int:
    """Emit through the ambient emitter, or do nothing when there is none."""
    emitter = current_emitter()
    if emitter is None:
        return 0
    return await emitter.emit(event_type, payload)


async def emit_token(text: str, node: str | None = None) -> None:
    """Stream a token delta from wherever the LLM call happens to be."""
    emitter = current_emitter()
    if emitter is None:
        return
    await emitter.emit_token(node or current_node() or "unknown", text)


__all__ = [
    "COALESCE_CHARS",
    "COALESCE_MS",
    "MAX_LINE_BYTES",
    "NullEmitter",
    "RunEmitter",
    "SandboxLineStreamer",
    "current_emitter",
    "current_node",
    "emit",
    "emit_token",
    "emitter_from_config",
    "reset_current_node",
    "reset_emitter",
    "set_current_node",
    "set_emitter",
    "truncate_line",
]
