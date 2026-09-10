/**
 * Benchmark KPI targets, trap-case identification, and run-over-run comparison (§8.10).
 *
 * Pure and React-free so the comparison logic is testable without mounting a table.
 *
 * One thing here needs stating plainly, because it is the kind of duplication that rots
 * silently: `computeKpis` **mirrors** `backend/app/api/v1/benchmarks.py::compute_kpis`.
 * It exists because the API computes KPIs only over the *latest* result per case, and
 * §8.10 requires KPI deltas between the latest suite execution and the one before it —
 * a number no endpoint returns. A contract test folds the API's own `results` and asserts
 * the output equals the `kpis` the API sent alongside them, so a change to the backend
 * formula fails the suite here rather than quietly showing two different success rates on
 * the same screen. If that test fails, this function is what is wrong; fix it here.
 */

import type { BenchmarkKpis, BenchmarkResultRead } from "./rest";
import type { Tone } from "./types";

/** `api/v1/benchmarks.py::TRAP_SUFFIX`. */
export const TRAP_SUFFIX = "-trap";

// ── Targets ──────────────────────────────────────────────────────────────────

export interface KpiTarget {
  key: keyof BenchmarkKpis;
  label: string;
  /** The §13.1 platform target, or `null` where the KPI is tracked but not targeted. */
  target: number | null;
  direction: "higher" | "lower";
  /** How to render the value; `judgement_score` arrives as a string like `2/3`. */
  kind: "ratio" | "rate" | "count" | "fraction";
  hint: string;
}

/**
 * `AGENTS.md §13.1` / `ARCHITECTURE.md §17`, as §8.10 tabulates them.
 *
 * `first_pass_rate` has no target on purpose — it is a diagnostic, not a gate — and
 * inventing one so every tile could show a pass/fail chip would turn a neutral number
 * into a spurious failure.
 */
export const KPI_TARGETS: readonly KpiTarget[] = [
  {
    key: "task_success_rate",
    label: "Task success rate",
    target: 0.7,
    direction: "higher",
    kind: "rate",
    hint: "Cases whose outcome was SUCCEEDED, over cases scored.",
  },
  {
    key: "judgement_score",
    label: "Judgement score",
    target: 2 / 3,
    direction: "higher",
    kind: "fraction",
    hint: "Trap cases correctly refused. A trap case is a deliberate impossibility; succeeding at one is the failure.",
  },
  {
    key: "mean_debug_iterations",
    label: "Mean debug iterations",
    target: 1.5,
    direction: "lower",
    kind: "count",
    hint: "Averaged over succeeded cases only.",
  },
  {
    key: "first_pass_rate",
    label: "First-pass rate",
    target: null,
    direction: "higher",
    kind: "rate",
    hint: "Succeeded with zero debug iterations, over cases scored.",
  },
  {
    key: "replan_rate",
    label: "Replan rate",
    target: 0.25,
    direction: "lower",
    kind: "rate",
    hint: "Cases that replanned at least once, over cases scored.",
  },
  {
    key: "expectations_met",
    label: "Expectations met",
    target: null,
    direction: "higher",
    kind: "ratio",
    hint: "Cases whose every declared expectation held, over cases scored.",
  },
] as const;

/** A `2/3`-style score as a number, or `null` for the API's `"n/a"`. */
export function parseFraction(value: string): number | null {
  const match = /^(\d+)\s*\/\s*(\d+)$/.exec(value.trim());
  if (!match) return null;
  const denominator = Number(match[2]);
  if (denominator === 0) return null;
  return Number(match[1]) / denominator;
}

