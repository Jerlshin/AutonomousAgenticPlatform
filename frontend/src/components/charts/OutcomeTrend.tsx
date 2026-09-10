"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { outcomeOf } from "@/lib/outcome";
import type { TaskRead } from "@/lib/rest";

/**
 * Outcomes per day, stacked (§8.2).
 *
 * Two rules from §9.3 and §10 shape this rather than the chart library's defaults:
 *
 * * **the data is pre-aggregated before it reaches Recharts.** Recharts recomputing
 *   scales over the raw rows every frame is the same bug as an unbounded array wearing a
 *   different hat.
 * * **outcome categories map to the platform's own tones**, not to a chart palette.
 *   `ok`/`warn`/`fail` mean the same thing here as they do in the criteria ledger, and a
 *   chart that invents its own colour for "failed" teaches operators that the colours do
 *   not mean anything.
 *
 * Imported dynamically by its parent so Recharts stays out of the first-load bundle
 * (§9.4).
 */

interface Day {
  day: string;
  label: string;
  succeeded: number;
  partial: number;
  failed: number;
}

/** How many days the trend covers. Beyond this the bars are too thin to read. */
const WINDOW_DAYS = 14;

export function buildTrend(tasks: readonly TaskRead[], now = Date.now()): Day[] {
  const buckets = new Map<string, Day>();
  const dayFormat = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short" });

  for (let offset = WINDOW_DAYS - 1; offset >= 0; offset--) {
    const date = new Date(now - offset * 86_400_000);
    const day = date.toISOString().slice(0, 10);
    buckets.set(day, {
      day,
      label: dayFormat.format(date),
      succeeded: 0,
      partial: 0,
      failed: 0,
    });
  }

  for (const task of tasks) {
    const day = (task.updated_at ?? "").slice(0, 10);
    const bucket = buckets.get(day);
    if (!bucket) continue;
    // `result.status` is the run's own outcome; the column only knows it completed. A
    // COMPLETED task that ended PARTIAL is not a success, and stacking it as one would
    // make the trend line lie in the direction everyone wants to believe.
    const outcome = outcomeOf(task);
    if (outcome === "SUCCEEDED") bucket.succeeded += 1;
    else if (outcome === "PARTIAL") bucket.partial += 1;
    else if (outcome === "FAILED" || outcome === "CANCELLED") bucket.failed += 1;
  }

  return [...buckets.values()];
}

export default function OutcomeTrend({ tasks }: { tasks: readonly TaskRead[] }) {
  const data = buildTrend(tasks);
  const empty = data.every((day) => day.succeeded + day.partial + day.failed === 0);

  if (empty) {
    return (
      <p className="px-3 py-8 text-center text-xs text-muted">
        No runs finished in the last {WINDOW_DAYS} days.
      </p>
    );
  }

  return (
    <div className="h-40 w-full px-1 pb-1">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -24 }}>
          <CartesianGrid stroke="var(--color-line)" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fill: "var(--color-muted)", fontSize: 10 }}
            stroke="var(--color-line)"
            interval="preserveStartEnd"
          />
          <YAxis
            allowDecimals={false}
            tick={{ fill: "var(--color-muted)", fontSize: 10 }}
            stroke="var(--color-line)"
          />
          <Tooltip
            cursor={{ fill: "var(--color-raised)" }}
            contentStyle={{
              background: "var(--color-raised)",
              border: "1px solid var(--color-line)",
              borderRadius: 4,
              fontSize: 11,
            }}
          />
          <Legend wrapperStyle={{ fontSize: 10, color: "var(--color-muted)" }} />
          <Bar dataKey="succeeded" stackId="o" fill="var(--color-ok)" name="Succeeded" />
          <Bar dataKey="partial" stackId="o" fill="var(--color-warn)" name="Partial" />
          <Bar dataKey="failed" stackId="o" fill="var(--color-fail)" name="Failed" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
