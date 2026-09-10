/**
 * Store fold correctness (§13).
 *
 * Three properties carry the pane layer, and each has been a real bug in systems shaped
 * like this one:
 *
 * * **the backward match** — loop 1 visits `coder` several times per run, and a
 *   completion that closes the *first* open visit leaves the real one running forever;
 * * **replay idempotency** — a reconnect re-delivers history, and a blind `push` turns
 *   every reconnect into a duplicated timeline;
 * * **absence is not success** — a criterion with no observation, after a verdict, is a
 *   failure, and rendering it as pending is how a run that missed its target looks like
 *   one that is still working.
 */

import { beforeEach, describe, expect, it } from "vitest";
import type { RunEvent } from "@/lib/events.generated";
import {
  CONSOLE_LIMIT,
  createRunStore,
  EVENT_LIMIT,
  ringPush,
  TIMELINE_LIMIT,
  type RunStoreApi,
} from "@/stores/runStore";

const RUN = "11111111-2222-3333-4444-555555555555";

function event<T extends RunEvent["type"]>(
  type: T,
  seq: number,
  payload: Extract<RunEvent, { type: T }>["payload"],
): RunEvent {
  return {
    v: 1,
    seq,
    run_id: RUN,
    ts: new Date(1_700_000_000_000 + seq * 1000).toISOString(),
    type,
    payload,
  } as RunEvent;
}

function nodeStarted(seq: number, node: string, phase = "IMPLEMENT"): RunEvent {
  return event("node.started", seq, {
    node,
    agent: node,
    phase,
    model: null,
    plan_step_id: null,
    step_seq: null,
  });
}

function nodeCompleted(seq: number, node: string, summary = "", degraded = false): RunEvent {
  return event("node.completed", seq, {
    node,
    duration_ms: 1234,
    tokens_in: 100,
    tokens_out: 200,
    llm_calls: 1,
    degraded,
    summary,
    step_seq: null,
  });
}

// One store per test, which is what `createRunStore` is for (§6.3). Sharing a module
// singleton between cases is how a fold test starts passing because of what the previous
// one left behind.
let api: RunStoreApi;
const store = () => api.getState();

beforeEach(() => {
  api = createRunStore();
  api.getState().reset(RUN);
});

// ------------------------------------------------------------------------------------

describe("ring buffers", () => {
  it("keeps the newest entries and reports how many it dropped", () => {
    expect(ringPush([1, 2, 3], [4], 3)).toEqual({ next: [2, 3, 4], dropped: 1 });
    expect(ringPush([1, 2], [3], 5)).toEqual({ next: [1, 2, 3], dropped: 0 });
    expect(ringPush([], [], 5)).toEqual({ next: [], dropped: 0 });
  });

  it("handles a burst larger than the cap without allocating past it", () => {
    const { next, dropped } = ringPush([1, 2], [3, 4, 5, 6, 7], 3);
    expect(next).toEqual([5, 6, 7]);
    expect(dropped).toBe(4);
  });

  it("caps the console and makes the eviction visible", () => {
    store().reset(RUN, { console: 4 });
    store().ingest(
      Array.from({ length: 10 }, (_, index) =>
        event("sandbox.stdout", index + 1, {
          execution_id: "e1",
          line: `line ${index + 1}`,
          ts: "",
        }),
      ),
    );

    expect(store().consoleLines).toHaveLength(4);
    expect(store().consoleLines.at(-1)?.text).toBe("line 10");
    expect(store().droppedConsoleLines).toBe(6);
    // A log viewer that quietly loses lines is worse than one that says it did.
    expect(store().historyComplete).toBe(false);
  });

  it("caps the raw event ring independently of the console", () => {
    store().reset(RUN, { events: 3 });
    store().ingest(
      Array.from({ length: 5 }, (_, index) => nodeStarted(index + 1, `n${index}`)),
    );
    expect(store().events).toHaveLength(3);
    expect(store().droppedEvents).toBe(2);
  });

  it("exports the §3.7 caps so tests do not hard-code them twice", () => {
    expect(EVENT_LIMIT).toBe(2000);
    expect(CONSOLE_LIMIT).toBe(5000);
    expect(TIMELINE_LIMIT).toBe(500);
  });

  it("never evicts a running timeline entry", () => {
    const events: RunEvent[] = [];
    for (let visit = 0; visit < TIMELINE_LIMIT + 20; visit++) {
      const seq = visit * 2 + 1;
      events.push(nodeStarted(seq, `n${visit}`));
      events.push(nodeCompleted(seq + 1, `n${visit}`));
    }
    events.push(nodeStarted(100_000, "coder"));
    store().ingest(events);

    expect(store().timeline.length).toBeLessThanOrEqual(TIMELINE_LIMIT);
    // The row that says what is executing right now is the one the pane cannot do without.
    expect(store().timeline.some((entry) => entry.status === "running")).toBe(true);
  });
});

