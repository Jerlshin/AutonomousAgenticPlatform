/**
 * The per-run Zustand store, and the ring buffers that make it survive a long run.
 *
 * ARCHITECTURE.md §18.4: "Buffers are ring buffers, and that is load-bearing. A run
 * emitting 10 000 console lines must not grow an unbounded React array — the tab freezes
 * long before the run finishes."
 *
 * Zustand rather than Context for the reason §18.1 gives: a `token.delta` arrives several
 * times a second, and a Context value changing that often re-renders every consumer in
 * the tree. Here each pane subscribes to one slice and re-renders only when that slice
 * changes.
 *
 * **Events are folded on arrival, not on render.** The timeline, the criteria table and
 * the graph's active node are all derived state, and deriving them by scanning 2 000
 * events on every frame is the same performance bug as the unbounded array. Each event is
 * applied once, to the slice it belongs to.
 */

import { create } from "zustand";
import type { RunEvent } from "./events.generated";
import type {
  ArtifactRow,
  ConsoleLine,
  CriterionRow,
  MetricPoint,
  RunDetail,
  TimelineEntry,
} from "./types";

/** §18.4's caps. Events are cheap; console lines are the ones that arrive in thousands. */
export const EVENT_BUFFER_LIMIT = 2000;
export const CONSOLE_BUFFER_LIMIT = 5000;

export type StreamStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface RunStreamState {
  runId: string;
  run: RunDetail | null;
  status: StreamStatus;
  lastSeq: number;
  /** True once `replay.complete` has arrived, so a pane can tell history from live. */
  replayed: boolean;
  events: RunEvent[];
  consoleLines: ConsoleLine[];
  timeline: TimelineEntry[];
  criteria: CriterionRow[];
  artifacts: ArtifactRow[];
  metrics: MetricPoint[];
  activeNode: string | null;
  visitedNodes: string[];
  failedNodes: string[];
  budgets: Record<string, { used: number; limit: number; percent: number }>;
  pendingGate: { gate: string; prompt: string; options: string[] } | null;
  terminal: boolean;
  error: string | null;
}

interface RunStreamActions {
  reset: (runId: string) => void;
  setStatus: (status: StreamStatus) => void;
  setRun: (run: RunDetail) => void;
  ingest: (event: RunEvent) => void;
}

/** Append with a cap: keep the newest `limit` entries and drop the oldest. */
function ring<T>(buffer: T[], item: T, limit: number): T[] {
  if (buffer.length < limit) return [...buffer, item];
  return [...buffer.slice(buffer.length - limit + 1), item];
}

const EMPTY: Omit<RunStreamState, "runId"> = {
  run: null,
  status: "connecting",
  lastSeq: 0,
  replayed: false,
  events: [],
  consoleLines: [],
  timeline: [],
  criteria: [],
  artifacts: [],
  metrics: [],
  activeNode: null,
  visitedNodes: [],
  failedNodes: [],
  budgets: {},
  pendingGate: null,
  terminal: false,
  error: null,
};

let consoleLineId = 0;

export const useRunStore = create<RunStreamState & RunStreamActions>((set) => ({
  runId: "",
  ...EMPTY,

  reset: (runId) => set({ runId, ...EMPTY }),
  setStatus: (status) => set({ status }),
  setRun: (run) => set({ run }),

  ingest: (event) =>
    set((state) => {
      const next: Partial<RunStreamState> = {
        events: ring(state.events, event, EVENT_BUFFER_LIMIT),
      };
      // Control frames carry seq 0 and must not move the resume cursor (§9.2).
      if (event.seq > 0) next.lastSeq = Math.max(state.lastSeq, event.seq);
      return { ...next, ...applyEvent(state, event) };
    }),
}));

/**
 * Fold one event into the derived slices.
 *
 * Returns only the slices that changed, so Zustand's shallow comparison keeps a
 * `token.delta` from re-rendering the artifact pane.
 */
