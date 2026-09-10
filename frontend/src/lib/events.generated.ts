// GENERATED FILE — DO NOT EDIT.
//
// Source: backend/app/schemas/events.py
// Regenerate: make gen-event-types
//
// The `pluton.v1` wire contract (docs/ARCHITECTURE.md §9, docs/FRONTEND.md §4.3). Every
// name below is the backend's own, so renaming an event or a payload field there and
// forgetting the frontend is a TypeScript error here rather than an `undefined` at
// runtime (§18.5).

import type { RunRead } from "./rest";

export const PROTOCOL = "pluton.v1" as const;
export const PROTOCOL_VERSION = 1 as const;

/** Every server→client event type (§9.4). */
export type RunEventType =
  | "hello"
  | "ping"
  | "replay.complete"
  | "replay.gap"
  | "error"
  | "run.snapshot"
  | "run.queued"
  | "run.started"
  | "run.phase"
  | "run.completed"
  | "run.failed"
  | "run.cancelled"
  | "node.started"
  | "node.progress"
  | "node.completed"
  | "node.failed"
  | "node.retrying"
  | "token.delta"
  | "tool.started"
  | "tool.completed"
  | "retrieval.results"
  | "plan.created"
  | "plan.revised"
  | "code.revision"
  | "sandbox.started"
  | "sandbox.stdout"
  | "sandbox.stderr"
  | "sandbox.truncated"
  | "sandbox.exit"
  | "artifact.created"
  | "metric.logged"
  | "evaluation.completed"
  | "interrupt.requested"
  | "budget.warning";

/** Every client→server message type (§9.5). */
export type ClientMessageType =
  | "auth"
  | "pong"
  | "resync"
  | "cancel"
  | "approve"
  | "subscribe";

/** Frames describing the connection rather than the run. They carry `seq: 0` and are never replayed. */
export const CONTROL_EVENTS: readonly RunEventType[] = [
  "error",
  "hello",
  "ping",
  "replay.complete",
  "replay.gap",
  "run.snapshot",
] as const;

/** After one of these the run is over and the reconnect loop stops (§9.8). */
export const TERMINAL_EVENTS: readonly RunEventType[] = [
  "run.cancelled",
  "run.completed",
  "run.failed",
] as const;

/** WebSocket close codes (§9.7). The client's reconnect policy keys off these. */
export const CloseCode = {
  NORMAL: 1000,
  GOING_AWAY: 1001,
  INTERNAL_ERROR: 1011,
  PROTOCOL_ERROR: 4400,
  UNAUTHENTICATED: 4401,
  FORBIDDEN: 4403,
  NOT_FOUND: 4404,
  QUOTA_EXCEEDED: 4429,
} as const;

// ── Shared payload objects ─────────────────────────────────────────────────

/** One checked criterion. `observed is None` means the metric was never produced. */
export interface CriterionResultRef {
  criterion_id: string;
  metric: string;
  comparator: string;
  threshold: number;
  observed: number | null;
  passed: boolean;
  required: boolean;
  weight: number;
  note: string;
}

/** One produced file, as `Deliverable` in `engine/state.py` serialises it. */
export interface DeliverableRef {
  artifact_id: string | null;
  name: string;
  artifact_type: "code" | "model" | "plot" | "report" | "metrics" | "log" | "bundle";
  path: string;
  sha256: string;
  size_bytes: number;
  mime_type: string;
}

/** `Verdict` in `engine/state.py`. */
export interface EvaluationRef {
  decision: "ACCEPT" | "REFINE" | "REPLAN" | "ABORT";
  passed: boolean;
  score: number;
  criteria_results: CriterionResultRef[];
  rubric: RubricScoreRef[];
  rubric_mean: number | null;
  replan_directive: string | null;
  refine_directive: string | null;
  summary: string;
}