describe("node.completed matches the visit that is still open", () => {
  it("closes the most recent open entry, searching backwards", () => {
    store().ingest([
      nodeStarted(1, "coder"),
      nodeCompleted(2, "coder", "rev 1"),
      nodeStarted(3, "sandbox_exec"),
      nodeStarted(4, "coder"),
      nodeStarted(5, "coder"),
    ]);
    store().ingest([nodeCompleted(6, "coder", "rev 3")]);

    const coder = store().timeline.filter((entry) => entry.node === "coder");
    expect(coder).toHaveLength(3);
    expect(coder[0]?.summary).toBe("rev 1");
    expect(coder[1]?.status).toBe("running");
    // Matching forwards would have closed the first visit a second time.
    expect(coder[2]?.summary).toBe("rev 3");
    expect(coder[2]?.status).toBe("succeeded");
  });

  it("marks a degraded completion distinctly from a successful one", () => {
    store().ingest([
      nodeStarted(1, "researcher"),
      nodeCompleted(2, "researcher", "fell back", true),
    ]);
    expect(store().timeline[0]?.status).toBe("degraded");
    expect(store().nodeState.researcher?.status).toBe("degraded");
  });

  it("annotates a retry rather than adding a row", () => {
    store().ingest([
      nodeStarted(1, "coder"),
      event("node.retrying", 2, {
        node: "coder",
        attempt: 2,
        max_attempts: 3,
        backoff_ms: null,
      }),
    ]);
    expect(store().timeline).toHaveLength(1);
    expect(store().timeline[0]?.attempt).toBe(2);
    expect(store().timeline[0]?.maxAttempts).toBe(3);
  });
});

describe("idempotency under replay", () => {
  const history: RunEvent[] = [
    nodeStarted(1, "planner"),
    nodeCompleted(2, "planner", "4-step plan"),
    nodeStarted(3, "coder"),
    event("code.revision", 4, {
      revision: 1,
      path: "main.py",
      language: "python",
      sha256: "abc",
      lines_changed: 142,
      diff: "",
      rationale: "first draft",
      addresses_error: null,
    }),
    nodeCompleted(5, "coder", "142 lines"),
    event("sandbox.stdout", 6, { execution_id: "e1", line: "Fitting 5 folds", ts: "" }),
    event("metric.logged", 7, {
      key: "accuracy",
      value: 0.9737,
      step: 0,
      mlflow_run_id: "c8f1",
    }),
  ];

  it("folding the same sequence twice leaves state identical", () => {
    store().ingest(history);
    const first = JSON.stringify(snapshot());

    store().ingest(history);
    expect(JSON.stringify(snapshot())).toBe(first);
  });

  it("a fresh store fed the same sequence reaches the same state", () => {
    store().ingest(history);
    const first = JSON.stringify(snapshot());

    store().reset(RUN);
    store().ingest(history);
    expect(JSON.stringify(snapshot())).toBe(first);
  });

  it("ignores a replayed event at or below the highest seq folded", () => {
    store().ingest(history);
    const revisions = store().codeRevisions.length;
    store().ingest([history[3]!]);
    expect(store().codeRevisions).toHaveLength(revisions);
  });

  function snapshot() {
    const state = store();
    return {
      timeline: state.timeline,
      criteria: state.criteria,
      artifacts: state.artifacts,
      codeRevisions: state.codeRevisions,
      metrics: state.metrics,
      consoleLines: state.consoleLines.map((line) => line.text),
      nodeState: state.nodeState,
      usage: state.usage,
    };
  }
});