function applyEvent(
  state: RunStreamState,
  event: RunEvent,
): Partial<RunStreamState> {
  const p = event.payload as Record<string, never> & Record<string, unknown>;

  switch (event.type) {
    case "replay.complete":
      return { replayed: true };

    case "run.snapshot":
      return { run: p as unknown as RunDetail };

    case "run.phase":
      return state.run
        ? { run: { ...state.run, phase: p.phase as RunDetail["phase"] } }
        : {};

    case "run.completed":
    case "run.failed":
    case "run.cancelled":
      return {
        terminal: true,
        activeNode: null,
        error: typeof p.error === "string" ? p.error : state.error,
        artifacts: mergeDeliverables(state.artifacts, p.deliverables),
      };

    case "node.started": {
      const node = String(p.node ?? "unknown");
      return {
        activeNode: node,
        visitedNodes: state.visitedNodes.includes(node)
          ? state.visitedNodes
          : [...state.visitedNodes, node],
        timeline: [
          ...state.timeline,
          {
            node,
            seq: event.seq,
            status: "running",
            startedAt: event.ts,
          },
        ],
      };
    }

    case "node.completed":
      return {
        activeNode: null,
        timeline: closeEntry(state.timeline, String(p.node ?? ""), {
          status: p.degraded ? "degraded" : "succeeded",
          durationMs: numberOr(p.duration_ms),
          tokensOut: numberOr(p.tokens_out),
          summary: typeof p.summary === "string" ? p.summary : undefined,
        }),
      };

    case "node.failed": {
      const node = String(p.node ?? "");
      const detail = p.error as { message?: string } | undefined;
      return {
        failedNodes: state.failedNodes.includes(node)
          ? state.failedNodes
          : [...state.failedNodes, node],
        timeline: closeEntry(state.timeline, node, {
          status: "failed",
          error: detail?.message,
        }),
      };
    }

    case "token.delta":
      return {
        consoleLines: ring(
          state.consoleLines,
          {
            id: consoleLineId++,
            stream: "token",
            text: String(p.text ?? ""),
            ts: event.ts,
            node: typeof p.node === "string" ? p.node : undefined,
          },
          CONSOLE_BUFFER_LIMIT,
        ),
      };

    case "sandbox.stdout":
    case "sandbox.stderr":
      return {
        consoleLines: ring(
          state.consoleLines,
          {
            id: consoleLineId++,
            stream: event.type === "sandbox.stderr" ? "stderr" : "stdout",
            text: String(p.line ?? ""),
            ts: typeof p.ts === "string" ? p.ts : event.ts,
            executionId: typeof p.execution_id === "string" ? p.execution_id : undefined,
          },
          CONSOLE_BUFFER_LIMIT,
        ),
      };

    case "sandbox.truncated":
      return {
        consoleLines: ring(
          state.consoleLines,
          {
            id: consoleLineId++,
            stream: "stderr",
            text: `— ${p.bytes_dropped} bytes of ${p.stream} were dropped: the output cap was reached —`,
            ts: event.ts,
          },
          CONSOLE_BUFFER_LIMIT,
        ),
      };

    case "plan.created":
    case "plan.revised":
      return { criteria: criteriaFrom(p.success_criteria) };

    case "evaluation.completed":
      return { criteria: mergeResults(state.criteria, p.criteria_results) };

    case "artifact.created":
      return {
        artifacts: [...state.artifacts, p as unknown as ArtifactRow],
      };

    case "metric.logged":
      return {
        metrics: [
          ...state.metrics,
          {
            key: String(p.key ?? ""),
            value: Number(p.value ?? 0),
            step: Number(p.step ?? 0),
          },
        ],
      };

    case "budget.warning":
      return {
        budgets: {
          ...state.budgets,
          [String(p.resource)]: {
            used: Number(p.used ?? 0),
            limit: Number(p.limit ?? 0),
            percent: Number(p.percent ?? 0),
          },
        },
      };

    case "interrupt.requested":
      return {
        pendingGate: {
          gate: String(p.gate ?? ""),
          prompt: String(p.prompt ?? ""),
          options: Array.isArray(p.options) ? (p.options as string[]) : [],
        },
      };

    case "error":
      return { error: String(p.message ?? "protocol error") };

    default:
      return {};
  }
}

function numberOr(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

/**
 * Close the most recent open entry for `node`.
 *
 * Searched from the end because loop 1 visits `coder` several times in one run, and the
 * completion belongs to the visit that is still open — not to the first one that matched.
 */
function closeEntry(
  timeline: TimelineEntry[],
  node: string,
  patch: Partial<TimelineEntry>,
): TimelineEntry[] {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const entry = timeline[i];
    if (entry && entry.node === node && entry.status === "running") {
      const copy = [...timeline];
      copy[i] = { ...entry, ...patch };
      return copy;
    }
  }
  return timeline;
}

function criteriaFrom(raw: unknown): CriterionRow[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item, index) => {
    const c = item as Record<string, unknown>;
    return {
      id: String(c.id ?? `c${index + 1}`),
      metric: String(c.metric ?? ""),
      comparator: String(c.comparator ?? "gte"),
      threshold: Number(c.threshold ?? 0),
      required: c.required !== false,
    };
  });
}

function mergeResults(criteria: CriterionRow[], raw: unknown): CriterionRow[] {
  if (!Array.isArray(raw)) return criteria;
  const byId = new Map(criteria.map((c) => [c.id, c]));
  for (const item of raw) {
    const r = item as Record<string, unknown>;
    const id = String(r.criterion_id ?? r.id ?? "");
    const existing = byId.get(id);
    byId.set(id, {
      id,
      metric: String(r.metric ?? existing?.metric ?? ""),
      comparator: String(r.comparator ?? existing?.comparator ?? "gte"),
      threshold: Number(r.threshold ?? existing?.threshold ?? 0),
      required: r.required !== false,
      observed: r.observed === null ? null : Number(r.observed),
      passed: Boolean(r.passed),
    });
  }
  return [...byId.values()];
}

function mergeDeliverables(current: ArtifactRow[], raw: unknown): ArtifactRow[] {
  if (!Array.isArray(raw)) return current;
  const seen = new Set(current.map((a) => a.name));
  const extra = (raw as Record<string, unknown>[])
    .filter((d) => !seen.has(String(d.name)))
    .map((d) => ({
      name: String(d.name ?? ""),
      type: String(d.artifact_type ?? d.type ?? "log"),
      size_bytes: typeof d.size_bytes === "number" ? d.size_bytes : undefined,
      sha256: typeof d.sha256 === "string" ? d.sha256 : undefined,
    }));
  return extra.length ? [...current, ...extra] : current;
}
