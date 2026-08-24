// GENERATED FILE — DO NOT EDIT.
//
// Source: backend/app/schemas/events.py
// Regenerate: make gen-event-types
//
// The `pluton.v1` wire contract (docs/ARCHITECTURE.md §9). Every name below is the
// backend's own, so renaming an event there and forgetting the frontend is a TypeScript
// error here rather than an `undefined` at runtime (§18.5).

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

/** The §9.2 envelope every server→client message travels in. */
export interface RunEvent<P = Record<string, unknown>> {
  v: typeof PROTOCOL_VERSION;
  /** Gapless and strictly increasing per run, from 1. The resume cursor. Control frames carry 0. */
  seq: number;
  run_id: string;
  /** RFC 3339 UTC, millisecond precision. */
  ts: string;
  type: RunEventType;
  payload: P;
}

/** Sent to the server; see §9.5. */
export interface ClientMessage<P = Record<string, unknown>> {
  type: ClientMessageType;
  payload?: P;
}