describe("criteria: the plan's contract merged with the evaluator's observation", () => {
  const plan = event("plan.created", 1, {
    steps: [],
    success_criteria: [
      {
        id: "c1",
        metric: "accuracy",
        comparator: "gte",
        threshold: 0.95,
        tolerance: 0,
        required: true,
        weight: 1,
        rationale: "headline",
      },
      {
        id: "c2",
        metric: "auc_pr",
        comparator: "gte",
        threshold: 0.9,
        tolerance: 0,
        required: true,
        weight: 1,
        rationale: "",
      },
    ],
    task_kind: "tabular-classification",
    primary_metric: "accuracy",
    assumptions: [],
    revision: 1,
  });

  it("is pending before a verdict, because nothing has been measured yet", () => {
    store().ingest([plan]);
    expect(store().criteria).toHaveLength(2);
    expect(store().criteria.every((row) => row.verdictSeen)).toBe(false);
    expect(store().criteria.every((row) => row.observed === null)).toBe(true);
  });

  it("marks a criterion the verdict never observed, so absence is not success", () => {
    store().ingest([
      plan,
      event("evaluation.completed", 2, {
        decision: "REFINE",
        passed: false,
        score: 0.5,
        criteria_results: [
          {
            criterion_id: "c1",
            metric: "accuracy",
            comparator: "gte",
            threshold: 0.95,
            observed: 0.9737,
            passed: true,
            required: true,
            weight: 1,
            note: "",
          },
        ],
        rubric: [],
        rubric_mean: null,
        replan_directive: null,
        refine_directive: "raise auc_pr",
        summary: "",
      }),
    ]);

    const [accuracy, aucPr] = store().criteria;
    expect(accuracy?.passed).toBe(true);
    expect(accuracy?.observed).toBeCloseTo(0.9737);
    // `auc_pr` was never produced. Once a verdict exists it is failed, not pending —
    // AGENTS.md §7.6.
    expect(aucPr?.verdictSeen).toBe(true);
    expect(aucPr?.observed).toBeNull();
    expect(store().verdict?.decision).toBe("REFINE");
  });

  it("resets the ledger on a plan revision", () => {
    store().ingest([
      plan,
      event("evaluation.completed", 2, {
        decision: "REPLAN",
        passed: false,
        score: 0.2,
        criteria_results: [],
        rubric: [],
        rubric_mean: null,
        replan_directive: "try gradient boosting",
        refine_directive: null,
        summary: "",
      }),
      event("plan.revised", 3, {
        steps: [],
        success_criteria: [
          {
            id: "c9",
            metric: "f1_macro",
            comparator: "gte",
            threshold: 0.94,
            tolerance: 0,
            required: true,
            weight: 1,
            rationale: "",
          },
        ],
        task_kind: "tabular-classification",
        primary_metric: "f1_macro",
        assumptions: [],
        revision: 2,
        diff: "",
        reason: "the approach was wrong",
      }),
    ]);

    expect(store().planRevision).toBe(2);
    expect(store().criteria.map((row) => row.id)).toEqual(["c9"]);
    // Comparing against a silently changed contract is how a REPLAN looks like a
    // regression, so the verdict that judged the old criteria goes with them.
    expect(store().verdict).toBeNull();
  });
});

describe("token deltas are one growing row per node", () => {
  it("appends to the row it continues rather than pushing a new one", () => {
    store().ingest([
      event("token.delta", 1, { node: "planner", text: '{"steps":' }),
      event("token.delta", 2, { node: "planner", text: '[{"id":"s1"' }),
    ]);
    expect(store().consoleLines).toHaveLength(1);
    expect(store().consoleLines[0]?.text).toBe('{"steps":[{"id":"s1"');
  });

  it("closes the row at a newline and opens the next", () => {
    store().ingest([
      event("token.delta", 1, { node: "planner", text: "first" }),
      event("token.delta", 2, { node: "planner", text: " line\nsecond line" }),
    ]);
    expect(store().consoleLines.map((line) => line.text)).toEqual([
      "first line",
      "second line",
    ]);
  });

  it("starts a new row when the node changes", () => {
    store().ingest([
      event("token.delta", 1, { node: "planner", text: "plan" }),
      event("token.delta", 2, { node: "coder", text: "code" }),
    ]);
    expect(store().consoleLines).toHaveLength(2);
  });
});

