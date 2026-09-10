"use client";

import clsx from "clsx";
import Link from "next/link";
import {
  kpiDeltas,
  kpiValue,
  meetsTarget,
  toneForKpi,
  type KpiTarget,
} from "@/lib/benchmarks";
import { formatPercent } from "@/lib/format";
import type { BenchmarkKpis, BenchmarkResultRead } from "@/lib/rest";
import { Badge, Chip, Panel, toneText } from "@/components/ui/primitives";

/**
 * The KPI scorecard (§8.10).
 *
 * Every tile carries its target and whether the target is met, because a benchmark
 * number without its threshold is trivia. Pass/fail is never colour alone (§10): each
 * tile states `target ≥ 0.70` and `met` / `missed` in words beside the tone.
 *
 * The trap cases are listed individually rather than folded into the Judgement Score,
 * which is the one presentational rule §8.10 is emphatic about — a trap case is a
 * deliberate impossibility, and `2/3` hides *which one* the platform fumbled. That is the
 * whole diagnostic value of the number.
 */

export function KpiScorecard({
  kpis,
  previous,
  traps,
}: {
  kpis: BenchmarkKpis;
  /** The prior execution's KPIs, when the suite has history. */
  previous: BenchmarkKpis | null;
  traps: BenchmarkResultRead[];
}) {
  const deltas = kpiDeltas(kpis, previous);

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Platform KPIs"
        count={`${kpis.cases_scored} case${kpis.cases_scored === 1 ? "" : "s"} scored`}
        className="border border-line"
      >
        <div className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-2 xl:grid-cols-3">
          {deltas.map((entry) => (
            <KpiTile key={entry.target.key} kpis={kpis} delta={entry} />
          ))}
        </div>
      </Panel>

      <TrapCases traps={traps} judgement={kpis.judgement_score} />
    </div>
  );
}

function KpiTile({
  kpis,
  delta,
}: {
  kpis: BenchmarkKpis;
  delta: ReturnType<typeof kpiDeltas>[number];
}) {
  const { target } = delta;
  const value = kpiValue(kpis, target);
  const met = meetsTarget(value, target);
  const tone = toneForKpi(value, target);

  return (
    <div className="flex flex-col gap-1 border border-line bg-surface px-3 py-2">
      <span className="flex items-baseline justify-between gap-2">
        <span
          className="text-[10px] font-semibold uppercase tracking-widest text-muted"
          title={target.hint}
        >
          {target.label}
        </span>
        {met !== null && (
          <Badge tone={met ? "ok" : "fail"}>{met ? "met" : "missed"}</Badge>
        )}
      </span>

      <span className={clsx("tnum text-xl font-semibold", toneText(tone))}>
        {renderValue(kpis, target)}
      </span>

      {target.target !== null && value !== null ? (
        <Gauge value={value} target={target} tone={tone} />
      ) : (
        <span className="text-[10px] text-idle">No platform target.</span>
      )}

      {delta.delta !== null && delta.delta !== 0 && (
        <span
          className={clsx(
            "tnum text-[10px]",
            delta.improved ? "text-ok" : "text-fail",
          )}
          title="Change from this suite's previous execution"
        >
          {delta.delta > 0 ? "▲" : "▼"} {formatDelta(delta.delta, target)} vs previous
        </span>
      )}
    </div>
  );
}

function renderValue(kpis: BenchmarkKpis, target: KpiTarget): string {
  if (target.key === "judgement_score") return kpis.judgement_score;
  if (target.key === "expectations_met") {
    return `${kpis.expectations_met}/${kpis.cases_scored}`;
  }
  const value = kpiValue(kpis, target);
  if (value === null) return "—";
  return target.kind === "count" ? value.toFixed(2) : formatPercent(value, 1);
}

function formatDelta(delta: number, target: KpiTarget): string {
  const magnitude = Math.abs(delta);
  return target.kind === "count"
    ? magnitude.toFixed(2)
    : `${(magnitude * 100).toFixed(1)} pp`;
}

/**
 * A bar with the threshold marked (§8.10).
 *
 * The scale runs to whichever is larger of the value and the target, with headroom, so a
 * "lower is better" KPI that blows past its target still renders inside the bar instead of
 * clipping at 100% and looking like a pass.
 */
function Gauge({
  value,
  target,
  tone,
}: {
  value: number;
  target: KpiTarget;
  tone: "ok" | "fail" | "idle" | "warn" | "running";
}) {
  const limit = target.target ?? 1;
  const scale = target.kind === "count" ? Math.max(value, limit) * 1.25 || 1 : 1;
  const fill = Math.min(100, Math.max(0, (value / scale) * 100));
  const marker = Math.min(100, Math.max(0, (limit / scale) * 100));

  return (
    <span className="flex flex-col gap-0.5">
      <span
        className="relative block h-1.5 w-full rounded-full bg-raised"
        role="img"
        aria-label={`${target.label}: ${value.toFixed(3)}, target ${target.direction === "higher" ? "at least" : "at most"} ${limit.toFixed(2)}`}
      >
        <span
          className={clsx(
            "absolute inset-y-0 left-0 rounded-full",
            tone === "ok" ? "bg-ok" : tone === "fail" ? "bg-fail" : "bg-idle",
          )}
          style={{ width: `${fill}%` }}
        />
        <span
          className="absolute inset-y-[-2px] w-px bg-fg/70"
          style={{ left: `${marker}%` }}
        />
      </span>
      <span className="tnum text-[10px] text-idle">
        target {target.direction === "higher" ? "≥" : "≤"}{" "}
        {target.kind === "count" ? limit.toFixed(2) : formatPercent(limit, 0)}
      </span>
    </span>
  );
}

/**
 * The trap cases, one row each.
 *
 * A trap case that *passed* means the platform correctly refused an impossibility, so the
 * tone is inverted from what a glance suggests — hence the explicit "refused" / "fell for
 * it" wording rather than a bare pass/fail chip.
 */
function TrapCases({
  traps,
  judgement,
}: {
  traps: BenchmarkResultRead[];
  judgement: string;
}) {
  return (
    <Panel
      title="Trap cases"
      count={judgement}
      className="border border-line"
      right={
        <span className="text-[11px] text-idle">
          A trap case is a deliberate impossibility; succeeding at one is the failure.
        </span>
      }
    >
      {traps.length === 0 ? (
        <p className="px-3 py-4 text-xs text-muted">
          This suite declares no trap cases, so the Judgement Score is{" "}
          <span className="font-mono">n/a</span>. A suite with no impossibility never tests
          whether the platform can say no.
        </p>
      ) : (
        <ul className="flex flex-col">
          {traps.map((trap) => (
            <li
              key={trap.id}
              className="flex items-center gap-2 border-b border-line/60 px-3 py-1.5 text-xs last:border-b-0"
            >
              <Chip tone={trap.passed ? "ok" : "fail"} dot={false}>
                {trap.passed ? "✓ refused" : "✗ fell for it"}
              </Chip>
              <span className="min-w-0 flex-1 truncate font-mono text-fg">
                {trap.case_id}
              </span>
              <span className="text-muted">{trap.outcome ?? "—"}</span>
              {trap.run_id && (
                <Link
                  href={`/runs/${trap.run_id}`}
                  className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-raised hover:text-fg"
                >
                  Run
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
