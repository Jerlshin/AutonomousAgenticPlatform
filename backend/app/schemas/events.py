"""The `pluton.v1` wire contract (ARCHITECTURE.md §9.2, §9.4, §9.5).

One module holds every event name, the envelope they travel in, and the client messages
that come back, because all three are the same contract seen from different sides. The
frontend's TypeScript union is *generated* from this file by `scripts/gen_event_types.py`
(`make fe-types`), so renaming an event here is a frontend compile error rather than a
runtime `undefined` — which is the whole point of §18.5.

`EventEnvelope.payload` stays `dict[str, Any]` on the wire path: the envelope is written
by the emitter and read by a browser, and validating every frame would add a step on the
hot path — `token.delta` fires several times a second per run — to catch mistakes the
emitter call sites cannot make, since each one constructs its payload literally next to
the `EventType` it names.

What the `PAYLOADS` registry below adds is the *schema* of each of those payloads without
putting validation on that path. It is the source `scripts/gen_event_types.py` reads to
emit a discriminated `RunEvent` union, so `event.payload.node` is a `string` in the
frontend's store fold and a field renamed here is a TypeScript error there rather than a
runtime `undefined` (docs/FRONTEND.md §4.3). `tests/` asserts every `EventType` has an
entry, which is what keeps the registry from drifting behind the enum.

Emitters SHOULD construct these models rather than dict literals; where one still builds a
dict, the model remains the documented shape of what it builds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


# ------------------------------------------------------------------------------------
#  Per-event payload models  (§9.4)
# ------------------------------------------------------------------------------------
#
# One model per `EventType`, bound to it by `PAYLOADS` at the bottom of this section.
# They exist so `scripts/gen_event_types.py` can emit a discriminated union rather than
# thirty `Record<string, unknown>`s — see the module docstring. Field names are the
# emitter's own; where the emitter is not built yet (§15's B2), the model is the shape
# ARCHITECTURE.md §9.4 specifies and the emitter must match it when it lands.
#
# Nested models are shared where the same object appears in more than one payload
# (`DeliverableRef` in three terminal events, `CriterionResultRef` in the verdict), which
# keeps the generated TypeScript to one interface per concept.


class PingPayload(BaseModel):
    """Empty. Present so every event type has a registry entry."""


class RunSnapshotPayload(BaseModel):
    """The full `GET /runs/{id}` body.

    Deliberately open: the authority on this shape is `RunRead` in `schemas/run.py`,
    which OpenAPI already exports, and re-declaring its fields here would be exactly the
    hand-written duplicate §4.1 forbids. The generator special-cases this one and emits
    the `RunRead` alias from `rest.ts` instead of an interface built from this model.
    """

    model_config = ConfigDict(extra="allow")


class RunQueuedPayload(BaseModel):
    position: int = 0


class RunStartedPayload(BaseModel):
    worker_id: str = ""
    resumed: bool = False
    model_routing: dict[str, str] = Field(
        default_factory=dict,
        description='Role → model, e.g. {"planner": "qwen2.5:14b-instruct"} (ARCHITECTURE.md §11.1).',
    )


class RunPhasePayload(BaseModel):
    phase: str
    previous_phase: str | None = None


class DeliverableRef(BaseModel):
    """One produced file, as `Deliverable` in `engine/state.py` serialises it."""

    artifact_id: str | None = None
    name: str
    artifact_type: Literal["code", "model", "plot", "report", "metrics", "log", "bundle"]
    path: str
    sha256: str
    size_bytes: int
    mime_type: str


class CriterionResultRef(BaseModel):
    """One checked criterion. `observed is None` means the metric was never produced."""

    criterion_id: str
    metric: str
    comparator: str
    threshold: float
    observed: float | None = None
    passed: bool
    required: bool = True
    weight: float = 1.0
    note: str = ""


class SuccessCriterionRef(BaseModel):
    """The Planner's contract for one criterion, before anything has been measured."""

    id: str
    metric: str
    comparator: Literal["gte", "lte", "gt", "lt", "eq", "approx"]
    threshold: float
    tolerance: float = 0.0
    required: bool = True
    weight: float = 1.0
    rationale: str = ""


class RubricScoreRef(BaseModel):
    """Advisory only. It never influences `passed` (AGENTS.md §7.6)."""

    dimension: str
    score: int
    justification: str


