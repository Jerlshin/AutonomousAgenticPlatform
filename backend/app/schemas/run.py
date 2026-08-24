"""Run lifecycle payloads (ARCHITECTURE.md §8.2, §8.3).

**A Task is a Run, for now.** §7.1 gives `runs` its own table keyed off `tasks.id`, and
§21 marks that ORM `⬜`. Until it exists this package keeps the convention `experiments`
already established and documented: `run_id == task_id`, one active run per task. Every
field §8.3 puts on a run body is present here, sourced either from the `tasks` row or from
the `run:{id}:summary` hash the engine maintains — so when the table lands, the response
shape does not move and no client changes.

The two sources are not redundant. Postgres holds what survives a restart (status, error,
result); Redis holds what changes every few seconds (phase, current node, percent, token
spend) and would cost a write transaction per node boundary to keep in a row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.task import TaskStatus


class RunStatus(BaseModel):
    """The §5.3 state machine as the API reports it.

    `TaskStatus` is the durable column and has five members; the state machine has nine.
    The extra four (`QUEUED`, `AWAITING_INPUT`, `PARTIAL`, `INTERRUPTED`) are carried in
    the summary hash and surfaced through `RunRead.status_detail` rather than being
    forced into an enum a migration has not added yet.
    """

    status: TaskStatus
    detail: str | None = None


class RunRead(BaseModel):
    """Run detail — the body `GET /runs/{run_id}` returns and `run.snapshot` carries."""

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    task_id: uuid.UUID
    title: str
    prompt: str
    status: TaskStatus
    status_detail: str | None = Field(
        default=None,
        description="The §5.3 state when it is finer than the durable column: QUEUED, AWAITING_INPUT, PARTIAL, INTERRUPTED.",
    )
    phase: str | None = None
    current_node: str | None = None
    percent: float | None = None
    outcome: str | None = None
    last_seq: int = 0
    worker_id: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    node_visits: int = 0
    debug_iterations: int = 0
    replan_count: int = 0
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    ws_url: str


class RunAccepted(BaseModel):
    """`202 Accepted` from `POST /tasks/{task_id}/runs` (§5.2).

    Carries `ws_url` so the browser can open the stream without knowing how the path is
    assembled, and `already_running` so an idempotent replay of the same request is
    distinguishable from a fresh dispatch by something other than luck.
    """

    run_id: uuid.UUID
    task_id: uuid.UUID
    status: str = "QUEUED"
    job_id: str | None = None
    ws_url: str
    already_running: bool = False


class RunCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class RunApproveRequest(BaseModel):
    """Release a HITL gate (§9.5's `approve`)."""

    gate: str
    decision: str = Field(pattern="^(approve|reject)$")
    notes: str | None = Field(default=None, max_length=2000)


class RunEvent(BaseModel):
    """One entry of the durable log, in the §9.2 envelope shape."""

    v: int = 1
    seq: int
    run_id: str
    ts: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunEventsResponse(BaseModel):
    """`GET /runs/{run_id}/events?after_seq=` — the same backlog the WebSocket replays.

    Exists so a client that cannot hold a socket open (curl, a test, a notebook) reads the
    identical history a browser does, from the identical source. A second code path that
    reconstructed events from database rows would drift from the stream the moment either
    side changed.
    """

    run_id: uuid.UUID
    total: int
    after_seq: int
    last_seq: int
    oldest_available: int | None = None
    gap: bool = Field(
        default=False,
        description="True when `after_seq` predates retention: history the caller asked for has been trimmed.",
    )
    events: list[RunEvent]


class RunListResponse(BaseModel):
    total: int
    runs: list[RunRead]


__all__ = [
    "RunAccepted",
    "RunApproveRequest",
    "RunCancelRequest",
    "RunEvent",
    "RunEventsResponse",
    "RunListResponse",
    "RunRead",
    "RunStatus",
]
