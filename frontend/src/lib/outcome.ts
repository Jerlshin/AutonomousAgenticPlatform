/**
 * A run's outcome, read from the task row.
 *
 * Its own module, and that is a bundle decision rather than a taste one: this helper is
 * used by the task table, the dashboard tiles and the trend chart, and while it lived
 * beside the chart component every one of those imports pulled Recharts (~90 KB) into the
 * route's first-load JS. §9.4's budgets are the enforcement mechanism, and this is
 * exactly the kind of accidental edge they exist to catch.
 */

import type { TaskRead } from "./rest";

/**
 * `SUCCEEDED` | `PARTIAL` | `FAILED` | `CANCELLED` | null.
 *
 * `result.status` is the run's own outcome and wins: the durable column only knows the
 * task *completed*. A `COMPLETED` task whose run ended `PARTIAL` met some criteria and
 * missed others, and reporting that as a success is how a platform claims a success rate
 * it does not have.
 */
export function outcomeOf(task: TaskRead): string | null {
  const result = task.result as { status?: unknown } | null | undefined;
  if (result && typeof result.status === "string") return result.status;
  if (task.status === "COMPLETED") return "SUCCEEDED";
  if (task.status === "FAILED") return "FAILED";
  if (task.status === "CANCELLED") return "CANCELLED";
  return null;
}