class EvaluationRef(BaseModel):
    """`Verdict` in `engine/state.py`."""

    decision: Literal["ACCEPT", "REFINE", "REPLAN", "ABORT"]
    passed: bool
    score: float
    criteria_results: list[CriterionResultRef] = Field(default_factory=list)
    rubric: list[RubricScoreRef] = Field(default_factory=list)
    rubric_mean: float | None = None
    replan_directive: str | None = None
    refine_directive: str | None = None
    summary: str = ""


class MlflowRef(BaseModel):
    """`MLflowRef` in `engine/state.py` (MLOPS.md §4)."""

    experiment_id: str = ""
    experiment_name: str = ""
    run_id: str = ""
    parent_run_id: str | None = None
    artifact_uri: str = ""
    ui_url: str = ""
    logged_metrics: dict[str, float] = Field(default_factory=dict)
    logged_params: dict[str, str] = Field(default_factory=dict)
    registered_model: str | None = None
    model_version: str | None = None


class UsageRef(BaseModel):
    """`Usage` in `engine/state.py`, as the terminal payload serialises it."""

    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    node_visits: int = 0
    sandbox_executions: int = 0
    started_at: str | None = None


class RunCompletedPayload(BaseModel):
    """Terminal `SUCCEEDED` / `PARTIAL`. `worker/jobs._terminal_payload` builds it."""

    status: str
    deliverables: list[DeliverableRef] = Field(default_factory=list)
    bundle_url: str | None = None
    evaluation: EvaluationRef | None = None
    mlflow: MlflowRef | None = None
    usage: UsageRef | None = None


class RunFailedPayload(BaseModel):
    """Terminal `FAILED`, and the `INTERRUPTED` frame the reaper emits (`worker/cron.py`)."""

    status: str
    error: str | None = None
    last_node: str | None = None
    dossier_url: str | None = None
    resumable: bool = Field(
        default=False,
        description="True on the reaper's frame: the run stopped with a worker, not with itself.",
    )
    deliverables: list[DeliverableRef] = Field(default_factory=list)
    bundle_url: str | None = None
    evaluation: EvaluationRef | None = None
    mlflow: MlflowRef | None = None
    usage: UsageRef | None = None


class RunCancelledPayload(BaseModel):
    """Terminal `CANCELLED`, from the worker or from `POST /runs/{id}/cancel`."""

    status: str = "CANCELLED"
    reason: str = ""
    cancelled_by: Literal["operator", "system"] = "operator"
    deliverables: list[DeliverableRef] = Field(default_factory=list)
    bundle_url: str | None = None
    evaluation: EvaluationRef | None = None
    mlflow: MlflowRef | None = None
    usage: UsageRef | None = None


class NodeStartedPayload(BaseModel):
    node: str
    agent: str = ""
    phase: str = ""
    model: str | None = None
    plan_step_id: str | None = None
    step_seq: int | None = None


class NodeProgressPayload(BaseModel):
    node: str
    message: str = ""
    percent: float | None = None


class NodeCompletedPayload(BaseModel):
    node: str
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    degraded: bool = Field(
        default=False,
        description="The deterministic fallback ran. Materially different from success (AGENTS.md §8.2).",
    )
    summary: str = ""
    step_seq: int | None = None


class NodeErrorRef(BaseModel):
    kind: str
    message: str
    fingerprint: str = Field(
        default="",
        description="Stable across incidental detail — the key the stagnation rule counts.",
    )


class NodeFailedPayload(BaseModel):
    node: str
    error: NodeErrorRef
    will_retry: bool = False
    policy: str = ""
    step_seq: int | None = None


class NodeRetryingPayload(BaseModel):
    node: str
    attempt: int
    max_attempts: int
    backoff_ms: int | None = None


class TokenDeltaPayload(BaseModel):
    """Coalesced at 80 ms / 64 chars by `RunEmitter.emit_token` (§9.1)."""

    node: str
    text: str


class ToolStartedPayload(BaseModel):
    node: str
    tool: str
    args_digest: str = ""


class ToolCompletedPayload(BaseModel):
    node: str
    tool: str
    duration_ms: int = 0
    ok: bool = True
    result_digest: str = ""


