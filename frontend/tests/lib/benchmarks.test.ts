/**
 * KPI targets and run-over-run comparison (§8.10, §13).
 *
 * `computeKpis` itself is covered by the contract test against the shared fixture; what is
 * tested here is everything built on top of it — whether a target is met, and the
 * per-case transitions that make the results table a regression report rather than a
 * snapshot. The pass→fail row is the one the view exists to surface, so it gets the most
 * attention.
 */

import { describe, expect, it } from "vitest";
import {
  compareRuns,
  kpiDeltas,
  kpiValue,
  KPI_TARGETS,
  meetsTarget,
  parseFraction,
  readChecks,
  resultIsTrap,
  toneForKpi,
} from "@/lib/benchmarks";
import type { BenchmarkKpis, BenchmarkResultRead } from "@/lib/rest";

function result(overrides: Partial<BenchmarkResultRead> = {}): BenchmarkResultRead {
  return {
    id: crypto.randomUUID(),
    suite: "core",
    case_id: "iris",
    task_id: null,
    run_id: null,
    outcome: "SUCCEEDED",
    passed: true,
    metrics: {},
    checks: [],
    duration_seconds: 10,
    created_at: "2026-03-01T12:00:00+00:00",
    ...overrides,
  };
}

function kpis(overrides: Partial<BenchmarkKpis> = {}): BenchmarkKpis {
  return {
    cases_scored: 10,
    expectations_met: 8,
    task_success_rate: 0.8,
    judgement_score: "2/3",
    mean_debug_iterations: 1.2,
    first_pass_rate: 0.5,
    replan_rate: 0.2,
    ...overrides,
  };
}

const target = (key: string) => KPI_TARGETS.find((entry) => entry.key === key)!;

describe("KPI targets", () => {
  it("reads the judgement score out of its string ratio", () => {
    expect(parseFraction("2/3")).toBeCloseTo(2 / 3);
    expect(parseFraction("n/a")).toBeNull();
    expect(parseFraction("0/0")).toBeNull();
  });

  it("derives expectations met as a ratio of cases scored", () => {
    expect(kpiValue(kpis(), target("expectations_met"))).toBeCloseTo(0.8);
  });

  it("treats a higher-is-better target as met at the boundary", () => {
    const success = target("task_success_rate");
    expect(meetsTarget(0.7, success)).toBe(true);
    expect(meetsTarget(0.6999, success)).toBe(false);
  });

  it("treats a lower-is-better target as met at the boundary", () => {
    const debug = target("mean_debug_iterations");
    expect(meetsTarget(1.5, debug)).toBe(true);
    expect(meetsTarget(1.51, debug)).toBe(false);
  });

  it("has no verdict for a KPI with no platform target", () => {
    // first_pass_rate is a diagnostic, not a gate: inventing a threshold would turn a
    // neutral number into a spurious failure.
    expect(target("first_pass_rate").target).toBeNull();
    expect(meetsTarget(0.1, target("first_pass_rate"))).toBeNull();
    expect(toneForKpi(0.1, target("first_pass_rate"))).toBe("idle");
  });

  it("reports an unreadable judgement score as neutral rather than failed", () => {
    const value = kpiValue(kpis({ judgement_score: "n/a" }), target("judgement_score"));
    expect(value).toBeNull();
    expect(toneForKpi(value, target("judgement_score"))).toBe("idle");
  });
});

describe("kpiDeltas", () => {
  it("marks a rise in a higher-is-better KPI as an improvement", () => {
    const deltas = kpiDeltas(kpis({ task_success_rate: 0.9 }), kpis());
    const entry = deltas.find((d) => d.target.key === "task_success_rate")!;
    expect(entry.delta).toBeCloseTo(0.1);
    expect(entry.improved).toBe(true);
  });

  it("marks a rise in a lower-is-better KPI as a regression", () => {
    const deltas = kpiDeltas(kpis({ replan_rate: 0.4 }), kpis());
    const entry = deltas.find((d) => d.target.key === "replan_rate")!;
    expect(entry.improved).toBe(false);
  });

  it("has no delta without a previous execution", () => {
    expect(kpiDeltas(kpis(), null).every((entry) => entry.delta === null)).toBe(true);
  });
});

