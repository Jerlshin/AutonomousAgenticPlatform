"""Redis: connection pools, the run keyspace, Stream primitives and the run lock.

Three responsibilities live here, and they are together because they share one thing —
the keyspace table in `ARCHITECTURE.md` §7.2. A key pattern written in two modules is a
key pattern that eventually disagrees with itself, so every `run:{id}:*` name in the
platform is built by a function in this file and nowhere else.

**Two logical databases, deliberately.** `REDIS_URL` (db 0) holds operational state — the
arq queue, the event streams, the locks — and `REDIS_CACHE_URL` (db 1) holds nothing that
cannot be recomputed. That split is what makes `FLUSHDB 1` a safe thing to do on a machine
mid-run, which is the only reason a cache is worth having on a box this size.

**Streams, not pub/sub.** §9.1's first constraint is that a browser reconnecting after a
laptop sleep must not miss events, and pub/sub has no memory of what it published. The
stream is the durable log; the WebSocket layer replays from it with `XRANGE` and then
tails it with `XREAD BLOCK`. The client's resume cursor is the `seq` field, never the
Redis stream id — stream ids are an implementation detail that changes under `XTRIM`.

**The lock is compare-and-delete on release.** A worker that stalls past `RUN_LOCK_TTL_S`
loses its lock, and the reaper may hand the run to someone else; if that original worker
then wakes up and issues a bare `DEL`, it deletes a lock it no longer owns and two workers
execute one run. `_RELEASE_SCRIPT` refuses to delete a value that is not this worker's id,
which turns that race into a no-op instead of a corruption.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, TypeVar, cast

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import settings

_T = TypeVar("_T")


def awaited(value: Any) -> Awaitable[Any]:
    """Narrow one redis-py return value to the awaitable it actually is.

    redis-py types every command as `Awaitable[T] | T` because one implementation backs
    both the sync and the async client, so `await client.get(...)` does not type-check
    even though it is correct for `redis.asyncio.Redis`. This is the cast, in one place
    and named, rather than a `# type: ignore` per call site.
    """
    return cast(Awaitable[Any], value)


logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------
#  Keyspace  (ARCHITECTURE.md §7.2)
# ------------------------------------------------------------------------------------

# 24 h on every per-run key. A run is at most 30 minutes of wall clock and the UI stops
# asking about it long before the next day, so retention past this only costs memory.
RUN_KEY_TTL_S = 24 * 60 * 60

# `XTRIM MAXLEN ~ 10000`: approximate trimming trims whole macro-nodes, which is an order
# of magnitude cheaper than exact trimming and is why the `~` is not an oversight. A run
# emitting more than 10 000 events has a token-coalescing bug, not a retention problem.
EVENT_STREAM_MAXLEN = 10_000

# WebSocket tickets (§9.3). Single-use and short-lived: the ticket travels in a query
# string, which lands in access logs and browser history, so it must be worthless by the
# time anyone reads it back.
TICKET_TTL_S = 60


def events_key(run_id: str) -> str:
    """The durable event log for a run. The sole source for WebSocket replay."""
    return f"run:{run_id}:events"


def seq_key(run_id: str) -> str:
    """The monotonic `seq` allocator. `INCR` happens *before* the `XADD`."""
    return f"run:{run_id}:seq"


def summary_key(run_id: str) -> str:
    """Hot snapshot (status, phase, node, percent) for a cheap WebSocket `hello`."""
    return f"run:{run_id}:summary"


def control_channel(run_id: str) -> str:
    """Pub/sub channel carrying out-of-band `cancel` / `approve` to the executing worker.

    Pub/sub is correct *here* and wrong for events: a control signal that nobody is
    listening for is a signal for a run that is not executing, which is exactly the
    signal that should be dropped.
    """
    return f"run:{run_id}:control"


def lock_key(run_id: str) -> str:
    """Single-owner execution lock. Value is the worker id."""
    return f"lock:run:{run_id}"


def idem_key(idempotency_key: str) -> str:
    return f"idem:{idempotency_key}"


def conns_key(run_id: str) -> str:
    """Live WebSocket connection ids for a run, for the §9.7 `4429` quota."""
    return f"ws:conns:{run_id}"


def ticket_key(ticket: str) -> str:
    return f"ws:ticket:{ticket}"


# ------------------------------------------------------------------------------------
#  Connection pools
# ------------------------------------------------------------------------------------

_clients: dict[str, Redis] = {}


def _client_for(url: str) -> Redis:
    """A pooled client for `url`, created once per process.

    `redis.asyncio.from_url` builds its own `ConnectionPool`, so caching the client is
    caching the pool. `decode_responses=True` throughout: every value this platform puts
    in Redis is UTF-8 JSON or an identifier, and decoding at the boundary keeps `bytes`
    out of the event path entirely.
    """
    client = _clients.get(url)
    if client is None:
        client = aioredis.from_url(
            url,
            decode_responses=True,
            health_check_interval=30,
            socket_keepalive=True,
        )
        _clients[url] = client
    return client


def get_redis() -> Redis:
    """The operational database (db 0): queue, streams, locks, tickets."""
    return _client_for(settings.REDIS_URL)


def get_cache_redis() -> Redis:
    """The cache database (db 1). Everything here is recomputable by construction."""
    return _client_for(settings.REDIS_CACHE_URL)


async def close_redis() -> None:
    """Close every pooled client. Called from the API and worker shutdown hooks."""
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Closing a Redis client failed: %s", exc)
    _clients.clear()


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001 - callers want a bool, not a traceback
        logger.warning("Redis ping failed: %s", exc)
        return False


# ------------------------------------------------------------------------------------
#  Stream primitives  (ARCHITECTURE.md §7.2, §9)
# ------------------------------------------------------------------------------------


async def next_seq(client: Redis, run_id: str) -> int:
    """Allocate the next `seq` for a run.

    `INCR` on a missing key starts at 1, which is exactly what §9.2 requires ("starting
    at 1"), so the allocator needs no initialisation step.
    """
    key = seq_key(run_id)
    value = int(await client.incr(key))
    if value == 1:
        await client.expire(key, RUN_KEY_TTL_S)
    return value


async def append_event(
    client: Redis,
    run_id: str,
    *,
    seq: int,
    event_type: str,
    ts: str,
    payload_json: str,
    maxlen: int = EVENT_STREAM_MAXLEN,
) -> str:
    """`XADD` one event. Returns the stream id, which callers may ignore.

    Streams do not nest, so the envelope is written as flat fields with `payload` as a
    JSON string — the exact entry format in §7.2.
    """
    return str(
        await client.xadd(
            events_key(run_id),
            {
                "v": "1",
                "seq": str(seq),
                "type": event_type,
                "ts": ts,
                "payload": payload_json,
            },
            maxlen=maxlen,
            approximate=True,
        )
    )


def decode_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """One stream entry as the §9.2 envelope, with `payload` decoded from its JSON.

    Tolerant on purpose: a malformed entry is a bug on the write side, and dropping the
    whole replay because one field failed to parse would turn a cosmetic defect into an
    unusable UI. The stream id is carried in `_id` for the tail cursor and stripped
    before the envelope goes out on the wire.
    """
    try:
        payload = json.loads(fields.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}

    try:
        seq = int(fields.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0

    return {
        "v": int(fields.get("v") or 1),
        "seq": seq,
        "type": fields.get("type") or "error",
        "ts": fields.get("ts") or "",
        "payload": payload,
        "_id": entry_id,
    }


async def read_backlog(
    client: Redis, run_id: str, *, after_seq: int = 0, count: int = 5000
) -> tuple[list[dict[str, Any]], str, int | None]:
    """Every retained event with `seq > after_seq`, in stream order.

    Returns `(events, last_stream_id, oldest_retained_seq)`. `oldest_retained_seq` is what
    §9.3's gap detection compares against: if the client's cursor predates it, history it
    needs has already been trimmed and it must resynchronise from a snapshot rather than
    silently miss events.

    The filter is applied here rather than by asking Redis for a range, because the
    resume cursor is `seq` and the range index is the stream id — the two are only
    correlated, never equal, and `XTRIM` breaks even the correlation.
    """
    raw = await client.xrange(events_key(run_id), min="-", max="+", count=count)
    if not raw:
        return [], "0-0", None

    decoded = [decode_entry(entry_id, fields) for entry_id, fields in raw]
    oldest = next((e["seq"] for e in decoded if e["seq"] > 0), None)
    last_id = decoded[-1]["_id"]
    return [e for e in decoded if e["seq"] > after_seq], last_id, oldest


async def read_live(
    client: Redis, run_id: str, *, last_id: str, block_ms: int = 5000, count: int = 200
) -> tuple[list[dict[str, Any]], str]:
    """Block for up to `block_ms` on new entries after `last_id`.

    Returns `(events, new_last_id)`; an empty list means the block expired with nothing
    new, which is the heartbeat's cue and not an error.
    """
    response = await client.xread(
        {events_key(run_id): last_id}, count=count, block=block_ms
    )
    if not response:
        return [], last_id

    events = [
        decode_entry(entry_id, fields)
        for _stream, entries in response
        for entry_id, fields in entries
    ]
    return events, events[-1]["_id"] if events else last_id


async def last_seq(client: Redis, run_id: str) -> int:
    """The highest `seq` allocated for a run, or 0. Used by the `hello` frame."""
    value = await client.get(seq_key(run_id))
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


async def read_summary(client: Redis, run_id: str) -> dict[str, str]:
    """The run summary hash, or `{}` when the run has emitted nothing yet."""
    return dict(await awaited(client.hgetall(summary_key(run_id))) or {})


async def write_summary(client: Redis, run_id: str, values: dict[str, str]) -> None:
    """Merge `values` into the summary hash and refresh its TTL."""
    if not values:
        return
    key = summary_key(run_id)
    await awaited(client.hset(key, mapping=values))
    await awaited(client.expire(key, RUN_KEY_TTL_S))


async def trim_stream(
    client: Redis, run_id: str, *, maxlen: int = EVENT_STREAM_MAXLEN
) -> int:
    """`XTRIM MAXLEN ~`. Returns entries removed. The stream-trim cron calls this."""
    return int(await client.xtrim(events_key(run_id), maxlen=maxlen, approximate=True))


# ------------------------------------------------------------------------------------
#  Control channel
# ------------------------------------------------------------------------------------


async def publish_control(client: Redis, run_id: str, message: dict[str, Any]) -> int:
    """Send an out-of-band control signal. Returns the number of subscribers reached.

    Zero subscribers is meaningful to the caller: it means no worker currently holds this
    run, so a `cancel` has to be applied to the database row instead of delivered.
    """
    return int(await client.publish(control_channel(run_id), json.dumps(message)))


# ------------------------------------------------------------------------------------
#  Run lock  (ARCHITECTURE.md §5.4)
# ------------------------------------------------------------------------------------

# Compare-and-delete. Deleting unconditionally would let a worker whose lock had already
# expired remove the lock of the worker that legitimately took the run over.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Compare-and-extend, for the same reason: renewing a lock somebody else now owns would
# hand this worker a lease it never had.
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class LockNotAcquired(RuntimeError):
    """Another worker holds this run's lock."""

    def __init__(self, run_id: str, owner: str | None) -> None:
        super().__init__(
            f"run {run_id} is already locked by {owner or 'another worker'}"
        )
        self.run_id = run_id
        self.owner = owner


class RunLock:
    """Single-owner execution lock for a run, renewed while the graph runs.

    The TTL is the crash-recovery mechanism, not a timeout: a worker that dies takes its
    renewal task with it, the key expires, and the reaper sees `status = RUNNING` with no
    lock and marks the run `INTERRUPTED`. That is why the renewal interval must stay well
    under the TTL — `renew_every_s` defaults to 60 s against a 1800 s lease, so roughly
    thirty renewals may fail in a row before ownership is actually lost.
    """

    def __init__(
        self,
        client: Redis,
        run_id: str,
        worker_id: str,
        *,
        ttl_s: int | None = None,
        renew_every_s: int = 60,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.worker_id = worker_id
        self.ttl_s = ttl_s if ttl_s is not None else settings.RUN_LOCK_TTL_S
        self.renew_every_s = renew_every_s
        self.acquired = False
        self._renewer: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        """`SET lock:run:{id} {worker_id} NX EX {ttl}`. False means someone else won."""
        self.acquired = bool(
            await self.client.set(
                lock_key(self.run_id), self.worker_id, nx=True, ex=self.ttl_s
            )
        )
        return self.acquired

    async def owner(self) -> str | None:
        return cast("str | None", await awaited(self.client.get(lock_key(self.run_id))))

    async def renew(self) -> bool:
        """Extend the lease, but only while this worker still owns it."""
        result = await awaited(
            self.client.eval(
                _RENEW_SCRIPT, 1, lock_key(self.run_id), self.worker_id, str(self.ttl_s)
            )
        )
        return bool(result)

    async def release(self) -> bool:
        """Drop the lock if — and only if — this worker still owns it."""
        self._stop_renewer()
        if not self.acquired:
            return False
        self.acquired = False
        result = await awaited(
            self.client.eval(_RELEASE_SCRIPT, 1, lock_key(self.run_id), self.worker_id)
        )
        return bool(result)

    async def _renew_forever(self) -> None:
        while True:
            await asyncio.sleep(self.renew_every_s)
            try:
                if not await self.renew():
                    # Lost ownership. Say so loudly: from here the run is executing
                    # without a lock and the reaper is entitled to declare it interrupted.
                    logger.error(
                        "Lost the execution lock for run %s (worker %s)",
                        self.run_id,
                        self.worker_id,
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a blip must not end the run
                logger.warning(
                    "Renewing the lock for run %s failed: %s", self.run_id, exc
                )

    def start_renewer(self) -> None:
        if self._renewer is None and self.acquired:
            self._renewer = asyncio.create_task(
                self._renew_forever(), name=f"run-lock-renew-{self.run_id}"
            )

    def _stop_renewer(self) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
            self._renewer = None

    async def __aenter__(self) -> RunLock:
        if not await self.acquire():
            raise LockNotAcquired(self.run_id, await self.owner())
        self.start_renewer()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.release()


@asynccontextmanager
async def run_lock(
    run_id: str, worker_id: str | None = None, *, client: Redis | None = None
) -> AsyncIterator[RunLock]:
    """Hold the execution lock for `run_id`, renewing it for the duration."""
    lock = RunLock(
        client or get_redis(), run_id, worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with lock:
        yield lock


__all__ = [
    "EVENT_STREAM_MAXLEN",
    "LockNotAcquired",
    "RUN_KEY_TTL_S",
    "RunLock",
    "TICKET_TTL_S",
    "append_event",
    "close_redis",
    "conns_key",
    "control_channel",
    "decode_entry",
    "events_key",
    "get_cache_redis",
    "get_redis",
    "idem_key",
    "last_seq",
    "lock_key",
    "next_seq",
    "ping",
    "publish_control",
    "read_backlog",
    "read_live",
    "read_summary",
    "run_lock",
    "seq_key",
    "summary_key",
    "ticket_key",
    "trim_stream",
    "write_summary",
]