/** `MLflowRef` in `engine/state.py` (MLOPS.md §4). */
export interface MlflowRef {
  experiment_id: string;
  experiment_name: string;
  run_id: string;
  parent_run_id: string | null;
  artifact_uri: string;
  ui_url: string;
  logged_metrics: Record<string, number>;
  logged_params: Record<string, string>;
  registered_model: string | null;
  model_version: string | null;
}

export interface NodeErrorRef {
  kind: string;
  message: string;
  /** Stable across incidental detail — the key the stagnation rule counts. */
  fingerprint: string;
}

export interface PlanStepRef {
  id: string;
  index: number;
  title: string;
  description: string;
  kind: "research" | "implement" | "train" | "evaluate" | "report";
  depends_on: string[];
  acceptance: string[];
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED";
  attempts: number;
  notes: string;
}

export interface RetrievalHitRef {
  source_uri: string;
  section: string;
  score: number;
  title: string;
  collection: string;
  trust_level: string;
}

/** Advisory only. It never influences `passed` (AGENTS.md §7.6). */
export interface RubricScoreRef {
  dimension: string;
  score: number;
  justification: string;
}

export interface SandboxLimitsRef {
  cpus: number | null;
  memory: string;
  timeout_s: number;
  network: string;
}

/** The Planner's contract for one criterion, before anything has been measured. */
export interface SuccessCriterionRef {
  id: string;
  metric: string;
  comparator: "gte" | "lte" | "gt" | "lt" | "eq" | "approx";
  threshold: number;
  tolerance: number;
  required: boolean;
  weight: number;
  rationale: string;
}

/** `Usage` in `engine/state.py`, as the terminal payload serialises it. */
export interface UsageRef {
  tokens_in: number;
  tokens_out: number;
  llm_calls: number;
  node_visits: number;
  sandbox_executions: number;
  started_at: string | null;
}

// ── Per-event payloads ─────────────────────────────────────────────────────

/** The first frame after accept. `last_seq` tells a fresh client where the run is. */
export interface HelloPayload {
  protocol: "pluton.v1";
  run: Record<string, unknown>;
  last_seq: number;
  heartbeat_s: number;
}

/** Empty. Present so every event type has a registry entry. */
export type PingPayload = Record<string, never>;

export interface ReplayCompletePayload {
  through_seq: number;
}

/** The cursor predates retention; the client resynchronises from a snapshot. */
export interface ReplayGapPayload {
  requested_after: number;
  oldest_available: number;
}

export interface ErrorPayload {
  code: string;
  message: string;
  recoverable: boolean;
}

/** The full `GET /runs/{id}` body.  Deliberately open: the authority on this shape is `RunRead` in `schemas/run.py`, which OpenAPI already exports, and re-declaring its fields here would be exactly the hand-written duplicate §4.1 forbids. The generator special-cases this one and emits the `RunRead` alias from `rest.ts` instead of an interface built from this model. */
export type RunSnapshotPayload = RunRead;

export interface RunQueuedPayload {
  position: number;
}

export interface RunStartedPayload {
  worker_id: string;
  resumed: boolean;
  /** Role → model, e.g. {"planner": "qwen2.5:14b-instruct"} (ARCHITECTURE.md §11.1). */
  model_routing: Record<string, string>;
}

export interface RunPhasePayload {
  phase: string;
  previous_phase: string | null;
}

/** Terminal `SUCCEEDED` / `PARTIAL`. `worker/jobs._terminal_payload` builds it. */
export interface RunCompletedPayload {
  status: string;
  deliverables: DeliverableRef[];
  bundle_url: string | null;
  evaluation: EvaluationRef | null;
  mlflow: MlflowRef | null;
  usage: UsageRef | null;
}

/** Terminal `FAILED`, and the `INTERRUPTED` frame the reaper emits (`worker/cron.py`). */
export interface RunFailedPayload {
  status: string;
  error: string | null;
  last_node: string | null;
  dossier_url: string | null;
  /** True on the reaper's frame: the run stopped with a worker, not with itself. */
  resumable: boolean;
  deliverables: DeliverableRef[];
  bundle_url: string | null;
  evaluation: EvaluationRef | null;
  mlflow: MlflowRef | null;
  usage: UsageRef | null;
}