class RetrievalHitRef(BaseModel):
    source_uri: str = ""
    section: str = ""
    score: float = 0.0
    title: str = ""
    collection: str = ""
    trust_level: str = "curated"


class RetrievalResultsPayload(BaseModel):
    query: str
    hits: list[RetrievalHitRef] = Field(default_factory=list)
    node: str = "researcher"


class PlanStepRef(BaseModel):
    id: str
    index: int = 0
    title: str = ""
    description: str = ""
    kind: Literal["research", "implement", "train", "evaluate", "report"] = "implement"
    depends_on: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"] = "PENDING"
    attempts: int = 0
    notes: str = ""


class PlanCreatedPayload(BaseModel):
    steps: list[PlanStepRef] = Field(default_factory=list)
    success_criteria: list[SuccessCriterionRef] = Field(default_factory=list)
    task_kind: str = ""
    primary_metric: str = ""
    assumptions: list[str] = Field(default_factory=list)
    revision: int = 1


class PlanRevisedPayload(BaseModel):
    steps: list[PlanStepRef] = Field(default_factory=list)
    success_criteria: list[SuccessCriterionRef] = Field(default_factory=list)
    task_kind: str = ""
    primary_metric: str = ""
    assumptions: list[str] = Field(default_factory=list)
    revision: int = 2
    diff: str = ""
    reason: str = ""


class CodeRevisionPayload(BaseModel):
    revision: int
    path: str = "main.py"
    language: str = "python"
    sha256: str = ""
    lines_changed: int = 0
    diff: str = Field(
        default="",
        description="Unified diff against the previous revision. Empty for revision 1.",
    )
    rationale: str = ""
    addresses_error: str | None = Field(
        default=None, description="The ErrorRecord.fingerprint this revision was written against."
    )


class SandboxLimitsRef(BaseModel):
    cpus: float | None = None
    memory: str = ""
    timeout_s: int = 0
    network: str = ""


class SandboxStartedPayload(BaseModel):
    execution_id: str
    profile: Literal["exec", "train", "train-tracked"] = "exec"
    revision: int = 0
    image: str = ""
    limits: SandboxLimitsRef = Field(default_factory=SandboxLimitsRef)


class SandboxLinePayload(BaseModel):
    """`sandbox.stdout` and `sandbox.stderr`. Truncated to 4 KiB per frame (§9.1)."""

    execution_id: str = ""
    line: str
    ts: str = ""


class SandboxTruncatedPayload(BaseModel):
    execution_id: str
    stream: Literal["stdout", "stderr"]
    bytes_dropped: int


class SandboxExitPayload(BaseModel):
    execution_id: str
    exit_code: int | None = None
    timed_out: bool = False
    oom_killed: bool = False
    duration_ms: int = 0
    max_rss_bytes: int | None = None


class ArtifactCreatedPayload(BaseModel):
    artifact_id: str = ""
    name: str
    type: Literal["code", "model", "plot", "report", "metrics", "log", "bundle"] = "log"
    size_bytes: int = 0
    sha256: str = ""
    download_url: str | None = None


class MetricLoggedPayload(BaseModel):
    key: str
    value: float
    step: int = 0
    mlflow_run_id: str = ""


class EvaluationCompletedPayload(BaseModel):
    """The Evaluator's verdict. Same shape as `EvaluationRef`, sent on its own."""

    decision: Literal["ACCEPT", "REFINE", "REPLAN", "ABORT"]
    passed: bool
    score: float
    criteria_results: list[CriterionResultRef] = Field(default_factory=list)
    rubric: list[RubricScoreRef] = Field(default_factory=list)
    rubric_mean: float | None = None
    replan_directive: str | None = None
    refine_directive: str | None = None
    summary: str = ""


class InterruptRequestedPayload(BaseModel):
    """A HITL gate opened. Expiry is treated as rejection (AGENTS.md §9)."""

    gate: Literal[
        "after_plan",
        "before_sandbox_exec",
        "before_model_registration",
        "on_replan",
    ]
    prompt: str = ""
    options: list[str] = Field(default_factory=list)
    expires_at: str = Field(
        default="",
        description="RFC 3339. The run is CANCELLED if no decision arrives by then.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Gate-specific body: the plan for after_plan, the source for before_sandbox_exec.",
    )


