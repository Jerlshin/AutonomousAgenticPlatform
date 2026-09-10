/**
 * View models — the client's own folding of the stream, and nothing else.
 *
 * docs/FRONTEND.md §4.1: *no type describing a backend payload may be written by hand.*
 * Everything that crosses the wire lives in `rest.ts` (generated from OpenAPI) or
 * `events.generated.ts` (generated from `schemas/events.py`). What remains here is what
 * §4.5 exempts:
 *
 * * **view models** — `ConsoleLine`, `TimelineEntry` and friends are folded from many
 *   events and correspond to no backend model;
 * * **static domain tables** — `RunPhase`, `RunOutcome` and the gate names are
 *   transcribed from `AGENTS.md` and carry a comment naming the section they came from.
 *
 * The five hand-written REST interfaces this file used to carry (`RunDetail`,
 * `TaskSummary`, `TaskListResponse`, `RunAccepted`, `WsTicket`) were deleted in phase 0;
 * they are aliases in `rest.ts` now.
 */

import type { RunEventType } from "./events.generated";

/**
 * The five status tones of §10.
 *
 * Lives here rather than beside the components so `lib/status.ts` can map a run status to
 * a tone without importing a React module — `lib/` is for things with no React
 * dependency, and that rule is what keeps the pure helpers testable in isolation.
 *
 * A tone is never the *only* carrier of state: §10 requires a glyph or a label with it,
 * because the five include a red/green pair.
 */
export type Tone = "running" | "ok" | "warn" | "fail" | "idle";

// ── Static domain tables (AGENTS.md §3.1, transcribed) ───────────────────────

/**
 * The coarse progress signal every node sets on entry.
 *
 * Transcribed from `RunPhase` in `backend/app/engine/state.py`. It is not in OpenAPI —
 * `RunRead.phase` is a bare `string | null`, because the value is projected out of the
 * Redis summary hash rather than a typed column.
 */
export type RunPhase =
  | "INIT"
  | "PLANNING"
  | "RESEARCH"
  | "IMPLEMENT"
  | "EXECUTE"
  | "DEBUG"
  | "TRACK"
  | "EVALUATE"
  | "REPORT"
  | "COMPLETE";

/** Phase order, for the header's phase strip and duration attribution. */
export const PHASE_ORDER: readonly RunPhase[] = [
  "INIT",
  "PLANNING",
  "RESEARCH",
  "IMPLEMENT",
  "EXECUTE",
  "DEBUG",
  "TRACK",
  "EVALUATE",
  "REPORT",
  "COMPLETE",
] as const;

/** `RunOutcome` in `engine/state.py`. Not the same axis as `TaskStatus`. */
export type RunOutcome = "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED";

/**
 * The §5.3 states finer than the durable `tasks.status` column.
 *
 * `RunRead.status_detail` carries one of these when the summary hash knows something the
 * column cannot express. Hand-written because no backend model enumerates it — the
 * column's own enum (`TaskStatus` in `rest.ts`) has five members and the state machine
 * has nine.
 */
export type RunStatusDetail =
  | "QUEUED"
  | "RUNNING"
  | "AWAITING_INPUT"
  | "COMPLETED"
  | "PARTIAL"
  | "FAILED"
  | "CANCELLED"
  | "INTERRUPTED";

/** The four HITL gates of `AGENTS.md` §9. Metadata for each lives in `lib/gates.ts`. */
export type GateName =
  | "after_plan"
  | "before_sandbox_exec"
  | "before_model_registration"
  | "on_replan";

/** Decisions `RunApproveRequest` accepts today: it validates `^(approve|reject)$`. */
export type GateDecision = "approve" | "reject";

// ── Console (§8.5.5) ─────────────────────────────────────────────────────────

export type ConsoleStream = "stdout" | "stderr" | "token" | "marker";

/**
 * One line as the console pane holds it.
 *
 * `id` is a monotonic client-side counter rather than `seq`, because one `sandbox.stdout`
 * event is one line but one `token.delta` may be *appended to* the line before it (§8.5.5:
 * a 4000-token plan must not produce 4000 rows). React keys off `id`, never an index.
 */
export interface ConsoleLine {
  id: number;
  stream: ConsoleStream;
  text: string;
  ts: string;
  node?: string;
  executionId?: string;
  /** The event that produced the line, for the `data-seq` attribute and the debug drawer. */
  seq: number;
  /** Marker lines are the client's own notices and MUST look different from program output. */
  tone?: "warn" | "fail" | "info";
}

// ── Timeline (§8.5.4) ────────────────────────────────────────────────────────

export type TimelineStatus = "running" | "succeeded" | "failed" | "degraded" | "gate";

