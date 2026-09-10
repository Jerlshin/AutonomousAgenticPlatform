"use client";

import clsx from "clsx";
import { useMemo, useState } from "react";
import { formatComparator, formatMetric } from "@/lib/format";
import type { CriterionRow, Tone } from "@/lib/types";
import { useRunShallow, selectCriteria } from "@/hooks/useRunSlice";
import { Badge, Chip } from "@/components/ui/primitives";

/**
 * The criteria ledger (§8.5.4) — the platform's objectivity claim made visible.
 *
 * ```
 * ── CRITERIA ────────────────────────────  score 0.97 · ACCEPT ──
 *  ✅ accuracy    ≥ 0.9500   0.9737   required   w 1.0
 *  ⚠️ roc_auc     ≥ 0.9900   0.9948   optional   w 0.5   stretch goal
 *  ⏳ auc_pr      ≥ 0.9000       —     required   awaiting metrics.json
 * ```
 *
 * Its rules are strict because the numbers here are the ones people quote:
 *
 * * **required first, and visually distinct from optional.** A failed optional criterion
 *   is not a failed run (AGENTS.md §3.2), and a ledger that mixes them reports one as the
 *   other.
 * * **the comparator is rendered literally.** "Target 0.95" hides whether the bound is
 *   inclusive, and `approx` carries a tolerance that changes what passing means.
 * * **`observed === null` after a verdict is a failure, not a pending row.** Absence is
 *   not success (AGENTS.md §7.6) — a metric the run never produced is a metric it never
 *   met, and rendering it as "still working" is the single most flattering lie this
 *   component could tell.
 * * **the score and the decision are different quantities**, labelled as such, because
 *   operators conflate them.
 * * **the LLM rubric is advisory and collapsed.** It never influences `passed`; rendering
 *   it beside the hard result implies otherwise.
 */

