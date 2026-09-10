/**
 * Reading `RunRead.result` — the terminal truth (§2.3, §13).
 *
 * OpenAPI types this payload as a bare `object`, so this module is the only thing standing
 * between a backend a version ahead and a `TypeError` in a report. The tests that matter
 * are therefore the malformed ones: a missing key must read as absent, never crash the
 * page that is trying to explain what the platform holds.
 */

import { describe, expect, it } from "vitest";
import {
  criteriaFromResult,
  readTerminalResult,
  requiredCriteriaScore,
  verdictFromResult,
} from "@/lib/terminalResult";

const PAYLOAD = {
  status: "SUCCEEDED",
  bundle_url: "/api/v1/runs/abc/bundle",
  deliverables: [
    {
      artifact_id: "a1",
      name: "main.py",
      artifact_type: "code",
      path: "/w/main.py",
      sha256: "deadbeef",
      size_bytes: 2048,
      mime_type: "text/x-python",
    },
    {
      artifact_id: null,
      name: "REPORT.md",
      artifact_type: "report",
      path: "/w/REPORT.md",
      sha256: "cafebabe",
      size_bytes: 4096,
      mime_type: "text/markdown",
    },
  ],
  evaluation: {
    decision: "ACCEPT",
    passed: true,
    score: 0.91,
    criteria_results: [
      {
        criterion_id: "c1",
        metric: "accuracy",
        comparator: ">=",
        threshold: 0.9,
        observed: 0.9737,
        passed: true,
        required: true,
        weight: 1,
        note: "",
      },
      {
        criterion_id: "c2",
        metric: "roc_auc",
        comparator: ">=",
        threshold: 0.99,
        observed: null,
        passed: false,
        required: false,
        weight: 0.5,
        note: "never produced",
      },
    ],
    rubric: [{ dimension: "rigour", score: 4, justification: "sound" }],
    rubric_mean: 4,
    summary: "Met every required criterion.",
    replan_directive: null,
    refine_directive: null,
  },
  mlflow: {
    experiment_name: "pluton",
    run_id: "m1",
    parent_run_id: null,
    ui_url: "http://mlflow/runs/m1",
    logged_metrics: { accuracy: 0.9737, note: "not a number" },
  },
  usage: { tokens_in: 100, tokens_out: 200, llm_calls: 4 },
};

describe("readTerminalResult", () => {
  it("reads a complete payload", () => {
    const parsed = readTerminalResult(PAYLOAD)!;
    expect(parsed.status).toBe("SUCCEEDED");
    expect(parsed.deliverables).toHaveLength(2);
    expect(parsed.evaluation!.criteria_results).toHaveLength(2);
    expect(parsed.mlflow!.ui_url).toBe("http://mlflow/runs/m1");
    expect(parsed.usage!.tokens_in).toBe(100);
  });

  it("returns null for a run that has not finished", () => {
    expect(readTerminalResult(null)).toBeNull();
    expect(readTerminalResult(undefined)).toBeNull();
    expect(readTerminalResult("SUCCEEDED")).toBeNull();
    expect(readTerminalResult([])).toBeNull();
  });

  it("survives a payload with nothing but a status", () => {
    const parsed = readTerminalResult({ status: "FAILED" })!;
    expect(parsed.deliverables).toEqual([]);
    expect(parsed.evaluation).toBeNull();
    expect(parsed.mlflow).toBeNull();
    expect(parsed.usage).toBeNull();
  });

  it("drops a deliverable with no name rather than rendering a blank row", () => {
    const parsed = readTerminalResult({
      deliverables: [{ name: "" }, { sha256: "x" }, { name: "ok.py" }],
    })!;
    expect(parsed.deliverables.map((file) => file.name)).toEqual(["ok.py"]);
  });

  it("falls back to a known artifact type for an unrecognised one", () => {
    const parsed = readTerminalResult({
      deliverables: [{ name: "x", artifact_type: "hologram" }],
    })!;
    expect(parsed.deliverables[0]!.artifact_type).toBe("log");
  });

  it("keeps only numeric MLflow metrics", () => {
    const parsed = readTerminalResult(PAYLOAD)!;
    expect(parsed.mlflow!.logged_metrics).toEqual({ accuracy: 0.9737 });
  });

  it("reads the failure payload's error and last node", () => {
    const parsed = readTerminalResult({
      status: "FAILED",
      error: "sandbox timed out",
      last_node: "sandbox_exec",
    })!;
    expect(parsed.error).toBe("sandbox timed out");
    expect(parsed.last_node).toBe("sandbox_exec");
  });
});

describe("criteriaFromResult", () => {
  it("projects the evaluator's criteria onto the view model", () => {
    const rows = criteriaFromResult(readTerminalResult(PAYLOAD));
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      id: "c1",
      metric: "accuracy",
      observed: 0.9737,
      passed: true,
    });
  });

  it("marks every row as having seen a verdict", () => {
    // A row only exists in `result` because a verdict was reached and persisted, and
    // after a verdict an absent measurement is a failure rather than a pending one
    // (AGENTS.md §7.6). `verdictSeen: false` here would render it as still running.
    const rows = criteriaFromResult(readTerminalResult(PAYLOAD));
    expect(rows.every((row) => row.verdictSeen)).toBe(true);
    expect(rows[1]).toMatchObject({ observed: null, passed: false });
  });

  it("is empty for a run with no evaluation", () => {
    expect(criteriaFromResult(readTerminalResult({ status: "FAILED" }))).toEqual([]);
    expect(criteriaFromResult(null)).toEqual([]);
  });
});

describe("verdictFromResult", () => {
  it("prefers a replan directive and falls back to the refine one", () => {
    expect(verdictFromResult(readTerminalResult(PAYLOAD))!.directive).toBeNull();

    const refining = readTerminalResult({
      evaluation: { ...PAYLOAD.evaluation, refine_directive: "tune the threshold" },
    });
    expect(verdictFromResult(refining)!.directive).toBe("tune the threshold");
  });

  it("is null without an evaluation", () => {
    expect(verdictFromResult(readTerminalResult({}))).toBeNull();
  });
});

describe("requiredCriteriaScore", () => {
  it("counts only required criteria", () => {
    // c2 is optional and unmet; it must not drag the headline score down.
    expect(requiredCriteriaScore(PAYLOAD)).toBe("1/1");
  });

  it("is null when nothing required was scored", () => {
    expect(requiredCriteriaScore({ evaluation: { criteria_results: [] } })).toBeNull();
    expect(requiredCriteriaScore(null)).toBeNull();
    expect(requiredCriteriaScore({ evaluation: { criteria_results: "nope" } })).toBeNull();
  });
});