/** Terminal `CANCELLED`, from the worker or from `POST /runs/{id}/cancel`. */
export interface RunCancelledPayload {
  status: string;
  reason: string;
  cancelled_by: "operator" | "system";
  deliverables: DeliverableRef[];
  bundle_url: string | null;
  evaluation: EvaluationRef | null;
  mlflow: MlflowRef | null;
  usage: UsageRef | null;
}

export interface NodeStartedPayload {
  node: string;
  agent: string;
  phase: string;
  model: string | null;
  plan_step_id: string | null;
  step_seq: number | null;
}

export interface NodeProgressPayload {
  node: string;
  message: string;
  percent: number | null;
}

export interface NodeCompletedPayload {
  node: string;
  duration_ms: number;
  tokens_in: number;
  tokens_out: number;
  llm_calls: number;
  /** The deterministic fallback ran. Materially different from success (AGENTS.md §8.2). */
  degraded: boolean;
  summary: string;
  step_seq: number | null;
}

export interface NodeFailedPayload {
  node: string;
  error: NodeErrorRef;
  will_retry: boolean;
  policy: string;
  step_seq: number | null;
}

export interface NodeRetryingPayload {
  node: string;
  attempt: number;
  max_attempts: number;
  backoff_ms: number | null;
}

/** Coalesced at 80 ms / 64 chars by `RunEmitter.emit_token` (§9.1). */
export interface TokenDeltaPayload {
  node: string;
  text: string;
}

export interface ToolStartedPayload {
  node: string;
  tool: string;
  args_digest: string;
}

export interface ToolCompletedPayload {
  node: string;
  tool: string;
  duration_ms: number;
  ok: boolean;
  result_digest: string;
}

export interface RetrievalResultsPayload {
  query: string;
  hits: RetrievalHitRef[];
  node: string;
}

export interface PlanCreatedPayload {
  steps: PlanStepRef[];
  success_criteria: SuccessCriterionRef[];
  task_kind: string;
  primary_metric: string;
  assumptions: string[];
  revision: number;
}

export interface PlanRevisedPayload {
  steps: PlanStepRef[];
  success_criteria: SuccessCriterionRef[];
  task_kind: string;
  primary_metric: string;
  assumptions: string[];
  revision: number;
  diff: string;
  reason: string;
}

export interface CodeRevisionPayload {
  revision: number;
  path: string;
  language: string;
  sha256: string;
  lines_changed: number;
  /** Unified diff against the previous revision. Empty for revision 1. */
  diff: string;
  rationale: string;
  /** The ErrorRecord.fingerprint this revision was written against. */
  addresses_error: string | null;
}

export interface SandboxStartedPayload {
  execution_id: string;
  profile: "exec" | "train" | "train-tracked";
  revision: number;
  image: string;
  limits: SandboxLimitsRef;
}

/** `sandbox.stdout` and `sandbox.stderr`. Truncated to 4 KiB per frame (§9.1). */
export interface SandboxLinePayload {
  execution_id: string;
  line: string;
  ts: string;
}

export interface SandboxTruncatedPayload {
  execution_id: string;
  stream: "stdout" | "stderr";
  bytes_dropped: number;
}

export interface SandboxExitPayload {
  execution_id: string;
  exit_code: number | null;
  timed_out: boolean;
  oom_killed: boolean;
  duration_ms: number;
  max_rss_bytes: number | null;
}

export interface ArtifactCreatedPayload {
  artifact_id: string;
  name: string;
  type: "code" | "model" | "plot" | "report" | "metrics" | "log" | "bundle";
  size_bytes: number;
  sha256: string;
  download_url: string | null;
}

export interface MetricLoggedPayload {
  key: string;
  value: number;
  step: number;
  mlflow_run_id: string;
}

