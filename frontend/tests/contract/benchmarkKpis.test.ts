/**
 * The TypeScript KPI mirror agrees with the backend formula (§13, §8.10).
 *
 * `lib/benchmarks.ts::computeKpis` re-implements
 * `backend/app/api/v1/benchmarks.py::compute_kpis`, because the dashboard needs the KPIs
 * of a suite's *previous* execution and the API only ever scores the latest row per case.
 * A mirror that nothing checks is two implementations drifting quietly — showing one
 * success rate on a tile and a different one in the delta underneath it.
 *
 * The fixture is the third statement of the formula, and `backend/tests/test_benchmark_kpis.py`
 * asserts the Python side against the very same file. Changing the formula in one language
 * therefore reddens that language's suite immediately.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { computeKpis, resultIsTrap } from "@/lib/benchmarks";
import type { BenchmarkKpis, BenchmarkResultRead } from "@/lib/rest";

interface Fixture {
  suite: string;
  results: BenchmarkResultRead[];
  kpis: BenchmarkKpis;
}

// Resolved from the Vitest root rather than `import.meta.url`, matching
// graphTopology.test.ts: under jsdom the module URL is not a `file:` URL.
const fixture = JSON.parse(
  readFileSync(join(process.cwd(), "tests/fixtures/benchmark-kpis.json"), "utf8"),
) as Fixture;

describe("computeKpis against the backend contract fixture", () => {
  const computed = computeKpis(fixture.results);

  it("reproduces every integer KPI exactly", () => {
    expect(computed.cases_scored).toBe(fixture.kpis.cases_scored);
    expect(computed.expectations_met).toBe(fixture.kpis.expectations_met);
  });

  it("reproduces the judgement score, which is a string not a ratio", () => {
    expect(computed.judgement_score).toBe(fixture.kpis.judgement_score);
  });

  it("reproduces every rate", () => {
    expect(computed.task_success_rate).toBeCloseTo(fixture.kpis.task_success_rate, 12);
    expect(computed.first_pass_rate).toBeCloseTo(fixture.kpis.first_pass_rate, 12);
    expect(computed.replan_rate).toBeCloseTo(fixture.kpis.replan_rate, 12);
    expect(computed.mean_debug_iterations).toBeCloseTo(
      fixture.kpis.mean_debug_iterations,
      12,
    );
  });

  it("identifies traps by suffix or PARTIAL outcome, as the backend does", () => {
    const traps = fixture.results.filter(resultIsTrap).map((row) => row.case_id);
    expect(traps).toEqual(["imbalance-trap", "leakage-trap"]);
  });

  it("counts a row with no metrics as zero rather than skipping it", () => {
    // `ingest-smoke` has `metrics: {}`. It must still be in the denominator: dropping it
    // would inflate every rate, which is the failure mode of a permissive counter.
    expect(computed.cases_scored).toBe(fixture.results.length);
  });

  it("returns the empty shape rather than dividing by zero", () => {
    const empty = computeKpis([]);
    expect(empty.cases_scored).toBe(0);
    expect(empty.judgement_score).toBe("n/a");
    expect(empty.task_success_rate).toBe(0);
    expect(Number.isNaN(empty.mean_debug_iterations)).toBe(false);
  });
});