describe("terminal events", () => {
  it("clears the active node, closes open entries and folds the deliverables in", () => {
    store().ingest([
      nodeStarted(1, "coder"),
      event("run.failed", 2, {
        status: "FAILED",
        error: "the sandbox never produced metrics.json",
        last_node: "sandbox_exec",
        dossier_url: null,
        resumable: false,
        deliverables: [
          {
            artifact_id: null,
            name: "main.py",
            artifact_type: "code",
            path: "main.py",
            sha256: "deadbeef",
            size_bytes: 2900,
            mime_type: "text/x-python",
          },
        ],
        bundle_url: "/api/v1/runs/x/bundle",
        evaluation: null,
        mlflow: null,
        usage: null,
      }),
    ]);

    expect(store().terminal).toBe(true);
    // A graph left with a node pulsing after the run ended is a lie about the system.
    expect(store().activeNode).toBeNull();
    expect(store().timeline[0]?.status).toBe("failed");
    expect(store().outcome).toBe("FAILED");
    expect(store().bundleUrl).toBe("/api/v1/runs/x/bundle");
    // Rows the live stream never produced are labelled, because §2.4 requires a lossy
    // fallback to say so.
    expect(store().artifacts[0]).toMatchObject({ name: "main.py", reconstructed: true });
  });

  it("prefers the live tile over the reconstructed one for the same name", () => {
    store().ingest([
      event("artifact.created", 1, {
        artifact_id: "a1",
        name: "main.py",
        type: "code",
        size_bytes: 2900,
        sha256: "deadbeef",
        download_url: "/api/v1/artifacts/a1/download",
      }),
      event("run.completed", 2, {
        status: "SUCCEEDED",
        deliverables: [
          {
            artifact_id: null,
            name: "main.py",
            artifact_type: "code",
            path: "main.py",
            sha256: "deadbeef",
            size_bytes: 2900,
            mime_type: "text/x-python",
          },
        ],
        bundle_url: null,
        evaluation: null,
        mlflow: null,
        usage: null,
      }),
    ]);

    expect(store().artifacts).toHaveLength(1);
    expect(store().artifacts[0]?.downloadUrl).toBe("/api/v1/artifacts/a1/download");
    expect(store().artifacts[0]?.reconstructed).toBe(false);
  });
});

describe("graph state", () => {
  it("counts visits and records the edge that was traversed", () => {
    store().ingest([
      nodeStarted(1, "coder"),
      nodeCompleted(2, "coder"),
      nodeStarted(3, "sandbox_exec"),
      nodeCompleted(4, "sandbox_exec"),
      nodeStarted(5, "debugger"),
      nodeCompleted(6, "debugger"),
      nodeStarted(7, "coder"),
    ]);

    expect(store().nodeState.coder?.visits).toBe(2);
    expect(store().edgeTraversals["coder→sandbox_exec"]).toBe(1);
    expect(store().edgeTraversals["debugger→coder"]).toBe(1);
    expect(store().lastEdge).toBe("debugger→coder");
    expect(store().activeNode).toBe("coder");
  });

  it("accumulates usage across visits", () => {
    store().ingest([
      nodeStarted(1, "coder"),
      nodeCompleted(2, "coder"),
      nodeStarted(3, "coder"),
      nodeCompleted(4, "coder"),
    ]);
    expect(store().usage).toEqual({
      tokensIn: 200,
      tokensOut: 400,
      llmCalls: 2,
      nodeVisits: 2,
    });
  });

  it("tracks the phase and its history from node entry", () => {
    store().ingest([
      nodeStarted(1, "planner", "PLANNING"),
      nodeCompleted(2, "planner"),
      nodeStarted(3, "coder", "IMPLEMENT"),
    ]);
    expect(store().phase).toBe("IMPLEMENT");
    expect(store().phaseHistory.map((entry) => entry.phase)).toEqual([
      "PLANNING",
      "IMPLEMENT",
    ]);
  });
});