/** The Evaluator's verdict. Same shape as `EvaluationRef`, sent on its own. */
export interface EvaluationCompletedPayload {
  decision: "ACCEPT" | "REFINE" | "REPLAN" | "ABORT";
  passed: boolean;
  score: number;
  criteria_results: CriterionResultRef[];
  rubric: RubricScoreRef[];
  rubric_mean: number | null;
  replan_directive: string | null;
  refine_directive: string | null;
  summary: string;
}

/** A HITL gate opened. Expiry is treated as rejection (AGENTS.md §9). */
export interface InterruptRequestedPayload {
  gate: "after_plan" | "before_sandbox_exec" | "before_model_registration" | "on_replan";
  prompt: string;
  options: string[];
  /** RFC 3339. The run is CANCELLED if no decision arrives by then. */
  expires_at: string;
  /** Gate-specific body: the plan for after_plan, the source for before_sandbox_exec. */
  context: Record<string, unknown>;
}

/** Any budget crossing 80% (`RunEmitter.budget_warning`). */
export interface BudgetWarningPayload {
  resource: string;
  used: number;
  limit: number;
  percent: number;
}

/** Every event type bound to the payload it carries (§9.4). */
export interface RunEventMap {
  "hello": HelloPayload;
  "ping": PingPayload;
  "replay.complete": ReplayCompletePayload;
  "replay.gap": ReplayGapPayload;
  "error": ErrorPayload;
  "run.snapshot": RunSnapshotPayload;
  "run.queued": RunQueuedPayload;
  "run.started": RunStartedPayload;
  "run.phase": RunPhasePayload;
  "run.completed": RunCompletedPayload;
  "run.failed": RunFailedPayload;
  "run.cancelled": RunCancelledPayload;
  "node.started": NodeStartedPayload;
  "node.progress": NodeProgressPayload;
  "node.completed": NodeCompletedPayload;
  "node.failed": NodeFailedPayload;
  "node.retrying": NodeRetryingPayload;
  "token.delta": TokenDeltaPayload;
  "tool.started": ToolStartedPayload;
  "tool.completed": ToolCompletedPayload;
  "retrieval.results": RetrievalResultsPayload;
  "plan.created": PlanCreatedPayload;
  "plan.revised": PlanRevisedPayload;
  "code.revision": CodeRevisionPayload;
  "sandbox.started": SandboxStartedPayload;
  "sandbox.stdout": SandboxLinePayload;
  "sandbox.stderr": SandboxLinePayload;
  "sandbox.truncated": SandboxTruncatedPayload;
  "sandbox.exit": SandboxExitPayload;
  "artifact.created": ArtifactCreatedPayload;
  "metric.logged": MetricLoggedPayload;
  "evaluation.completed": EvaluationCompletedPayload;
  "interrupt.requested": InterruptRequestedPayload;
  "budget.warning": BudgetWarningPayload;
}

/**
 * The §9.2 envelope, discriminated on `type`.
 *
 * Narrowing on `event.type` narrows `event.payload` with it, which is what makes the
 * store's fold an exhaustive `switch` rather than a pile of `String(p.x ?? "")` (§4.3).
 */
export type RunEvent = {
  [K in keyof RunEventMap]: {
    v: typeof PROTOCOL_VERSION;
    /** Gapless and strictly increasing per run, from 1. The resume cursor. Control frames carry 0. */
    seq: number;
    run_id: string;
    /** RFC 3339 UTC, millisecond precision. */
    ts: string;
    type: K;
    payload: RunEventMap[K];
  };
}[keyof RunEventMap];

/** One member of the union, by event name: `RunEventOf<"node.started">`. */
export type RunEventOf<K extends RunEventType> = Extract<RunEvent, { type: K }>;

/** Sent to the server; see §9.5. */
export interface ClientMessage<P = Record<string, unknown>> {
  type: ClientMessageType;
  payload?: P;
}