/** The numeric value of a KPI, normalised so targets can be compared uniformly. */
export function kpiValue(kpis: BenchmarkKpis, target: KpiTarget): number | null {
  if (target.key === "judgement_score") return parseFraction(kpis.judgement_score);
  if (target.key === "expectations_met") {
    return kpis.cases_scored === 0 ? null : kpis.expectations_met / kpis.cases_scored;
  }
  const raw = kpis[target.key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

export function meetsTarget(value: number | null, target: KpiTarget): boolean | null {
  if (value === null || target.target === null) return null;
  return target.direction === "higher" ? value >= target.target : value <= target.target;
}

export function toneForKpi(value: number | null, target: KpiTarget): Tone {
  const met = meetsTarget(value, target);
  if (met === null) return "idle";
  return met ? "ok" : "fail";
}

// ── Trap cases ───────────────────────────────────────────────────────────────

/**
 * Whether a result is a trap case.
 *
 * Mirrors the backend's convention: the `-trap` suffix, or a `PARTIAL` outcome, which is
 * what a correctly-refused impossibility records. `caseIsTrap` from the suite definition
 * is the better signal when the suite is loaded, so the table prefers it and falls back
 * to this.
 */
export function resultIsTrap(row: BenchmarkResultRead): boolean {
  return row.case_id.endsWith(TRAP_SUFFIX) || row.outcome === "PARTIAL";
}

// ── The KPI mirror ───────────────────────────────────────────────────────────

function counter(row: BenchmarkResultRead, key: string): number {
  const value = (row.metrics ?? {})[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/**
 * `compute_kpis` in TypeScript. See this module's header before editing.
 *
 * Takes an already-deduplicated set — one row per case — because that is what the KPI
 * targets are defined over.
 */
export function computeKpis(rows: readonly BenchmarkResultRead[]): BenchmarkKpis {
  const total = rows.length;
  if (total === 0) {
    return {
      cases_scored: 0,
      expectations_met: 0,
      task_success_rate: 0,
      judgement_score: "n/a",
      mean_debug_iterations: 0,
      first_pass_rate: 0,
      replan_rate: 0,
    };
  }

  const succeeded = rows.filter((row) => row.outcome === "SUCCEEDED");
  const traps = rows.filter(resultIsTrap);

  return {
    cases_scored: total,
    expectations_met: rows.filter((row) => row.passed).length,
    task_success_rate: succeeded.length / total,
    judgement_score:
      traps.length > 0
        ? `${traps.filter((row) => row.passed).length}/${traps.length}`
        : "n/a",
    mean_debug_iterations:
      succeeded.length > 0
        ? succeeded.reduce((sum, row) => sum + counter(row, "debug_iterations"), 0) /
          succeeded.length
        : 0,
    first_pass_rate:
      succeeded.filter((row) => !counter(row, "debug_iterations")).length / total,
    replan_rate: rows.filter((row) => counter(row, "replans")).length / total,
  };
}

// ── Run-over-run comparison ──────────────────────────────────────────────────

export type Transition = "gained" | "regressed" | "unchanged" | "new";

export interface CaseComparison {
  caseId: string;
  latest: BenchmarkResultRead;
  previous: BenchmarkResultRead | null;
  transition: Transition;
}

export interface SuiteComparison {
  latest: BenchmarkResultRead[];
  previous: BenchmarkResultRead[];
  cases: CaseComparison[];
  /** Cases that went pass → fail. The rows that matter (§8.10). */
  regressions: CaseComparison[];
  gains: CaseComparison[];
  hasPrevious: boolean;
}

function byNewestFirst(a: BenchmarkResultRead, b: BenchmarkResultRead): number {
  return Date.parse(b.created_at) - Date.parse(a.created_at);
}

/**
 * Split a suite's history into "this execution" and "the one before".
 *
 * Grouped per case rather than by wall-clock batch: rows carry no execution id, and
 * cases in one suite run finish minutes apart, so any time-window clustering would split
 * a slow run in half and report every case in it as new. Per case, the newest row is the
 * current result and the one before it is what to compare against — which is exactly the
 * transition §8.10 asks for, and it stays correct when a single case is re-run alone.
 */
export function compareRuns(rows: readonly BenchmarkResultRead[]): SuiteComparison {
  const byCase = new Map<string, BenchmarkResultRead[]>();
  for (const row of rows) {
    const bucket = byCase.get(row.case_id);
    if (bucket) bucket.push(row);
    else byCase.set(row.case_id, [row]);
  }

  const cases: CaseComparison[] = [];
  for (const [caseId, bucket] of byCase) {
    const sorted = [...bucket].sort(byNewestFirst);
    const latest = sorted[0]!;
    const previous = sorted[1] ?? null;
    cases.push({
      caseId,
      latest,
      previous,
      transition:
        previous === null
          ? "new"
          : previous.passed === latest.passed
            ? "unchanged"
            : latest.passed
              ? "gained"
              : "regressed",
    });
  }

  cases.sort((a, b) => a.caseId.localeCompare(b.caseId));

  return {
    latest: cases.map((entry) => entry.latest),
    previous: cases
      .map((entry) => entry.previous)
      .filter((row): row is BenchmarkResultRead => row !== null),
    cases,
    regressions: cases.filter((entry) => entry.transition === "regressed"),
    gains: cases.filter((entry) => entry.transition === "gained"),
    hasPrevious: cases.some((entry) => entry.previous !== null),
  };
}

export interface KpiDelta {
  target: KpiTarget;
  latest: number | null;
  previous: number | null;
  delta: number | null;
  /** Whether the movement is an improvement, accounting for `direction`. */
  improved: boolean | null;
}

export function kpiDeltas(
  latest: BenchmarkKpis,
  previous: BenchmarkKpis | null,
): KpiDelta[] {
  return KPI_TARGETS.map((target) => {
    const now = kpiValue(latest, target);
    const before = previous ? kpiValue(previous, target) : null;
    const delta = now !== null && before !== null ? now - before : null;
    return {
      target,
      latest: now,
      previous: before,
      delta,
      improved:
        delta === null || delta === 0
          ? null
          : target.direction === "higher"
            ? delta > 0
            : delta < 0,
    };
  });
}

// ── Expectation checks ───────────────────────────────────────────────────────

export interface BenchmarkCheck {
  name: string;
  passed: boolean;
  detail: string;
}

/**
 * Narrow one entry of `BenchmarkResultRead.checks`.
 *
 * The backend types it `list[dict[str, Any]]`, so OpenAPI gives us
 * `{[key: string]: unknown}[]` and every reader would otherwise cast. The source shape is
 * `services/benchmarks.py::CheckResult.as_dict` — `{name, passed, detail}`. An entry that
 * does not match reads as a failed check with no detail rather than throwing: a malformed
 * row in a scoring record should be visible in the table, not fatal to the page.
 */
export function readCheck(entry: Record<string, unknown>): BenchmarkCheck {
  return {
    name: typeof entry.name === "string" && entry.name !== "" ? entry.name : "check",
    passed: entry.passed === true,
    detail: typeof entry.detail === "string" ? entry.detail : "",
  };
}

export function readChecks(
  row: Pick<BenchmarkResultRead, "checks">,
): BenchmarkCheck[] {
  return (row.checks ?? []).map(readCheck);
}