class BudgetWarningPayload(BaseModel):
    """Any budget crossing 80% (`RunEmitter.budget_warning`)."""

    resource: str
    used: float
    limit: float
    percent: float


# Every `EventType` bound to the model describing its payload. A test asserts this map is
# total over `EventType`, which is what makes the generated union total too.
PAYLOADS: dict[EventType, type[BaseModel]] = {
    EventType.HELLO: HelloPayload,
    EventType.PING: PingPayload,
    EventType.REPLAY_COMPLETE: ReplayCompletePayload,
    EventType.REPLAY_GAP: ReplayGapPayload,
    EventType.ERROR: ErrorPayload,
    EventType.RUN_SNAPSHOT: RunSnapshotPayload,
    EventType.RUN_QUEUED: RunQueuedPayload,
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.RUN_PHASE: RunPhasePayload,
    EventType.RUN_COMPLETED: RunCompletedPayload,
    EventType.RUN_FAILED: RunFailedPayload,
    EventType.RUN_CANCELLED: RunCancelledPayload,
    EventType.NODE_STARTED: NodeStartedPayload,
    EventType.NODE_PROGRESS: NodeProgressPayload,
    EventType.NODE_COMPLETED: NodeCompletedPayload,
    EventType.NODE_FAILED: NodeFailedPayload,
    EventType.NODE_RETRYING: NodeRetryingPayload,
    EventType.TOKEN_DELTA: TokenDeltaPayload,
    EventType.TOOL_STARTED: ToolStartedPayload,
    EventType.TOOL_COMPLETED: ToolCompletedPayload,
    EventType.RETRIEVAL_RESULTS: RetrievalResultsPayload,
    EventType.PLAN_CREATED: PlanCreatedPayload,
    EventType.PLAN_REVISED: PlanRevisedPayload,
    EventType.CODE_REVISION: CodeRevisionPayload,
    EventType.SANDBOX_STARTED: SandboxStartedPayload,
    EventType.SANDBOX_STDOUT: SandboxLinePayload,
    EventType.SANDBOX_STDERR: SandboxLinePayload,
    EventType.SANDBOX_TRUNCATED: SandboxTruncatedPayload,
    EventType.SANDBOX_EXIT: SandboxExitPayload,
    EventType.ARTIFACT_CREATED: ArtifactCreatedPayload,
    EventType.METRIC_LOGGED: MetricLoggedPayload,
    EventType.EVALUATION_COMPLETED: EvaluationCompletedPayload,
    EventType.INTERRUPT_REQUESTED: InterruptRequestedPayload,
    EventType.BUDGET_WARNING: BudgetWarningPayload,
}


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
    "PAYLOADS",
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "TERMINAL_EVENTS",
    "ArtifactCreatedPayload",
    "BudgetWarningPayload",
    "ClientMessageType",
    "CloseCode",
    "CodeRevisionPayload",
    "CriterionResultRef",
    "DeliverableRef",
    "ErrorPayload",
    "EvaluationCompletedPayload",
    "EvaluationRef",
    "EventEnvelope",
    "EventType",
    "HelloPayload",
    "InterruptRequestedPayload",
    "MetricLoggedPayload",
    "MlflowRef",
    "NodeCompletedPayload",
    "NodeErrorRef",
    "NodeFailedPayload",
    "NodeProgressPayload",
    "NodeRetryingPayload",
    "NodeStartedPayload",
    "PingPayload",
    "PlanCreatedPayload",
    "PlanRevisedPayload",
    "PlanStepRef",
    "ReplayCompletePayload",
    "ReplayGapPayload",
    "RetrievalHitRef",
    "RetrievalResultsPayload",
    "RubricScoreRef",
    "RunCancelledPayload",
    "RunCompletedPayload",
    "RunFailedPayload",
    "RunPhasePayload",
    "RunQueuedPayload",
    "RunSnapshotPayload",
    "RunStartedPayload",
    "SandboxExitPayload",
    "SandboxLimitsRef",
    "SandboxLinePayload",
    "SandboxStartedPayload",
    "SandboxTruncatedPayload",
    "SuccessCriterionRef",
    "TokenDeltaPayload",
    "ToolCompletedPayload",
    "ToolStartedPayload",
    "UsageRef",
    "WsTicketRequest",
    "WsTicketResponse",
    "envelope",
    "matches_filter",
    "now_rfc3339",
]
