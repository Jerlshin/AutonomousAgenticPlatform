"""The `pluton.v1` wire contract (ARCHITECTURE.md §9.2, §9.4, §9.5).

One module holds every event name, the envelope they travel in, and the client messages
that come back, because all three are the same contract seen from different sides. The
frontend's TypeScript union is *generated* from this file by `scripts/gen_event_types.py`
(`make fe-types`), so renaming an event here is a frontend compile error rather than a
runtime `undefined` — which is the whole point of §18.5.

Payloads are `dict[str, Any]` on the envelope rather than a discriminated union of thirty
models. The envelope is written by the emitter and read by a browser; a union would add a
validation step on the hot path — `token.delta` fires several times a second per run — to
catch mistakes the emitter call sites cannot make, since each one constructs its payload
literally next to the `EventType` it names. What *is* worth typing is the handful of
frames the protocol itself defines rather than the engine: `hello`, `replay.complete`,
`replay.gap` and `error` have models here because the WebSocket layer constructs them in
more than one place and they must agree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL = "pluton.v1"
PROTOCOL_VERSION = 1


class EventType(StrEnum):
    """Every server→client event type in §9.4.

    Dotted, and the prefix is load-bearing: §18.3 gives each pane of the live run view a
    slice of the stream, and every one of those slices is a prefix match. `subscribe`
    filters accept both an exact type and a `node.*`-style prefix for that reason.
    """

    # Protocol / control. These carry `seq: 0` and are never written to the durable log.
    HELLO = "hello"
    PING = "ping"
    REPLAY_COMPLETE = "replay.complete"
    REPLAY_GAP = "replay.gap"
    ERROR = "error"

    # Run lifecycle.
    RUN_SNAPSHOT = "run.snapshot"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_PHASE = "run.phase"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"

    # Node lifecycle.
    NODE_STARTED = "node.started"
    NODE_PROGRESS = "node.progress"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    NODE_RETRYING = "node.retrying"

    # Agent work.
    TOKEN_DELTA = "token.delta"  # noqa: S105 - an LLM token, not a credential
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    RETRIEVAL_RESULTS = "retrieval.results"
    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"
    CODE_REVISION = "code.revision"

    # Sandbox.
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_STDOUT = "sandbox.stdout"
    SANDBOX_STDERR = "sandbox.stderr"
    SANDBOX_TRUNCATED = "sandbox.truncated"
    SANDBOX_EXIT = "sandbox.exit"

    # Results.
    ARTIFACT_CREATED = "artifact.created"
    METRIC_LOGGED = "metric.logged"
    EVALUATION_COMPLETED = "evaluation.completed"

    # Operator attention.
    INTERRUPT_REQUESTED = "interrupt.requested"
    BUDGET_WARNING = "budget.warning"


# Frames that describe the connection rather than the run. §9.2: they carry `seq: 0` and
# are excluded from the durable log, so a client resuming with `after_seq` never replays
# a `hello` from a previous session.
CONTROL_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.HELLO,
        EventType.PING,
        EventType.REPLAY_COMPLETE,
        EventType.REPLAY_GAP,
        EventType.RUN_SNAPSHOT,
        EventType.ERROR,
    }
)

# Terminal events. The WebSocket layer closes with `1000` after forwarding one of these,
# and the client's reconnect loop (§9.8) treats that close as "stop", not "retry".
TERMINAL_EVENTS: frozenset[EventType] = frozenset(
    {EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED}
)


class ClientMessageType(StrEnum):
    """Client→server messages (§9.5)."""

    AUTH = "auth"
    PONG = "pong"
    RESYNC = "resync"
    CANCEL = "cancel"
    APPROVE = "approve"
    SUBSCRIBE = "subscribe"


class CloseCode:
    """§9.7. Named because `4429` in a `close()` call is unreadable six months later."""

    NORMAL = 1000
    GOING_AWAY = 1001
    INTERNAL_ERROR = 1011
    PROTOCOL_ERROR = 4400
    UNAUTHENTICATED = 4401
    FORBIDDEN = 4403
    NOT_FOUND = 4404
    QUOTA_EXCEEDED = 4429


def now_rfc3339() -> str:
    """RFC 3339 UTC with millisecond precision and a `Z` suffix (§8.1, §9.2).

    Millisecond precision rather than microsecond because `ts` is a display and ordering
    aid, not the ordering mechanism — `seq` is — and `Date.parse` in a browser truncates
    past milliseconds anyway.
    """
    moment = datetime.now(UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


class EventEnvelope(BaseModel):
    """Every server→client message (§9.2)."""

    v: Literal[1] = PROTOCOL_VERSION  # type: ignore[assignment]  # int constant
    seq: int = Field(default=0, ge=0)
    run_id: str
    ts: str = Field(default_factory=now_rfc3339)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HelloPayload(BaseModel):
    """The first frame after accept. `last_seq` tells a fresh client where the run is."""

    protocol: Literal["pluton.v1"] = PROTOCOL  # type: ignore[assignment]  # str constant
    run: dict[str, Any] = Field(default_factory=dict)
    last_seq: int = 0
    heartbeat_s: int = 20


class ReplayCompletePayload(BaseModel):
    through_seq: int


class ReplayGapPayload(BaseModel):
    """The cursor predates retention; the client resynchronises from a snapshot."""

    requested_after: int
    oldest_available: int


class ErrorPayload(BaseModel):
    code: str
    message: str
    recoverable: bool = True


class WsTicketRequest(BaseModel):
    run_id: str


class WsTicketResponse(BaseModel):
    """A single-use, run-scoped, 60-second credential (§9.3).

    `ws_url` is returned assembled rather than left to the client to build: the query
    parameter names are part of the protocol, and a client that spells `after_seq` wrong
    silently replays the whole backlog on every reconnect instead of failing loudly.
    """

    ticket: str
    run_id: str
    expires_in: int
    ws_url: str


def envelope(
    run_id: str,
    event_type: EventType | str,
    payload: dict[str, Any] | BaseModel | None = None,
    *,
    seq: int = 0,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build one wire message. The single constructor for the §9.2 envelope."""
    if isinstance(payload, BaseModel):
        body = payload.model_dump(mode="json")
    else:
        body = dict(payload or {})
    return {
        "v": PROTOCOL_VERSION,
        "seq": seq,
        "run_id": str(run_id),
        "ts": ts or now_rfc3339(),
        "type": str(event_type),
        "payload": body,
    }


def matches_filter(event_type: str, wanted: set[str] | None) -> bool:
    """Whether `event_type` passes a client's `subscribe` filter.

    `None` means no filter was ever sent, which is not the same as an empty filter: a
    client that sends `{"types": []}` has asked for nothing and gets nothing, while a
    client that never subscribed gets everything. Prefix entries ending in `.` or `*`
    match a family, so a dashboard asks for `node.` rather than enumerating five names
    that a later protocol version will make six.
    """
    if wanted is None:
        return True
    if event_type in wanted:
        return True
    return any(
        event_type.startswith(w.rstrip("*")) for w in wanted if w.endswith((".", "*"))
    )


__all__ = [
    "CONTROL_EVENTS",
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "TERMINAL_EVENTS",
    "ClientMessageType",
    "CloseCode",
    "ErrorPayload",
    "EventEnvelope",
    "EventType",
    "HelloPayload",
    "ReplayCompletePayload",
    "ReplayGapPayload",
    "WsTicketRequest",
    "WsTicketResponse",
    "envelope",
    "matches_filter",
    "now_rfc3339",
]
