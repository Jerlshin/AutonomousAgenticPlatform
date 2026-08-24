"""The `pluton.v1` WebSocket endpoint (ARCHITECTURE.md §9).

`GET /api/v1/ws/runs/{run_id}` upgrades to a socket that replays a run's history and then
tails it live. `POST /api/v1/ws/tickets` mints the credential a browser connects with.

The whole design follows from four constraints in §9.1, and each one shows up as a
specific mechanism here:

* *A browser reconnecting after a laptop sleep must not miss events.* Replay comes from a
  Redis **Stream**, not a pub/sub subscription, and the cursor is the gapless `seq` field
  rather than the Redis stream id — stream ids move under `XTRIM`, `seq` does not.
* *An LLM emitting 40 tok/s must not produce 40 frames/s.* Coalescing happens upstream in
  `engine/events.py`, before the durable write, so it costs one Redis entry per coalesced
  frame rather than one per token *and* one per client.
* *A slow client must not stall the worker.* This module is the only thing that ever
  writes to a socket. The worker writes to Redis and stops caring; a client that cannot
  keep up is disconnected here, at `4429` or on a send failure, and the run does not
  notice.
* *Sandbox stdout can be megabytes.* Lines arrive already truncated and capped by
  `SandboxLineStreamer`; nothing unbounded reaches this layer.

**On the `subscribe` filter and the resume cursor.** A client that filters out
`token.delta` never sees those seqs, so its `last_seq` trails the stream and a reconnect
replays events it deliberately declined. That is the honest tradeoff of a server-side
filter and it is cheap — the replay is bounded by retention and the client discards them
again. The alternative, tracking a separate "highest seq withheld" cursor per connection,
adds protocol surface to save a few frames on reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.runs import get_run_redis, run_snapshot, ws_url
from app.core import metrics, redis as redis_layer
from app.core.db import AsyncSessionLocal, get_db
from app.core.security import (
    auth_required,
    authenticate_websocket,
    issue_ticket,
    require_token,
)
from app.db.models.task import Task
from app.schemas.events import (
    PROTOCOL,
    TERMINAL_EVENTS,
    ClientMessageType,
    CloseCode,
    ErrorPayload,
    EventType,
    HelloPayload,
    ReplayCompletePayload,
    ReplayGapPayload,
    WsTicketRequest,
    WsTicketResponse,
    envelope,
    matches_filter,
)

logger = logging.getLogger(__name__)

# Not `dependencies=[Depends(require_token)]` at the router level, unlike every other v1
# router: `HTTPBearer` reads an HTTP request and a WebSocket scope is not one, so a
# router-level dependency would fail the upgrade rather than authenticate it. `/tickets`
# carries the dependency on the route; the socket authenticates by ticket or first frame
# (§9.3), which is what `_authenticate` implements.
router = APIRouter()

# §9.7's `4429`: eight sockets per run, sixty-four per server. Per-run is the one that
# matters in practice — a page with four panes opens one socket, so eight is four tabs —
# and the server bound is the backstop against a client in a reconnect loop.
MAX_CONNECTIONS_PER_RUN = 8
MAX_CONNECTIONS_PER_SERVER = 64

# §9.3's `hello`, and the interval the client is told to expect pings at.
HEARTBEAT_S = 20

# Two consecutive missed pongs close the socket (§9.5). The peer has had 40 s to answer a
# frame that costs it nothing; at that point the connection is dead and holding it open
# only keeps a `ws:conns` slot occupied against the quota.
MAX_MISSED_PONGS = 2

# How long `XREAD` blocks. Short enough that the heartbeat and terminal checks stay
# responsive, long enough that an idle run is not a polling loop.
READ_BLOCK_MS = 5000

# §9.3's first-frame auth window.
AUTH_FRAME_TIMEOUT_S = 5.0

# Live connections across this process, for the server-wide half of the quota.
_open_connections = 0


# ------------------------------------------------------------------------------------
#  Tickets
# ------------------------------------------------------------------------------------


@router.post(
    "/tickets",
    response_model=WsTicketResponse,
    dependencies=[Depends(require_token)],
    summary="Mint a WebSocket ticket",
)
async def create_ticket(
    payload: WsTicketRequest, db: AsyncSession = Depends(get_db)
) -> WsTicketResponse:
    """Exchange a bearer token for a single-use, run-scoped, 60-second ticket (§9.3)."""
    try:
        run_id = uuid.UUID(payload.run_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="run_id must be a UUID.",
        ) from exc

    if await db.get(Task, run_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found."
        )

    ticket, ttl = await issue_ticket(str(run_id))
    return WsTicketResponse(
        ticket=ticket,
        run_id=str(run_id),
        expires_in=ttl,
        ws_url=f"{ws_url(run_id)}?ticket={ticket}",
    )


# ------------------------------------------------------------------------------------
#  The connection
# ------------------------------------------------------------------------------------


class RunConnection:
    """One client socket, and everything that is per-connection rather than per-run.

    The filter, the heartbeat bookkeeping and the closed flag live here because a run has
    many connections and each of them may have made different choices — one dashboard
    subscribing to `node.` while a console tab takes everything.
    """

    def __init__(self, websocket: WebSocket, run_id: str, client: Redis) -> None:
        self.ws = websocket
        self.run_id = run_id
        self.client = client
        self.conn_id = uuid.uuid4().hex
        self.filter: set[str] | None = None
        self.missed_pongs = 0
        self.last_ping = 0.0
        self.closed = False
        # `closed` means the socket is gone; `finished` means we are done with it. The
        # distinction decides whether a close frame is still worth sending.
        self.finished = False
        self.resync = asyncio.Event()
        self.close_code = CloseCode.NORMAL

    async def send(self, message: dict[str, Any]) -> bool:
        """Write one frame. Returns False once the socket is gone, and never raises.

        Callers treat the return value as "keep going": a disconnect during a replay is a
        completely ordinary event — the user closed the tab — and unwinding it as an
        exception through every loop in this module would be noise, not information.
        """
        if self.closed:
            return False
        try:
            await self.ws.send_text(json.dumps(message))
            return True
        except (WebSocketDisconnect, RuntimeError) as exc:
            logger.debug(
                "Send on run %s failed; connection is gone: %s", self.run_id, exc
            )
            self.closed = True
            return False
        except Exception as exc:  # noqa: BLE001 - one bad client is not a server error
            logger.warning("Unexpected send failure on run %s: %s", self.run_id, exc)
            self.closed = True
            return False

    async def send_event(
        self, event_type: EventType | str, payload: Any = None, *, seq: int = 0
    ) -> bool:
        if not matches_filter(str(event_type), self.filter):
            return True
        sent = await self.send(envelope(self.run_id, event_type, payload, seq=seq))
        if sent:
            # Counted after the write, not before: a frame the socket refused was never
            # sent, and counting attempts would make the events-sent panel disagree with
            # what a client actually received during a disconnect.
            metrics.record_ws_event(str(event_type))
        return sent

    async def send_error(
        self, code: str, message: str, *, recoverable: bool = True
    ) -> bool:
        return await self.send_event(
            EventType.ERROR,
            ErrorPayload(code=code, message=message, recoverable=recoverable),
        )

    async def close(self, code: int = CloseCode.NORMAL, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.ws.close(code=code, reason=reason)


@router.websocket("/runs/{run_id}")
async def run_stream(
    websocket: WebSocket,
    run_id: str,
    after_seq: int = Query(0, ge=0),
    ticket: str | None = Query(default=None),
    client: Redis = Depends(get_run_redis),
) -> None:
    """Replay a run's history, then tail it live (§9.3)."""
    global _open_connections

    await _accept(websocket)
    conn = RunConnection(websocket, str(run_id), client)

    if not await _authenticate(conn, ticket):
        await conn.close(CloseCode.UNAUTHENTICATED, "authentication failed")
        return
    if not await _run_exists(run_id):
        await conn.close(CloseCode.NOT_FOUND, "run not found")
        return
    if not await _claim_slot(conn):
        await conn.close(CloseCode.QUOTA_EXCEEDED, "connection quota exceeded")
        return

    _open_connections += 1
    connection_gauge = contextlib.ExitStack()
    connection_gauge.enter_context(metrics.track_ws_connection())
    reader: asyncio.Task[None] | None = None
    try:
        await _send_hello(conn)
        reader = asyncio.create_task(
            _read_client(conn), name=f"ws-reader-{conn.conn_id}"
        )
        await _pump(conn, after_seq)
    except WebSocketDisconnect:
        logger.debug("Client disconnected from run %s", run_id)
    except Exception as exc:  # noqa: BLE001 - a socket failure is not a server failure
        logger.exception("WebSocket for run %s failed: %s", run_id, exc)
        conn.close_code = CloseCode.INTERNAL_ERROR
    finally:
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        _open_connections -= 1
        connection_gauge.close()
        await _release_slot(conn)
        await conn.close(conn.close_code)