/** One node visit. Loop 1 visits `coder` several times, so this is not keyed by node. */
export interface TimelineEntry {
  /** `${seq}` of the `node.started` that opened it — stable across replay. */
  id: string;
  node: string;
  agent: string;
  seq: number;
  status: TimelineStatus;
  startedAt: string;
  durationMs?: number;
  tokensIn?: number;
  tokensOut?: number;
  llmCalls?: number;
  summary?: string;
  error?: string;
  errorKind?: string;
  /** Set by `node.retrying`, which annotates the open entry rather than adding a row. */
  attempt?: number;
  maxAttempts?: number;
  /** Groups the entry under its plan step's sticky sub-header when the payload carries one. */
  planStepId?: string | null;
  /** `coder` entries correlate to a `code.revision` by revision number. */
  revision?: number;
  /** `sandbox_exec` entries correlate to their container by execution id. */
  executionId?: string;
  /** A resolved HITL gate renders as a first-class timeline entry (§8.6). */
  gate?: GateName;
}

/** A plan step, from `plan.created` / `plan.revised`. */
export interface PlanStepRow {
  id: string;
  index: number;
  title: string;
  description: string;
  kind: string;
  dependsOn: string[];
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED";
  attempts: number;
}

/**
 * One criterion, merged from the plan's contract and the evaluator's observation.
 *
 * `observed === null` *after* a verdict renders as failed, not pending: absence is not
 * success (AGENTS.md §7.6). Before a verdict, `verdictSeen === false` keeps it pending.
 */
export interface CriterionRow {
  id: string;
  metric: string;
  comparator: string;
  threshold: number;
  tolerance: number;
  required: boolean;
  weight: number;
  rationale: string;
  observed: number | null;
  passed: boolean | null;
  note: string;
  /** True once `evaluation.completed` has been folded, which is what makes absence fail. */
  verdictSeen: boolean;
}

/** The evaluator's verdict, minus the criteria (which are merged into `CriterionRow`). */
export interface VerdictRow {
  decision: "ACCEPT" | "REFINE" | "REPLAN" | "ABORT";
  passed: boolean;
  score: number;
  rubric: { dimension: string; score: number; justification: string }[];
  rubricMean: number | null;
  summary: string;
  directive: string | null;
}

// ── Sandbox, artifacts, metrics (§8.5.5, §8.5.6) ─────────────────────────────

export interface SandboxExecutionRow {
  executionId: string;
  profile: string;
  revision: number;
  image: string;
  limits: { cpus: number | null; memory: string; timeoutS: number; network: string };
  startedAt: string;
  exitCode?: number | null;
  timedOut?: boolean;
  oomKilled?: boolean;
  durationMs?: number;
  maxRssBytes?: number | null;
}

export interface ArtifactRow {
  artifactId: string | null;
  name: string;
  type: "code" | "model" | "plot" | "report" | "metrics" | "log" | "bundle";
  sizeBytes: number;
  sha256: string;
  downloadUrl: string | null;
  /** True when the row came from the terminal `result` body rather than a live event. */
  reconstructed: boolean;
}

export interface MetricPoint {
  key: string;
  value: number;
  step: number;
  ts: string;
}

// ── Code, retrieval, diagnosis (§8.7) ────────────────────────────────────────

export interface CodeRevisionRow {
  revision: number;
  path: string;
  language: string;
  sha256: string;
  linesChanged: number;
  diff: string;
  rationale: string;
  addressesError: string | null;
  ts: string;
  /**
   * Full source, when it is available. Dropped before the metadata when the ring evicts
   * (§3.7) — a revision the operator can still see listed is better than one that
   * vanished to save the same bytes.
   */
  content?: string;
}

export interface RetrievalHitRow {
  query: string;
  sourceUri: string;
  section: string;
  title: string;
  score: number;
  collection: string;
  trustLevel: string;
  seq: number;
}

/** Derived from the debugger's `node.completed` summary, pending a typed emitter (B2). */
export interface DiagnosisRow {
  seq: number;
  ts: string;
  summary: string;
  revision: number | null;
}

// ── Operator attention (§8.6) ────────────────────────────────────────────────

export interface PendingGate {
  gate: GateName;
  prompt: string;
  options: string[];
  /** RFC 3339. Expiry is treated as rejection and terminates the run CANCELLED. */
  expiresAt: string | null;
  context: Record<string, unknown>;
  seq: number;
}

export interface GateDecisionRow {
  gate: GateName;
  decision: GateDecision;
  notes: string;
  decidedAt: string;
  via: "socket" | "rest";
}

// ── Graph (§8.5.3) ───────────────────────────────────────────────────────────

export type GraphNodeStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "degraded";

export interface GraphNodeState {
  status: GraphNodeStatus;
  visits: number;
  lastDurationMs?: number;
  lastError?: string;
  lastSummary?: string;
  cumulativeMs: number;
  model?: string;
  retrying?: { attempt: number; maxAttempts: number };
}

// ── Transport (§7.6) ─────────────────────────────────────────────────────────

export interface StreamError {
  kind: "auth" | "forbidden" | "not_found" | "protocol" | "quota" | "network" | "server";
  /** Operator-facing. Never a stack trace. */
  message: string;
  /** The WebSocket close code, when there was one. */
  code?: number;
  retryable: boolean;
}

/** What `cancel` / `approve` resolve to — *delivery*, never *effect* (§7.5). */
export interface CommandResult {
  ok: boolean;
  via: "socket" | "rest";
  error?: string;
}

/** A latency sample for the §9.5 instrumentation reservoir. */
export interface LatencySample {
  type: RunEventType;
  ms: number;
}