describe("compareRuns", () => {
  it("pairs each case's newest result with the one before it", () => {
    const comparison = compareRuns([
      result({ case_id: "iris", passed: false, created_at: "2026-03-02T12:00:00Z" }),
      result({ case_id: "iris", passed: true, created_at: "2026-03-01T12:00:00Z" }),
    ]);
    expect(comparison.cases).toHaveLength(1);
    expect(comparison.cases[0]!.latest.passed).toBe(false);
    expect(comparison.cases[0]!.previous!.passed).toBe(true);
  });

  it("identifies a pass to fail transition as a regression", () => {
    const comparison = compareRuns([
      result({ case_id: "iris", passed: false, created_at: "2026-03-02T12:00:00Z" }),
      result({ case_id: "iris", passed: true, created_at: "2026-03-01T12:00:00Z" }),
    ]);
    expect(comparison.cases[0]!.transition).toBe("regressed");
    expect(comparison.regressions.map((entry) => entry.caseId)).toEqual(["iris"]);
  });

  it("identifies a fail to pass transition as a gain", () => {
    const comparison = compareRuns([
      result({ case_id: "iris", passed: true, created_at: "2026-03-02T12:00:00Z" }),
      result({ case_id: "iris", passed: false, created_at: "2026-03-01T12:00:00Z" }),
    ]);
    expect(comparison.cases[0]!.transition).toBe("gained");
    expect(comparison.gains).toHaveLength(1);
    expect(comparison.regressions).toHaveLength(0);
  });

  it("calls a case with one recorded result new, not unchanged", () => {
    const comparison = compareRuns([result({ case_id: "iris" })]);
    expect(comparison.cases[0]!.transition).toBe("new");
    expect(comparison.hasPrevious).toBe(false);
  });

  it("ignores results ordered oldest-first in the input", () => {
    // The API returns newest-first, but nothing in the type says so, and a comparison
    // that silently inverts when the order changes would report every pass as a
    // regression.
    const ascending = [
      result({ case_id: "iris", passed: true, created_at: "2026-03-01T12:00:00Z" }),
      result({ case_id: "iris", passed: false, created_at: "2026-03-02T12:00:00Z" }),
    ];
    expect(compareRuns(ascending).cases[0]!.transition).toBe("regressed");
  });

  it("keeps cases independent when only one was re-run", () => {
    const comparison = compareRuns([
      result({ case_id: "iris", passed: false, created_at: "2026-03-02T12:00:00Z" }),
      result({ case_id: "iris", passed: true, created_at: "2026-03-01T12:00:00Z" }),
      result({ case_id: "titanic", passed: true, created_at: "2026-03-01T11:00:00Z" }),
    ]);
    const byCase = Object.fromEntries(
      comparison.cases.map((entry) => [entry.caseId, entry.transition]),
    );
    expect(byCase).toEqual({ iris: "regressed", titanic: "new" });
    // The latest set is one row per case, which is what the KPIs are defined over.
    expect(comparison.latest).toHaveLength(2);
    expect(comparison.previous).toHaveLength(1);
  });

  it("handles an empty history", () => {
    const comparison = compareRuns([]);
    expect(comparison.cases).toEqual([]);
    expect(comparison.hasPrevious).toBe(false);
  });
});

describe("trap identification and checks", () => {
  it("treats a -trap suffix or a PARTIAL outcome as a trap", () => {
    expect(resultIsTrap(result({ case_id: "leakage-trap" }))).toBe(true);
    expect(resultIsTrap(result({ case_id: "iris", outcome: "PARTIAL" }))).toBe(true);
    expect(resultIsTrap(result({ case_id: "iris" }))).toBe(false);
  });

  it("narrows a well-formed check", () => {
    const checks = readChecks({
      checks: [{ name: "metric:r2", passed: false, detail: "0.61 < 0.70" }],
    });
    expect(checks[0]).toEqual({
      name: "metric:r2",
      passed: false,
      detail: "0.61 < 0.70",
    });
  });

  it("reads a malformed check as failed rather than throwing", () => {
    // A malformed scoring record should be visible in the table, not fatal to the page.
    const checks = readChecks({ checks: [{}, { passed: "yes" }] });
    expect(checks).toEqual([
      { name: "check", passed: false, detail: "" },
      { name: "check", passed: false, detail: "" },
    ]);
  });

  it("handles an absent checks array", () => {
    expect(readChecks({})).toEqual([]);
  });
});