async def _accept(websocket: WebSocket) -> None:
    """Accept, echoing `pluton.v1` back when the client offered it.

    A client that offers a subprotocol and is accepted without one is entitled to treat
    that as a rejection, so the echo is not decorative. A client that offers nothing is
    accepted anyway — `curl` and `websocat` do not send the header and are useful for
    debugging this endpoint.
    """
    offered = websocket.headers.get("sec-websocket-protocol", "")
    wanted = [p.strip() for p in offered.split(",") if p.strip()]
    if PROTOCOL in wanted:
        await websocket.accept(subprotocol=PROTOCOL)
    else:
        await websocket.accept()


async def _authenticate(conn: RunConnection, ticket: str | None) -> bool:
    """Ticket, or first-frame auth within `AUTH_FRAME_TIMEOUT_S` (§9.3)."""
    if ticket:
        return await authenticate_websocket(conn.run_id, ticket=ticket)
    if not auth_required():
        # A token-less development box (see `core/security.py`). There is nothing for the
        # client to prove, and waiting five seconds for it to prove nothing would delay
        # the first frame of every stream on the machine this platform is built on.
        return True

    # No ticket: the socket is open but the server says nothing until the client proves
    # itself. The timeout is what stops an unauthenticated socket from being a free way to
    # hold a connection slot.
    try:
        raw = await asyncio.wait_for(
            conn.ws.receive_text(), timeout=AUTH_FRAME_TIMEOUT_S
        )
    except (TimeoutError, WebSocketDisconnect, RuntimeError):
        return False
    except Exception:  # noqa: BLE001
        return False

    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if message.get("type") != ClientMessageType.AUTH:
        return False
    token = (message.get("payload") or {}).get("token")
    return await authenticate_websocket(conn.run_id, token=token)