describe("gates", () => {
  const gate = event("interrupt.requested", 5, {
    gate: "before_sandbox_exec",
    prompt: "review this code before it runs",
    options: ["approve", "reject"],
    expires_at: "2026-01-01T00:30:00Z",
    context: {},
  });

  it("holds the gate until the graph moves on", () => {
    store().ingest([gate]);
    expect(store().pendingGate?.gate).toBe("before_sandbox_exec");

    // The protocol has no `interrupt.resolved`; the run simply leaves AWAITING_INPUT.
    store().ingest([nodeStarted(6, "sandbox_exec")]);
    expect(store().pendingGate).toBeNull();
  });

  it("does not clear on an event that predates it", () => {
    store().ingest([gate]);
    store().ingest([nodeStarted(4, "coder")]);
    expect(store().pendingGate).not.toBeNull();
  });
});

describe("run.snapshot", () => {
  it("replaces the run body wholesale rather than merging", () => {
    store().setRun({
      run_id: RUN,
      task_id: RUN,
      title: "old title",
      prompt: "p",
      status: "RUNNING",
      status_detail: "RUNNING",
      phase: "PLANNING",
      current_node: "planner",
      percent: 10,
      outcome: null,
      last_seq: 3,
      worker_id: "worker-1",
      tokens_in: 1,
      tokens_out: 2,
      node_visits: 1,
      debug_iterations: 0,
      replan_count: 0,
      error: null,
      result: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      ws_url: "",
    });

    store().ingest([
      event("run.snapshot", 0, {
        run_id: RUN,
        task_id: RUN,
        title: "new title",
        prompt: "p",
        status: "RUNNING",
        status_detail: "RUNNING",
        phase: "EXECUTE",
        current_node: "sandbox_exec",
        percent: 62,
        outcome: null,
        last_seq: 44,
        worker_id: "worker-2",
        tokens_in: 41_000,
        tokens_out: 9_000,
        node_visits: 11,
        debug_iterations: 1,
        replan_count: 0,
        error: null,
        result: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:05:00Z",
        ws_url: "",
      }),
    ]);

    // A merge would preserve stale fields that the gap invalidated.
    expect(store().run?.title).toBe("new title");
    expect(store().run?.worker_id).toBe("worker-2");
    expect(store().phase).toBe("EXECUTE");
    expect(store().activeNode).toBe("sandbox_exec");
    expect(store().lastSeq).toBe(44);
  });
});

describe("sandbox output", () => {
  it("renders truncation as a marker the operator can tell from program output", () => {
    store().ingest([
      event("sandbox.truncated", 1, {
        execution_id: "e1",
        stream: "stdout",
        bytes_dropped: 1_258_291,
      }),
    ]);
    const line = store().consoleLines[0];
    expect(line?.stream).toBe("marker");
    expect(line?.tone).toBe("warn");
    expect(line?.text).toContain("1.2 MB");
    expect(store().truncations[0]?.bytesDropped).toBe(1_258_291);
  });

  it("closes the execution row on exit", () => {
    store().ingest([
      event("sandbox.started", 1, {
        execution_id: "e1",
        profile: "train",
        revision: 2,
        image: "pluton/sandbox:train",
        limits: { cpus: 4, memory: "6g", timeout_s: 900, network: "none" },
      }),
      event("sandbox.exit", 2, {
        execution_id: "e1",
        exit_code: 0,
        timed_out: false,
        oom_killed: false,
        duration_ms: 18_442,
        max_rss_bytes: 412_000_000,
      }),
    ]);
    expect(store().executions).toHaveLength(1);
    expect(store().executions[0]).toMatchObject({ exitCode: 0, durationMs: 18_442 });
  });
});