export function CriteriaLedger() {
  const { criteria, verdict, planRevision } = useRunShallow(selectCriteria);
  const [rubricOpen, setRubricOpen] = useState(false);

  // Required first, then by metric name — memoized outside the render body's hot path so
  // the sort does not re-run on every parent commit (§9.3).
  const sorted = useMemo(
    () =>
      [...criteria].sort((a, b) => {
        if (a.required !== b.required) return a.required ? -1 : 1;
        return a.metric.localeCompare(b.metric);
      }),
    [criteria],
  );

  if (criteria.length === 0) {
    return (
      <div className="shrink-0 border-t border-line px-3 py-2 text-[11px] text-muted">
        No success criteria yet — the Planner emits them with the plan.
      </div>
    );
  }

  return (
    <section
      aria-label="Success criteria"
      className="flex shrink-0 flex-col border-t border-line bg-ink"
    >
      <header className="flex items-center justify-between gap-2 px-3 py-1">
        <h3 className="text-[10px] font-semibold uppercase tracking-widest text-muted">
          Criteria
          {planRevision > 1 && (
            <span className="ml-2 rounded bg-warn/15 px-1 text-[9px] font-semibold tracking-normal text-warn">
              revised (revision {planRevision})
            </span>
          )}
        </h3>
        {verdict && (
          <span className="flex items-center gap-2">
            <span
              className="tnum text-[11px] text-muted"
              title="The weighted satisfaction of the success criteria. Not the same quantity as the decision."
            >
              score {formatMetric(verdict.score)}
            </span>
            <Badge
              tone={decisionTone(verdict.decision)}
              title="The Evaluator's routing decision, from the criteria and the budget state."
            >
              {verdict.decision}
            </Badge>
          </span>
        )}
      </header>

      {planRevision > 1 && (
        <p className="px-3 pb-1 text-[10px] text-warn">
          The plan was revised — these criteria replaced the previous contract. Numbers
          before the revision were measured against different targets.
        </p>
      )}

      <ul className="max-h-40 overflow-auto px-1 pb-1">
        {sorted.map((row) => (
          <CriterionLine key={row.id} row={row} />
        ))}
      </ul>

      {verdict && verdict.rubric.length > 0 && (
        <div className="border-t border-line/60">
          <button
            type="button"
            onClick={() => setRubricOpen((open) => !open)}
            aria-expanded={rubricOpen}
            className="flex w-full items-center gap-2 px-3 py-1 text-left text-[10px] uppercase tracking-widest text-muted transition-colors hover:text-fg"
          >
            <span aria-hidden>{rubricOpen ? "▾" : "▸"}</span>
            Advisory rubric
            {verdict.rubricMean != null && (
              <span className="tnum normal-case tracking-normal text-idle">
                mean {verdict.rubricMean.toFixed(2)} / 5
              </span>
            )}
          </button>
          {rubricOpen && (
            <div className="px-3 pb-2">
              <p className="mb-1 text-[10px] text-idle">
                Advisory only. The rubric is model-generated and never influences whether a
                criterion passed.
              </p>
              <ul className="flex flex-col gap-0.5">
                {verdict.rubric.map((score) => (
                  <li key={score.dimension} className="flex gap-2 text-[11px]">
                    <span className="w-32 shrink-0 font-mono text-muted">
                      {score.dimension}
                    </span>
                    <span className="tnum w-8 shrink-0 text-right">{score.score}/5</span>
                    <span className="min-w-0 flex-1 text-muted">
                      {score.justification}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {verdict?.summary && (
        <p className="border-t border-line/60 px-3 py-1 text-[11px] text-muted">
          {verdict.summary}
        </p>
      )}
    </section>
  );
}

function CriterionLine({ row }: { row: CriterionRow }) {
  const state = criterionState(row);

  return (
    <li
      className={clsx(
        "flex items-center gap-2 px-2 py-0.5 text-[11px]",
        !row.required && "opacity-80",
      )}
      title={row.note || row.rationale || undefined}
    >
      <span className={clsx("w-4 shrink-0 text-center", state.className)} aria-hidden>
        {state.glyph}
      </span>
      <span className="w-28 shrink-0 truncate font-mono">{row.metric}</span>
      <span className="tnum w-24 shrink-0 text-muted">
        {formatComparator(row.comparator, row.threshold, row.tolerance)}
      </span>
      <span className={clsx("tnum w-20 shrink-0 text-right", state.className)}>
        {row.observed == null ? "—" : formatMetric(row.observed)}
      </span>
      <Chip tone={row.required ? "idle" : "idle"} dot={false} className="shrink-0">
        {row.required ? "required" : "optional"}
      </Chip>
      <span className="tnum w-10 shrink-0 text-right text-idle">
        w {row.weight.toFixed(1)}
      </span>
      <span className="min-w-0 flex-1 truncate text-idle">{state.note}</span>
    </li>
  );
}

interface CriterionState {
  glyph: string;
  className: string;
  note: string;
}

/**
 * The four states a criterion can be in, and the one that matters.
 *
 * Before a verdict, an unobserved criterion is **pending** — during `PLANNING` nothing
 * has been measured. After a verdict, an unobserved criterion is **failed**, annotated
 * with why: the metric never reached `metrics.json`, which is a defect in the run, not a
 * state it is still working through.
 */
function criterionState(row: CriterionRow): CriterionState {
  if (!row.verdictSeen) {
    return {
      glyph: "⏳",
      className: "text-idle",
      note: row.observed == null ? "not measured yet" : "",
    };
  }
  if (row.observed == null) {
    return {
      glyph: "✗",
      className: "text-fail",
      note: "metric absent from metrics.json",
    };
  }
  if (row.passed) {
    return { glyph: "✓", className: "text-ok", note: row.note };
  }
  return {
    glyph: row.required ? "✗" : "⚠",
    className: row.required ? "text-fail" : "text-warn",
    note: row.note || (row.required ? "" : "optional target missed"),
  };
}

function decisionTone(decision: string): Tone {
  switch (decision) {
    case "ACCEPT":
      return "ok";
    case "REFINE":
      return "warn";
    case "REPLAN":
      return "running";
    case "ABORT":
      return "fail";
    default:
      return "idle";
  }
}