def get_session_factory() -> Any:
    """The factory the two DB reads in this module open short-lived sessions from.

    Not a FastAPI dependency: a `Depends(get_db)` on a WebSocket route holds one session
    open for the life of the socket, which for this endpoint is the life of the run. Both
    reads here are momentary — does the run exist, what does it look like now — so they
    open and close their own. A module-level indirection is what keeps them overridable
    from a test.
    """
    return AsyncSessionLocal


async def _run_exists(run_id: str) -> bool:
    """Whether the run is real. A socket for a typo'd id closes `4404` rather than hanging."""
    try:
        key = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        return False
    try:
        async with get_session_factory()() as session:
            return await session.get(Task, key) is not None
    except Exception as exc:  # noqa: BLE001 - an unreachable database is not a 404
        logger.warning("Could not verify run %s exists: %s", run_id, exc)
        return True


async def _claim_slot(conn: RunConnection) -> bool:
    """Take a connection slot against the per-run and per-server quotas (§9.7)."""
    if _open_connections >= MAX_CONNECTIONS_PER_SERVER:
        return False
    try:
        key = redis_layer.conns_key(conn.run_id)
        await redis_layer.awaited(conn.client.sadd(key, conn.conn_id))
        await redis_layer.awaited(conn.client.expire(key, 3600))
        if (
            int(await redis_layer.awaited(conn.client.scard(key)))
            > MAX_CONNECTIONS_PER_RUN
        ):
            await redis_layer.awaited(conn.client.srem(key, conn.conn_id))
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - accounting failure must not deny service
        logger.warning("Connection accounting failed for run %s: %s", conn.run_id, exc)
        return True


