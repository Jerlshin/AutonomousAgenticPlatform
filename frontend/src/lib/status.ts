/**
 * Status → tone and glyph, in one place.
 *
 * Three different vocabularies reach the UI and they are not the same axis, which is
 * exactly why they are mapped here rather than in each view that renders one:
 *
 * * `TaskStatus` — the durable column, five members;
 * * `RunStatusDetail` — the §5.3 state machine, nine, carried in `status_detail`;
 * * `RunOutcome` — how the run *ended*, four, and orthogonal to the first two: a
 *   `COMPLETED` task can carry a `PARTIAL` outcome, which is not a success.
 *
 * Getting the third one wrong is the interesting failure: a dashboard that colours
 * `COMPLETED` green regardless of outcome reports a run that missed half its criteria as
 * a win.
 */

import type { RunOutcome, Tone } from "./types";

/** Glyphs, so state is never carried by colour alone (§10). */
export const STATUS_GLYPH: Record<Tone, string> = {
  running: "▶",
  ok: "✓",
  warn: "⚠",
  fail: "✗",
  idle: "◦",
};

/**
 * The tone for a run's *effective* status.
 *
 * `status_detail` wins when present because it is the more current of the two: the
 * summary hash knows `AWAITING_INPUT` and `PARTIAL` while the durable column still says
 * `RUNNING` or `COMPLETED`.
 */
export function toneForStatus(
  status: string | null | undefined,
  detail?: string | null,
  outcome?: string | null,
): Tone {
  const effective = detail || status || "";
  switch (effective) {
    case "RUNNING":
      return "running";
    case "COMPLETED":
      // A completed run that ended PARTIAL met some criteria and missed others. Painting
      // it the same green as a clean success is the single most misleading thing this
      // dashboard could do.
      return outcome === "PARTIAL" ? "warn" : "ok";
    case "SUCCEEDED":
      return "ok";
    case "PARTIAL":
    case "AWAITING_INPUT":
    case "INTERRUPTED":
      return "warn";
    case "FAILED":
    case "CANCELLED":
      return "fail";
    case "PENDING":
    case "QUEUED":
    default:
      return "idle";
  }
}

export function toneForOutcome(outcome: RunOutcome | string | null | undefined): Tone {
  switch (outcome) {
    case "SUCCEEDED":
      return "ok";
    case "PARTIAL":
      return "warn";
    case "FAILED":
    case "CANCELLED":
      return "fail";
    default:
      return "idle";
  }
}

/** The label an operator reads: the finer state when there is one. */
export function statusLabel(
  status: string | null | undefined,
  detail?: string | null,
): string {
  return detail || status || "—";
}

/** True while the run is doing something, which is what decides polling and pulsing. */
export function isActiveStatus(
  status: string | null | undefined,
  detail?: string | null,
): boolean {
  const effective = detail || status || "";
  return (
    effective === "RUNNING" ||
    effective === "PENDING" ||
    effective === "QUEUED" ||
    effective === "AWAITING_INPUT"
  );
}

/** A budget chip turns `warn` at the threshold `budget.warning` fires at, `fail` at 100%. */
export function toneForBudget(percent: number): Tone {
  if (percent >= 100) return "fail";
  if (percent >= 80) return "warn";
  return "idle";
}