async def _release_slot(conn: RunConnection) -> None:
    with contextlib.suppress(Exception):
        await redis_layer.awaited(
            conn.client.srem(redis_layer.conns_key(conn.run_id), conn.conn_id)
        )


async def _send_hello(conn: RunConnection) -> None:
    """The first frame: protocol, a snapshot of the run, and where the stream is."""
    summary = await redis_layer.read_summary(conn.client, conn.run_id)
    last = await redis_layer.last_seq(conn.client, conn.run_id)
    await conn.send(
        envelope(
            conn.run_id,
            EventType.HELLO,
            HelloPayload(run=summary, last_seq=last, heartbeat_s=HEARTBEAT_S),
        )
    )


async def _pump(conn: RunConnection, after_seq: int) -> None:
    """Replay the backlog, then tail live until the run ends or the client goes away."""
    cursor = await _replay(conn, after_seq)
    if conn.closed or conn.finished:
        return

    conn.last_ping = time.monotonic()
    while not conn.closed and not conn.finished:
        if conn.resync.is_set():
            conn.resync.clear()
            await _send_snapshot(conn)

        try:
            events, cursor = await redis_layer.read_live(
                conn.client, conn.run_id, last_id=cursor, block_ms=READ_BLOCK_MS
            )
        except Exception as exc:  # noqa: BLE001 - a Redis blip is not a protocol error
            logger.warning("Tailing run %s failed: %s", conn.run_id, exc)
            await asyncio.sleep(1.0)
            continue

        for event in events:
            if not await _forward(conn, event):
                return
            if event["type"] in TERMINAL_EVENTS:
                # §9.7's `1000`: the run reached a terminal state, so there is nothing
                # further to stream and the client's reconnect loop should stop.
                await conn.send_event(
                    EventType.REPLAY_COMPLETE,
                    ReplayCompletePayload(through_seq=event["seq"]),
                )
                return

        # Only an idle tail is evidence of anything: a stream still producing events is a
        # live connection, so the heartbeat is reserved for silence.
        if not events and not await _heartbeat(conn):
            return


async def _replay(conn: RunConnection, after_seq: int) -> str:
    """Send everything with `seq > after_seq`. Returns the live-tail cursor.

    A cursor older than retention gets `replay.gap` plus a full snapshot rather than a
    silently short replay: the client's state is then rebuilt from authoritative data,
    which is recoverable, instead of being missing history it does not know it is missing.
    """
    try:
        events, cursor, oldest = await redis_layer.read_backlog(
            conn.client, conn.run_id, after_seq=after_seq
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Replaying run %s failed: %s", conn.run_id, exc)
        await conn.send_error("replay_failed", str(exc))
        return "$"

    if oldest is not None and after_seq and oldest > after_seq + 1:
        # A gap means retention lost history a live client was still asking for, which is
        # the signal that `trim_event_streams` is trimming faster than clients reconnect.
        metrics.record_ws_replay_gap()
        await conn.send_event(
            EventType.REPLAY_GAP,
            ReplayGapPayload(requested_after=after_seq, oldest_available=oldest),
        )
        await _send_snapshot(conn)

    through = after_seq
    terminal_seen = False
    for event in events:
        if not await _forward(conn, event):
            return cursor
        through = max(through, event["seq"])
        terminal_seen = terminal_seen or event["type"] in TERMINAL_EVENTS

    await conn.send_event(
        EventType.REPLAY_COMPLETE, ReplayCompletePayload(through_seq=through)
    )
    if terminal_seen:
        # Connecting to a finished run is a legitimate thing to do — the report view does
        # it — and it should end in a clean close rather than an idle tail on a stream
        # nothing will ever write to again.
        conn.finished = True
    return cursor


async def _forward(conn: RunConnection, event: dict[str, Any]) -> bool:
    """Send one stored event in the §9.2 envelope, dropping the stream id."""
    return await conn.send_event(event["type"], event["payload"], seq=event["seq"])


async def _send_snapshot(conn: RunConnection) -> None:
    """`run.snapshot` — the full run body, for gap recovery and client `resync`."""
    try:
        async with get_session_factory()() as session:
            payload = await run_snapshot(uuid.UUID(conn.run_id), session, conn.client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build a snapshot for run %s: %s", conn.run_id, exc)
        await conn.send_error("snapshot_failed", str(exc))
        return
    await conn.send_event(EventType.RUN_SNAPSHOT, payload)


async def _heartbeat(conn: RunConnection) -> bool:
    """Send a `ping` when one is due. False means the peer has stopped answering.

    The clock lives on the connection rather than in the caller's loop: a `pong` handled
    by the reader task resets `missed_pongs`, and both halves have to agree on when the
    last ping went out.
    """
    now = time.monotonic()
    if now - conn.last_ping < HEARTBEAT_S:
        return True
    if conn.missed_pongs >= MAX_MISSED_PONGS:
        logger.info(
            "Run %s: client missed %d pongs; closing", conn.run_id, conn.missed_pongs
        )
        conn.close_code = CloseCode.GOING_AWAY
        return False
    conn.missed_pongs += 1
    conn.last_ping = now
    return await conn.send_event(EventType.PING, {})


# ------------------------------------------------------------------------------------
#  Client → server
# ------------------------------------------------------------------------------------


async def _read_client(conn: RunConnection) -> None:
    """Handle inbound frames for the life of the connection (§9.5).

    A separate task from the pump because both sides are blocking waits — `XREAD BLOCK`
    on one, `receive` on the other — and a single loop would have to poll one of them.
    """
    while not conn.closed:
        try:
            message = await conn.ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            conn.closed = True
            return

        if message.get("type") == "websocket.disconnect":
            conn.closed = True
            return
        if message.get("bytes") is not None:
            # §9: binary frames are rejected outright. This protocol is UTF-8 JSON, and a
            # binary frame means the peer is speaking something else.
            conn.close_code = CloseCode.PROTOCOL_ERROR
            await conn.close(CloseCode.PROTOCOL_ERROR, "binary frames are not accepted")
            return

        raw = message.get("text")
        if raw is None:
            continue
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            conn.close_code = CloseCode.PROTOCOL_ERROR
            await conn.close(CloseCode.PROTOCOL_ERROR, "malformed JSON")
            return
        if not isinstance(body, dict):
            await conn.send_error(
                "malformed_message", "a message must be a JSON object"
            )
            continue

        await _handle_client_message(conn, body)


async def _handle_client_message(conn: RunConnection, body: dict[str, Any]) -> None:
    """Dispatch one client message. Unknown types are answered, not fatal (§9.5)."""
    kind = body.get("type")
    payload = body.get("payload") or {}

    if kind == ClientMessageType.PONG:
        conn.missed_pongs = 0
    elif kind == ClientMessageType.AUTH:
        # A late or duplicate auth frame. The socket is already authenticated by the time
        # this task exists, so acknowledging and ignoring it is friendlier than closing on
        # a client that simply re-sent its credential.
        pass
    elif kind == ClientMessageType.RESYNC:
        conn.resync.set()
    elif kind == ClientMessageType.SUBSCRIBE:
        types = payload.get("types")
        conn.filter = {str(t) for t in types} if isinstance(types, list) else None
    elif kind == ClientMessageType.CANCEL:
        await redis_layer.publish_control(
            conn.client,
            conn.run_id,
            {
                "op": "cancel",
                "reason": payload.get("reason") or "cancelled from the UI",
            },
        )
    elif kind == ClientMessageType.APPROVE:
        await redis_layer.publish_control(
            conn.client,
            conn.run_id,
            {
                "op": "approve",
                "gate": payload.get("gate"),
                "decision": payload.get("decision"),
                "notes": payload.get("notes"),
            },
        )
    else:
        await conn.send_error(
            "unknown_message_type", f"unsupported message type: {kind!r}"
        )


__all__ = [
    "HEARTBEAT_S",
    "get_session_factory",
    "MAX_CONNECTIONS_PER_RUN",
    "MAX_CONNECTIONS_PER_SERVER",
    "RunConnection",
    "router",
]
