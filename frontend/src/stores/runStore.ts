/**
 * The per-run Zustand store: the folded projection of one run's event stream.
 *
 * docs/FRONTEND.md §6.3. Every field here is written by exactly one place — `applyEvent`
 * — and read by panes through selectors (§6.5). Zustand rather than Context for the
 * reason §6.1 gives: a `token.delta` arrives several times a second, and a Context value
 * changing at that rate re-renders every consumer beneath the provider.
 *
 * Three invariants carry most of the weight:
 *
 * **Events are folded on arrival, never on render.** Deriving a timeline by scanning
 * 2 000 events every frame is the same bug as an unbounded array, arriving later. When a
 * pane needs a slice this store does not expose, the fix is to extend the fold — never to
 * re-derive it by scanning `events` inside a component.
 *
 * **Buffers are ring buffers, and that is load-bearing.** A `train` run emits tens of
 * thousands of console lines. The caps in §3.7 are module constants with their reasoning
 * attached and are exported so tests assert against them rather than hard-coding a number
 * in two places. Every eviction is counted, because a log viewer that quietly loses lines
 * is worse than one that says it did.
 *
 * **Ingest is a batch.** The hook queues frames and flushes once per animation frame
 * (§6.4), so one array copy serves a whole burst and store commits stay bounded at ~60/s
 * regardless of stream rate.
 */

import { createStore, type StoreApi } from "zustand/vanilla";
import type {
  ArtifactCreatedPayload,
  CriterionResultRef,
  DeliverableRef,
  EvaluationRef,
  MlflowRef,
  PlanStepRef,
  RunEvent,
  SuccessCriterionRef,
  UsageRef,
} from "@/lib/events.generated";
import type { RunRead } from "@/lib/rest";
import type {
  ArtifactRow,
  CodeRevisionRow,
  ConsoleLine,
  CriterionRow,
  DiagnosisRow,
  GateDecisionRow,
  GateName,
  GraphNodeState,
  MetricPoint,
  PendingGate,
  PlanStepRow,
  RetrievalHitRow,
  RunOutcome,
  RunPhase,
  SandboxExecutionRow,
  StreamError,
  TimelineEntry,
  VerdictRow,
} from "@/lib/types";

// ------------------------------------------------------------------------------------
//  Caps (§3.7)
// ------------------------------------------------------------------------------------

/** Raw envelopes, kept only for the debug drawer. Panes read folded slices, not these. */
export const EVENT_LIMIT = 2000;

/** ~500 KB at typical line length. A `train` run emits tens of thousands. */
export const CONSOLE_LIMIT = 5000;

/**
 * A 60-node-visit budget makes 500 unreachable in practice; the cap guards against a
 * pathological loop. A `running` entry is never evicted — losing the row that says what
 * is happening right now to make room for history is exactly backwards.
 */
export const TIMELINE_LIMIT = 500;

/** `max_sandbox_executions`. Full source per revision is the largest payload held. */
export const CODE_REVISION_LIMIT = 12;

/** Debug detail, not primary product. */
export const RETRIEVAL_LIMIT = 200;

/** `metric_series` caps at 2 000; this leaves headroom for two series. */
export const METRIC_LIMIT = 4000;

/** Above this many revisions, the oldest keeps its metadata and loses its source. */
export const CODE_CONTENT_LIMIT = 4;

// ------------------------------------------------------------------------------------
//  State
// ------------------------------------------------------------------------------------

export type StreamStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

/** The body of whichever terminal event ended the run, kept verbatim (§8.5.6, §8.8). */
export type TerminalEvent = Extract<
  RunEvent,
  { type: "run.completed" | "run.failed" | "run.cancelled" }
>;
export type TerminalPayload = TerminalEvent["payload"];

/**
 * Ring-buffer caps for this store instance.
 *
 * The §3.7 numbers are the defaults; `reset` accepts overrides so a test can drive
 * eviction with three lines instead of five thousand and still assert against the same
 * code path a real run takes.
 */
export interface BufferLimits {
  events: number;
  console: number;
}

export interface RunStreamState {
  // ── identity & connection ───────────────────────────────────────────────
  runId: string;
  limits: BufferLimits;
  status: StreamStatus;
  lastSeq: number;
  /** `replay.complete` seen — history is drained and this is the live tail. */
  replayed: boolean;
  /** False after a `replay.gap` or a ring eviction (§3.4). Panes MUST say so. */
  historyComplete: boolean;
  oldestAvailable: number | null;
  droppedEvents: number;
  droppedConsoleLines: number;
  /** What was actually sent to `subscribe`, control floor included (§3.5). */
  effectiveFilter: string[] | null;
  error: StreamError | null;

  // ── run identity (seeded from REST, refreshed by run.snapshot) ──────────
  run: RunRead | null;
  phase: RunPhase | null;
  phaseHistory: { phase: RunPhase; ts: string }[];
  outcome: RunOutcome | null;
  terminal: boolean;
  terminalPayload: TerminalPayload | null;
  workerId: string | null;
  /** role → model, from `run.started`. The graph's tooltips read it. */
  modelRouting: Record<string, string>;
  queuePosition: number | null;

  // ── graph (§8.5.3) ──────────────────────────────────────────────────────
  activeNode: string | null;
  nodeState: Record<string, GraphNodeState>;
  /** `"coder→sandbox_exec"` → traversal count. */
  edgeTraversals: Record<string, number>;
  lastEdge: string | null;

  // ── timeline (§8.5.4) ───────────────────────────────────────────────────
  timeline: TimelineEntry[];
  planSteps: PlanStepRow[];
  planRevision: number;
  criteria: CriterionRow[];
  verdict: VerdictRow | null;
  usage: {
    tokensIn: number;
    tokensOut: number;
    llmCalls: number;
    nodeVisits: number;
  };
  budgets: Record<string, { used: number; limit: number; percent: number }>;

  // ── console (§8.5.5) ────────────────────────────────────────────────────
  consoleLines: ConsoleLine[];
  executions: SandboxExecutionRow[];
  truncations: { executionId: string; stream: string; bytesDropped: number }[];

  // ── artifacts & metrics (§8.5.6) ────────────────────────────────────────
  artifacts: ArtifactRow[];
  metrics: MetricPoint[];
  mlflow: MlflowRef | null;
  bundleUrl: string | null;

  // ── code & retrieval (§8.7) ─────────────────────────────────────────────
  codeRevisions: CodeRevisionRow[];
  retrievalHits: RetrievalHitRow[];
  diagnoses: DiagnosisRow[];

  // ── operator attention (§8.6) ───────────────────────────────────────────
  pendingGate: PendingGate | null;
  gateHistory: GateDecisionRow[];

  // ── raw, for the debug drawer only ──────────────────────────────────────
  /** Panes MUST NOT scan this. It exists so a disagreement can be adjudicated (§8.5.2). */
  events: RunEvent[];
}

export interface RunStreamActions {
  reset(runId: string, limits?: Partial<BufferLimits>): void;
  setStatus(status: StreamStatus): void;
  setError(error: StreamError | null): void;
  setRun(run: RunRead): void;
  setEffectiveFilter(types: string[] | null): void;
  /** A BATCH. The hook's rAF flusher is the only caller (§6.4, invariant I9). */
  ingest(events: RunEvent[]): void;
  /**
   * Record a decision this client just submitted.
   *
   * The one action outside the fold, because it is the one piece of run state that has no
   * event: the protocol has no `interrupt.resolved`, so a gate the operator answered
   * would otherwise vanish from the timeline the moment the dialog closed. It records
   * *delivery*, never effect — the run leaving `AWAITING_INPUT` is what clears
   * `pendingGate`, and that still arrives over the socket.
   */
  recordGateDecision(row: GateDecisionRow): void;
}

export type RunStore = RunStreamState & RunStreamActions;

const EMPTY: Omit<RunStreamState, "runId" | "limits"> = {
  status: "idle",
  lastSeq: 0,
  replayed: false,
  historyComplete: true,
  oldestAvailable: null,
  droppedEvents: 0,
  droppedConsoleLines: 0,
  effectiveFilter: null,
  error: null,

  run: null,
  phase: null,
  phaseHistory: [],
  outcome: null,
  terminal: false,
  terminalPayload: null,
  workerId: null,
  modelRouting: {},
  queuePosition: null,

  activeNode: null,
  nodeState: {},
  edgeTraversals: {},
  lastEdge: null,

  timeline: [],
  planSteps: [],
  planRevision: 0,
  criteria: [],
  verdict: null,
  usage: { tokensIn: 0, tokensOut: 0, llmCalls: 0, nodeVisits: 0 },
  budgets: {},

  consoleLines: [],
  executions: [],
  truncations: [],

  artifacts: [],
  metrics: [],
  mlflow: null,
  bundleUrl: null,

  codeRevisions: [],
  retrievalHits: [],
  diagnoses: [],

  pendingGate: null,
  gateHistory: [],

  events: [],
};

/**
 * Console line ids are monotonic for the life of the tab.
 *
 * Not derived from `seq`, because one `sandbox.stdout` event is one line but a
 * `token.delta` is *appended to* the line before it (§8.5.5) — a 4 000-token plan must
 * produce one growing row, not 4 000 rows. Shared across store instances on purpose: the
 * dashboard mounts several streams at once, and a per-store counter would hand two panes
 * the same React key for different lines.
 */
let consoleLineId = 0;

export type RunStoreApi = StoreApi<RunStore>;

/**
 * One store per mounted run stream (§6.3).
 *
 * A factory rather than a module singleton because the dashboard tails up to four active
 * runs at once (§8.2), and a single global store would have four folds writing one
 * `activeNode`. `useRunStream` creates one and `RunStoreProvider` puts it in scope for
 * the panes; nothing else calls this.
 */
export function createRunStore(): RunStoreApi {
  return createStore<RunStore>()((set) => ({
    runId: "",
    limits: { events: EVENT_LIMIT, console: CONSOLE_LIMIT },
    ...EMPTY,

    reset: (runId, limits) =>
      set({
        runId,
        limits: {
          events: limits?.events ?? EVENT_LIMIT,
          console: limits?.console ?? CONSOLE_LIMIT,
        },
        ...EMPTY,
      }),
    setStatus: (status) => set({ status }),
    setError: (error) => set({ error }),
    setRun: (run) => set({ run }),
    setEffectiveFilter: (effectiveFilter) => set({ effectiveFilter }),
    recordGateDecision: (row) =>
      set((state) => ({ gateHistory: [...state.gateHistory, row] })),

    ingest: (events) =>
      set((state) => (events.length === 0 ? {} : reduceBatch(state, events))),
  }));
}

// ------------------------------------------------------------------------------------
//  Ring buffers
// ------------------------------------------------------------------------------------

/**
 * Append `items` to `buffer`, keeping the newest `limit`.
 *
 * The whole batch is appended in one pass. The naive form — one `[...buffer, item]` per
 * event — copies 5 000 elements per console line, and a `train` run produces those in
 * bursts of hundreds.
 */
export function ringPush<T>(
  buffer: T[],
  items: readonly T[],
  limit: number,
): { next: T[]; dropped: number } {
  if (items.length === 0) return { next: buffer, dropped: 0 };
  const total = buffer.length + items.length;
  if (total <= limit) return { next: buffer.concat(items), dropped: 0 };
  const dropped = total - limit;
  // Slice the survivors out of each side rather than concatenating and trimming: when a
  // burst is larger than the cap, concatenating first allocates an array bigger than the
  // buffer will ever hold.
  if (items.length >= limit) {
    return { next: items.slice(items.length - limit), dropped };
  }
  return { next: buffer.slice(dropped).concat(items), dropped };
}

/**
 * The timeline's cap, which never evicts a `running` entry.
 *
 * A row that says what is executing right now is the one row the pane cannot do without;
 * dropping it to make room for history would make the pane wrong precisely while it is
 * being watched.
 */
function trimTimeline(entries: TimelineEntry[]): TimelineEntry[] {
  if (entries.length <= TIMELINE_LIMIT) return entries;
  const excess = entries.length - TIMELINE_LIMIT;
  const kept: TimelineEntry[] = [];
  let removed = 0;
  for (const entry of entries) {
    if (removed < excess && entry.status !== "running") {
      removed += 1;
      continue;
    }
    kept.push(entry);
  }
  return kept;
}

/**
 * Drop the source of every revision but the newest few, keeping their metadata.
 *
 * §3.7: "content dropped before metadata". A revision the operator can still see listed,
 * with its rationale and the error it addressed, is far more useful than one that
 * vanished entirely to save the same bytes.
 */
function trimRevisionContent(revisions: CodeRevisionRow[]): CodeRevisionRow[] {
  if (revisions.length <= CODE_CONTENT_LIMIT) return revisions;
  const firstKept = revisions.length - CODE_CONTENT_LIMIT;
  return revisions.map((revision, index) =>
    index < firstKept && revision.content !== undefined
      ? { ...revision, content: undefined }
      : revision,
  );
}

// ------------------------------------------------------------------------------------
//  The batch fold
// ------------------------------------------------------------------------------------

/**
 * A copy-on-write working set for one batch.
 *
 * Slices are read through `get` and written through `set`, so a batch that touches only
 * the console returns a patch containing only the console — which is what keeps Zustand's
 * shallow comparison from re-rendering the artifact deck on a `token.delta` (§6.3).
 *
 * The two high-volume buffers are special-cased: their appends are *collected* and pushed
 * through the ring once at commit, rather than copied per event.
 */
class Draft {
  readonly patch: Partial<RunStreamState> = {};
  readonly newEvents: RunEvent[] = [];
  readonly newConsole: ConsoleLine[] = [];
  readonly newMetrics: MetricPoint[] = [];
  readonly newRetrieval: RetrievalHitRow[] = [];

  constructor(private readonly base: RunStreamState) {}

  get<K extends keyof RunStreamState>(key: K): RunStreamState[K] {
    const patched = this.patch[key];
    return patched === undefined ? this.base[key] : (patched as RunStreamState[K]);
  }

  set<K extends keyof RunStreamState>(key: K, value: RunStreamState[K]): void {
    this.patch[key] = value;
  }

  /** Append a console line, coalescing token deltas into the row they continue. */
  pushConsole(line: ConsoleLine): void {
    this.newConsole.push(line);
  }

  /** The line a `token.delta` would continue, whether it is in this batch or the base. */
  lastConsoleLine(): ConsoleLine | undefined {
    if (this.newConsole.length > 0) return this.newConsole[this.newConsole.length - 1];
    const held = this.get("consoleLines");
    return held[held.length - 1];
  }

  replaceLastConsoleLine(line: ConsoleLine): void {
    if (this.newConsole.length > 0) {
      this.newConsole[this.newConsole.length - 1] = line;
      return;
    }
    const held = this.get("consoleLines");
    if (held.length === 0) return;
    const copy = held.slice();
    copy[copy.length - 1] = line;
    this.set("consoleLines", copy);
  }

  commit(): Partial<RunStreamState> {
    const limits = this.get("limits");
    if (this.newEvents.length > 0) {
      const { next, dropped } = ringPush(
        this.get("events"),
        this.newEvents,
        limits.events,
      );
      this.set("events", next);
      if (dropped > 0) this.set("droppedEvents", this.get("droppedEvents") + dropped);
    }
    if (this.newConsole.length > 0) {
      const { next, dropped } = ringPush(
        this.get("consoleLines"),
        this.newConsole,
        limits.console,
      );
      this.set("consoleLines", next);
      if (dropped > 0) {
        this.set("droppedConsoleLines", this.get("droppedConsoleLines") + dropped);
        // A client-side eviction is a hole in history exactly as a server-side one is,
        // and the panes must stop claiming to hold a complete record (§3.4).
        this.set("historyComplete", false);
      }
    }
    if (this.newMetrics.length > 0) {
      this.set(
        "metrics",
        ringPush(this.get("metrics"), this.newMetrics, METRIC_LIMIT).next,
      );
    }
    if (this.newRetrieval.length > 0) {
      this.set(
        "retrievalHits",
        ringPush(this.get("retrievalHits"), this.newRetrieval, RETRIEVAL_LIMIT).next,
      );
    }
    return this.patch;
  }
}

function reduceBatch(
  state: RunStreamState,
  events: readonly RunEvent[],
): Partial<RunStreamState> {
  const draft = new Draft(state);
  for (const event of events) {
    // Idempotency per `seq`, in one O(1) test (§6.3, §7.4 I3).
    //
    // `seq` is gapless and strictly increasing per run, so an event at or below the
    // highest already folded is a replayed duplicate. Structural idempotency backs this
    // up where it is cheap — timeline entries are keyed by the `seq` that opened them,
    // criteria merge by id, artifacts de-duplicate by name — so re-folding a whole
    // recorded stream into a fresh store reproduces the same state either way. What the
    // guard buys is that the append-only buffers do not need an O(n) membership test per
    // line to get there.
    if (event.seq > 0) {
      if (event.seq <= draft.get("lastSeq")) continue;
      draft.set("lastSeq", event.seq);
    }
    draft.newEvents.push(event);
    applyEvent(draft, event);
  }
  return draft.commit();
}

/**
 * Fold one event into the derived slices.
 *
 * An exhaustive `switch` over `RunEvent["type"]`: with the discriminated union generated
 * from `schemas/events.py` (§4.3), a new backend event becomes a compile error at the
 * `never` in the default case, listing this file as the one that must handle it. There is
 * deliberately no `String(p.x ?? "")` anywhere below — the compiler guarantees the shape,
 * and a defensive coercion would keep the runtime tolerant of a payload the contract says
 * cannot arrive, which is how a silent mismatch gets a second life.
 */
function applyEvent(draft: Draft, event: RunEvent): void {
  switch (event.type) {
    // ── protocol ────────────────────────────────────────────────────────────
    case "hello": {
      const { payload } = event;
      // `hello.run` is the summary hash, not a `RunRead`; the REST seed and
      // `run.snapshot` are the authorities on the run body. Only `last_seq` is taken —
      // it tells a fresh client where the stream is before replay starts.
      if (payload.last_seq > draft.get("lastSeq")) {
        draft.set("lastSeq", payload.last_seq);
      }
      return;
    }

    case "ping":
      // Answered synchronously by the hook and never folded: a heartbeat is a fact about
      // the connection, not about the run.
      return;

    case "replay.complete":
      draft.set("replayed", true);
      return;

    case "replay.gap":
      // §3.4: history the client asked for has been trimmed. The snapshot that follows
      // rebuilds the authoritative slices; the derived buffers stay suspect until a
      // fresh run, and the panes say so.
      draft.set("historyComplete", false);
      draft.set("oldestAvailable", event.payload.oldest_available);
      return;

    case "run.snapshot":
      // Wholesale, never a merge: it is the authoritative body, and a merge would
      // preserve stale fields that the gap invalidated (§6.3).
      draft.set("run", event.payload);
      draft.set("phase", (event.payload.phase as RunPhase | undefined) ?? draft.get("phase"));
      draft.set("activeNode", event.payload.current_node ?? null);
      draft.set("workerId", event.payload.worker_id ?? null);
      draft.set(
        "outcome",
        (event.payload.outcome as RunOutcome | undefined) ?? draft.get("outcome"),
      );
      if (event.payload.last_seq > draft.get("lastSeq")) {
        draft.set("lastSeq", event.payload.last_seq);
      }
      return;

    case "error":
      draft.set("error", {
        kind: "server",
        message: event.payload.message,
        retryable: event.payload.recoverable,
      });
      return;

    // ── run lifecycle ───────────────────────────────────────────────────────
    case "run.queued":
      draft.set("queuePosition", event.payload.position);
      return;

    case "run.started":
      draft.set("workerId", event.payload.worker_id);
      draft.set("modelRouting", event.payload.model_routing);
      draft.set("queuePosition", null);
      return;

    case "run.phase": {
      const phase = event.payload.phase as RunPhase;
      draft.set("phase", phase);
      draft.set("phaseHistory", [
        ...draft.get("phaseHistory"),
        { phase, ts: event.ts },
      ]);
      clearGateIfMovedOn(draft, event.seq);
      return;
    }

    case "run.completed":
    case "run.failed":
    case "run.cancelled": {
      const payload = event.payload;
      draft.set("terminal", true);
      draft.set("terminalPayload", payload);
      // A graph left with a node pulsing after the run ended is a lie about the system's
      // state (§6.3).
      draft.set("activeNode", null);
      draft.set("pendingGate", null);
      closeOpenEntries(draft, event.ts);

      const status = "status" in payload ? payload.status : "CANCELLED";
      draft.set("outcome", outcomeOf(event.type, status));
      draft.set("phase", "COMPLETE");
      if (payload.bundle_url) draft.set("bundleUrl", payload.bundle_url);
      if (payload.mlflow) draft.set("mlflow", payload.mlflow);
      if (payload.evaluation) applyVerdict(draft, payload.evaluation);
      if (payload.usage) applyUsage(draft, payload.usage);
      if (payload.deliverables.length > 0) {
        draft.set("artifacts", mergeDeliverables(draft.get("artifacts"), payload.deliverables));
      }
      return;
    }

    // ── node lifecycle ──────────────────────────────────────────────────────
    case "node.started": {
      const { node, agent, phase, model, plan_step_id } = event.payload;
      // The edge is the pair of *consecutive `node.started`s*, which is not the same as
      // "whatever was active": `node.completed` clears `activeNode` — a graph left with a
      // node pulsing after it finished would be a lie — so by the time the next node
      // starts there is nothing active to draw the edge from. The previous entry in the
      // timeline is the node control actually came from.
      const previous = draft.get("timeline").at(-1)?.node ?? null;
      if (previous && previous !== node) {
        const edge = `${previous}→${node}`;
        const traversals = draft.get("edgeTraversals");
        draft.set("edgeTraversals", { ...traversals, [edge]: (traversals[edge] ?? 0) + 1 });
        draft.set("lastEdge", edge);
      }
      draft.set("activeNode", node);
      if (phase) {
        const current = draft.get("phase");
        if (current !== phase) {
          draft.set("phase", phase as RunPhase);
          draft.set("phaseHistory", [
            ...draft.get("phaseHistory"),
            { phase: phase as RunPhase, ts: event.ts },
          ]);
        }
      }

      const nodes = draft.get("nodeState");
      const held = nodes[node];
      draft.set("nodeState", {
        ...nodes,
        [node]: {
          status: "running",
          visits: (held?.visits ?? 0) + 1,
          cumulativeMs: held?.cumulativeMs ?? 0,
          lastDurationMs: held?.lastDurationMs,
          lastError: held?.lastError,
          lastSummary: held?.lastSummary,
          model: model ?? held?.model ?? draft.get("modelRouting")[node],
        },
      });

      const timeline = draft.get("timeline");
      const id = String(event.seq);
      // Structural idempotency: the entry is keyed by the `seq` that opened it, so a
      // replayed `node.started` re-opens nothing.
      if (!timeline.some((entry) => entry.id === id)) {
        draft.set(
          "timeline",
          trimTimeline([
            ...timeline,
            {
              id,
              node,
              agent,
              seq: event.seq,
              status: "running",
              startedAt: event.ts,
              planStepId: plan_step_id,
            },
          ]),
        );
      }
      clearGateIfMovedOn(draft, event.seq);
      return;
    }

    case "node.progress": {
      const { node, message, percent } = event.payload;
      if (!message) return;
      draft.pushConsole({
        id: consoleLineId++,
        stream: "marker",
        tone: "info",
        text: percent == null ? `— ${node}: ${message} —` : `— ${node}: ${message} (${Math.round(percent)}%) —`,
        ts: event.ts,
        node,
        seq: event.seq,
      });
      return;
    }

    case "node.completed": {
      const p = event.payload;
      const status = p.degraded ? "degraded" : "succeeded";
      draft.set("activeNode", null);
      closeEntry(draft, p.node, {
        status,
        durationMs: p.duration_ms,
        tokensIn: p.tokens_in,
        tokensOut: p.tokens_out,
        llmCalls: p.llm_calls,
        summary: p.summary,
      });

      const nodes = draft.get("nodeState");
      const held = nodes[p.node];
      draft.set("nodeState", {
        ...nodes,
        [p.node]: {
          status,
          visits: held?.visits ?? 1,
          cumulativeMs: (held?.cumulativeMs ?? 0) + p.duration_ms,
          lastDurationMs: p.duration_ms,
          lastSummary: p.summary,
          lastError: held?.lastError,
          model: held?.model,
        },
      });

      const usage = draft.get("usage");
      draft.set("usage", {
        tokensIn: usage.tokensIn + p.tokens_in,
        tokensOut: usage.tokensOut + p.tokens_out,
        llmCalls: usage.llmCalls + p.llm_calls,
        nodeVisits: usage.nodeVisits + 1,
      });

      // The Debugger's diagnosis has no typed emitter yet (§15, B2), and its summary is
      // the only place the root cause reaches the client. Captured here rather than
      // re-derived by the diff viewer, which would have to scan the event ring to do it.
      if (p.node === "debugger" && p.summary) {
        draft.set("diagnoses", [
          ...draft.get("diagnoses"),
          {
            seq: event.seq,
            ts: event.ts,
            summary: p.summary,
            revision: draft.get("codeRevisions").at(-1)?.revision ?? null,
          },
        ]);
      }
      return;
    }

    case "node.failed": {
      const p = event.payload;
      closeEntry(draft, p.node, {
        status: "failed",
        error: p.error.message,
        errorKind: p.error.kind,
      });
      const nodes = draft.get("nodeState");
      const held = nodes[p.node];
      draft.set("nodeState", {
        ...nodes,
        [p.node]: {
          status: "failed",
          visits: held?.visits ?? 1,
          cumulativeMs: held?.cumulativeMs ?? 0,
          lastDurationMs: held?.lastDurationMs,
          lastError: `${p.error.kind}: ${p.error.message}`,
          lastSummary: held?.lastSummary,
          model: held?.model,
        },
      });
      if (!p.will_retry) draft.set("activeNode", null);
      return;
    }

    case "node.retrying": {
      const p = event.payload;
      // Annotates the open entry rather than adding a row (§8.5.4): three retries of one
      // node are one thing that happened, not three.
      const timeline = draft.get("timeline");
      for (let i = timeline.length - 1; i >= 0; i--) {
        const entry = timeline[i];
        if (entry && entry.node === p.node) {
          const copy = timeline.slice();
          copy[i] = {
            ...entry,
            status: "running",
            attempt: p.attempt,
            maxAttempts: p.max_attempts,
          };
          draft.set("timeline", copy);
          break;
        }
      }
      const nodes = draft.get("nodeState");
      const held = nodes[p.node];
      draft.set("nodeState", {
        ...nodes,
        [p.node]: {
          status: "running",
          visits: held?.visits ?? 1,
          cumulativeMs: held?.cumulativeMs ?? 0,
          lastDurationMs: held?.lastDurationMs,
          lastError: held?.lastError,
          lastSummary: held?.lastSummary,
          model: held?.model,
          retrying: { attempt: p.attempt, maxAttempts: p.max_attempts },
        },
      });
      draft.set("activeNode", p.node);
      return;
    }

    // ── agent work ──────────────────────────────────────────────────────────
    case "token.delta":
      appendToken(draft, event.payload.node, event.payload.text, event.ts, event.seq);
      return;

    case "tool.started":
      draft.pushConsole({
        id: consoleLineId++,
        stream: "marker",
        tone: "info",
        text: `— ${event.payload.node} → ${event.payload.tool} —`,
        ts: event.ts,
        node: event.payload.node,
        seq: event.seq,
      });
      return;

    case "tool.completed": {
      const p = event.payload;
      draft.pushConsole({
        id: consoleLineId++,
        stream: "marker",
        tone: p.ok ? "info" : "fail",
        text: `— ${p.node} ← ${p.tool} ${p.ok ? "ok" : "failed"} in ${p.duration_ms}ms —`,
        ts: event.ts,
        node: p.node,
        seq: event.seq,
      });
      return;
    }

    case "retrieval.results": {
      const { query, hits } = event.payload;
      for (const hit of hits) {
        draft.newRetrieval.push({
          query,
          sourceUri: hit.source_uri,
          section: hit.section,
          title: hit.title,
          score: hit.score,
          collection: hit.collection,
          trustLevel: hit.trust_level,
          seq: event.seq,
        });
      }
      return;
    }

    case "plan.created":
      draft.set("planSteps", event.payload.steps.map(toPlanStep));
      draft.set("planRevision", event.payload.revision);
      draft.set("criteria", criteriaFrom(event.payload.success_criteria));
      return;

    case "plan.revised":
      // §8.5.4: a revision visibly resets the ledger. Comparing against a silently
      // changed contract is how a REPLAN looks like a regression, so the criteria are
      // replaced outright and the verdict that judged the old ones is cleared.
      draft.set("planSteps", event.payload.steps.map(toPlanStep));
      draft.set("planRevision", event.payload.revision);
      draft.set("criteria", criteriaFrom(event.payload.success_criteria));
      draft.set("verdict", null);
      return;

    case "code.revision": {
      const p = event.payload;
      const held = draft.get("codeRevisions");
      if (held.some((revision) => revision.revision === p.revision)) return;
      const next = ringPush(
        held,
        [
          {
            revision: p.revision,
            path: p.path,
            language: p.language,
            sha256: p.sha256,
            linesChanged: p.lines_changed,
            diff: p.diff,
            rationale: p.rationale,
            addressesError: p.addresses_error,
            ts: event.ts,
          },
        ],
        CODE_REVISION_LIMIT,
      ).next;
      draft.set("codeRevisions", trimRevisionContent(next));
      // Correlate the open `coder` entry with the revision it produced (§8.5.4).
      annotateOpen(draft, "coder", { revision: p.revision });
      return;
    }

    // ── sandbox ─────────────────────────────────────────────────────────────
    case "sandbox.started": {
      const p = event.payload;
      const held = draft.get("executions");
      if (!held.some((execution) => execution.executionId === p.execution_id)) {
        draft.set("executions", [
          ...held,
          {
            executionId: p.execution_id,
            profile: p.profile,
            revision: p.revision,
            image: p.image,
            limits: {
              cpus: p.limits.cpus,
              memory: p.limits.memory,
              timeoutS: p.limits.timeout_s,
              network: p.limits.network,
            },
            startedAt: event.ts,
          },
        ]);
      }
      annotateOpen(draft, "sandbox_exec", {
        executionId: p.execution_id,
        revision: p.revision,
      });
      draft.pushConsole({
        id: consoleLineId++,
        stream: "marker",
        tone: "info",
        text: `— sandbox ${p.execution_id.slice(0, 8)} started · ${p.profile} · rev ${p.revision} · ${p.limits.cpus ?? "?"} CPU · ${p.limits.memory} · net ${p.limits.network} —`,
        ts: event.ts,
        executionId: p.execution_id,
        seq: event.seq,
      });
      return;
    }

    case "sandbox.stdout":
    case "sandbox.stderr":
      draft.pushConsole({
        id: consoleLineId++,
        stream: event.type === "sandbox.stderr" ? "stderr" : "stdout",
        text: event.payload.line,
        ts: event.payload.ts || event.ts,
        executionId: event.payload.execution_id || undefined,
        seq: event.seq,
      });
      return;

    case "sandbox.truncated": {
      const p = event.payload;
      draft.set("truncations", [
        ...draft.get("truncations"),
        {
          executionId: p.execution_id,
          stream: p.stream,
          bytesDropped: p.bytes_dropped,
        },
      ]);
      draft.pushConsole({
        id: consoleLineId++,
        stream: "marker",
        tone: "warn",
        text: `— ${formatDropped(p.bytes_dropped)} of ${p.stream} dropped: the output cap was reached —`,
        ts: event.ts,
        executionId: p.execution_id,
        seq: event.seq,
      });
      return;
    }

    case "sandbox.exit": {
      const p = event.payload;
      const held = draft.get("executions");
      const index = held.findIndex((execution) => execution.executionId === p.execution_id);
      if (index >= 0) {
        const copy = held.slice();
        copy[index] = {
          ...held[index]!,
          exitCode: p.exit_code,
          timedOut: p.timed_out,
          oomKilled: p.oom_killed,
          durationMs: p.duration_ms,
          maxRssBytes: p.max_rss_bytes,
        };
        draft.set("executions", copy);
      }
      const how = p.timed_out
        ? "timed out"
        : p.oom_killed
          ? "OOM-killed"
          : `exit ${p.exit_code ?? "?"}`;
      draft.pushConsole({
        id: consoleLineId++,
        stream: "marker",
        tone: p.exit_code === 0 && !p.timed_out && !p.oom_killed ? "info" : "fail",
        text: `— sandbox ${p.execution_id.slice(0, 8)} ${how} in ${p.duration_ms}ms —`,
        ts: event.ts,
        executionId: p.execution_id,
        seq: event.seq,
      });
      return;
    }

    // ── results ─────────────────────────────────────────────────────────────
    case "artifact.created":
      draft.set("artifacts", mergeArtifact(draft.get("artifacts"), event.payload));
      return;

    case "metric.logged":
      draft.newMetrics.push({
        key: event.payload.key,
        value: event.payload.value,
        step: event.payload.step,
        ts: event.ts,
      });
      return;

    case "evaluation.completed":
      applyVerdict(draft, event.payload);
      return;

    // ── operator attention ──────────────────────────────────────────────────
    case "interrupt.requested": {
      const p = event.payload;
      draft.set("pendingGate", {
        gate: p.gate as GateName,
        prompt: p.prompt,
        options: p.options,
        expiresAt: p.expires_at || null,
        context: p.context,
        seq: event.seq,
      });
      return;
    }

    case "budget.warning": {
      const p = event.payload;
      draft.set("budgets", {
        ...draft.get("budgets"),
        [p.resource]: { used: p.used, limit: p.limit, percent: p.percent },
      });
      return;
    }

    default: {
      // Exhaustiveness. A new `EventType` reaches the frontend as a compile error here,
      // naming the file that must handle it — which is the entire point of generating the
      // union from `schemas/events.py` (§4.3).
      const unreachable: never = event;
      return unreachable;
    }
  }
}

// ------------------------------------------------------------------------------------
//  Fold helpers
// ------------------------------------------------------------------------------------

function outcomeOf(type: string, status: string): RunOutcome {
  if (type === "run.cancelled") return "CANCELLED";
  if (type === "run.failed") return "FAILED";
  return status === "PARTIAL" ? "PARTIAL" : "SUCCEEDED";
}

/**
 * Close the most recent *open* entry for `node`.
 *
 * Searched from the end, never the front: loop 1 visits `coder` several times in one run
 * (AGENTS.md §6.1), and the completion belongs to the visit that is still open. Matching
 * forwards would close the first visit repeatedly and leave the real one running forever.
 */
function closeEntry(
  draft: Draft,
  node: string,
  patch: Partial<TimelineEntry>,
): void {
  const timeline = draft.get("timeline");
  for (let i = timeline.length - 1; i >= 0; i--) {
    const entry = timeline[i];
    if (entry && entry.node === node && entry.status === "running") {
      const copy = timeline.slice();
      copy[i] = { ...entry, ...patch };
      draft.set("timeline", copy);
      return;
    }
  }
}

/** Annotate the most recent open entry for `node` without closing it. */
function annotateOpen(draft: Draft, node: string, patch: Partial<TimelineEntry>): void {
  const timeline = draft.get("timeline");
  for (let i = timeline.length - 1; i >= 0; i--) {
    const entry = timeline[i];
    if (entry && entry.node === node && entry.status === "running") {
      const copy = timeline.slice();
      copy[i] = { ...entry, ...patch };
      draft.set("timeline", copy);
      return;
    }
  }
}

/** A terminal event ends every visit that never reported an end of its own. */
function closeOpenEntries(draft: Draft, ts: string): void {
  const timeline = draft.get("timeline");
  if (!timeline.some((entry) => entry.status === "running")) return;
  draft.set(
    "timeline",
    timeline.map((entry) =>
      entry.status === "running"
        ? {
            ...entry,
            status: "failed",
            error: entry.error ?? "the run ended while this node was executing",
            durationMs:
              entry.durationMs ?? Math.max(0, Date.parse(ts) - Date.parse(entry.startedAt)),
          }
        : entry,
    ),
  );
  const nodes = draft.get("nodeState");
  let touched = false;
  const next: Record<string, GraphNodeState> = { ...nodes };
  for (const [name, node] of Object.entries(nodes)) {
    if (node.status === "running") {
      next[name] = { ...node, status: "failed" };
      touched = true;
    }
  }
  if (touched) draft.set("nodeState", next);
}

/**
 * The graph moving on is the only signal that a gate was released.
 *
 * The protocol has no `interrupt.resolved`: the run simply leaves `AWAITING_INPUT` and
 * the next node starts. Clearing on any later node entry or phase change is therefore the
 * honest reading, and it is why §8.6 forbids closing the dialog on the *response* to an
 * approval — delivery is not effect.
 */
function clearGateIfMovedOn(draft: Draft, seq: number): void {
  const gate = draft.get("pendingGate");
  if (gate && seq > gate.seq) draft.set("pendingGate", null);
}

function toPlanStep(step: PlanStepRef): PlanStepRow {
  return {
    id: step.id,
    index: step.index,
    title: step.title,
    description: step.description,
    kind: step.kind,
    dependsOn: step.depends_on,
    status: step.status,
    attempts: step.attempts,
  };
}

function criteriaFrom(criteria: SuccessCriterionRef[]): CriterionRow[] {
  return criteria.map((criterion) => ({
    id: criterion.id,
    metric: criterion.metric,
    comparator: criterion.comparator,
    threshold: criterion.threshold,
    tolerance: criterion.tolerance,
    required: criterion.required,
    weight: criterion.weight,
    rationale: criterion.rationale,
    observed: null,
    passed: null,
    note: "",
    verdictSeen: false,
  }));
}

/**
 * Merge the evaluator's observations into the plan's contract, by `criterion_id`.
 *
 * A result whose id the plan never declared is still shown: the evaluator checked
 * something, and hiding it because the two lists disagree would hide the disagreement.
 */
function mergeCriteria(
  criteria: CriterionRow[],
  results: CriterionResultRef[],
): CriterionRow[] {
  if (results.length === 0) {
    return criteria.map((row) => ({ ...row, verdictSeen: true }));
  }
  const byId = new Map(criteria.map((row) => [row.id, row]));
  for (const result of results) {
    const held = byId.get(result.criterion_id);
    byId.set(result.criterion_id, {
      id: result.criterion_id,
      metric: result.metric,
      comparator: result.comparator,
      threshold: result.threshold,
      tolerance: held?.tolerance ?? 0,
      required: result.required,
      weight: result.weight,
      rationale: held?.rationale ?? "",
      observed: result.observed,
      passed: result.passed,
      note: result.note,
      verdictSeen: true,
    });
  }
  // Criteria the verdict did not mention are still judged by it: absence is not success
  // (AGENTS.md §7.6), and `verdictSeen` is what makes the ledger render them as failed.
  return [...byId.values()].map((row) => ({ ...row, verdictSeen: true }));
}

function applyVerdict(
  draft: Draft,
  verdict: EvaluationRef | Extract<RunEvent, { type: "evaluation.completed" }>["payload"],
): void {
  draft.set("criteria", mergeCriteria(draft.get("criteria"), verdict.criteria_results));
  draft.set("verdict", {
    decision: verdict.decision,
    passed: verdict.passed,
    score: verdict.score,
    rubric: verdict.rubric.map((score) => ({
      dimension: score.dimension,
      score: score.score,
      justification: score.justification,
    })),
    rubricMean: verdict.rubric_mean,
    summary: verdict.summary,
    directive: verdict.replan_directive ?? verdict.refine_directive,
  });
}

function applyUsage(draft: Draft, usage: UsageRef): void {
  // The terminal payload's usage is the run's own accumulation and is authoritative over
  // the client's running sum, which can be short by whatever the ring evicted.
  draft.set("usage", {
    tokensIn: usage.tokens_in,
    tokensOut: usage.tokens_out,
    llmCalls: usage.llm_calls,
    nodeVisits: usage.node_visits,
  });
}

function mergeArtifact(
  artifacts: ArtifactRow[],
  payload: ArtifactCreatedPayload,
): ArtifactRow[] {
  const row: ArtifactRow = {
    artifactId: payload.artifact_id || null,
    name: payload.name,
    type: payload.type,
    sizeBytes: payload.size_bytes,
    sha256: payload.sha256,
    downloadUrl: payload.download_url,
    reconstructed: false,
  };
  const index = artifacts.findIndex((held) => held.name === row.name);
  if (index < 0) return [...artifacts, row];
  const copy = artifacts.slice();
  copy[index] = row;
  return copy;
}

/**
 * Fold the terminal payload's deliverables in beside the live tiles.
 *
 * De-duplicated by `name`, and a live tile wins: it carries the `download_url` and the
 * artifact id that a deliverable record does not. What a deliverable adds is the rows for
 * a run whose `artifact.created` events were never seen — a tab opened after the fact, or
 * history the ring evicted — and those are marked `reconstructed` so §2.4's rule about
 * labelling lossy fallbacks holds.
 */
function mergeDeliverables(
  artifacts: ArtifactRow[],
  deliverables: DeliverableRef[],
): ArtifactRow[] {
  const held = new Set(artifacts.map((artifact) => artifact.name));
  const extra = deliverables
    .filter((deliverable) => !held.has(deliverable.name))
    .map<ArtifactRow>((deliverable) => ({
      artifactId: deliverable.artifact_id,
      name: deliverable.name,
      type: deliverable.artifact_type,
      sizeBytes: deliverable.size_bytes,
      sha256: deliverable.sha256,
      downloadUrl: null,
      reconstructed: true,
    }));
  return extra.length > 0 ? [...artifacts, ...extra] : artifacts;
}

/**
 * Append an LLM token delta to the row it continues.
 *
 * §8.5.5: "A delta appended to the last token line MUST mutate that row, not push a new
 * one, or a 4 000-token plan produces 4 000 rows." A newline inside the delta closes the
 * row and opens the next, which is what keeps a streamed JSON plan readable instead of
 * arriving as one line thousands of characters wide.
 */
function appendToken(
  draft: Draft,
  node: string,
  text: string,
  ts: string,
  seq: number,
): void {
  if (!text) return;
  const last = draft.lastConsoleLine();
  const continues = last !== undefined && last.stream === "token" && last.node === node;
  const combined = continues ? last.text + text : text;
  const parts = combined.split("\n");
  const tail = parts.pop() ?? "";

  if (continues) {
    if (parts.length === 0) {
      draft.replaceLastConsoleLine({ ...last, text: combined, seq });
      return;
    }
    // The row being continued becomes the first completed line; the rest are new.
    draft.replaceLastConsoleLine({ ...last, text: parts[0]!, seq });
    for (const line of parts.slice(1)) {
      draft.pushConsole({
        id: consoleLineId++,
        stream: "token",
        text: line,
        ts,
        node,
        seq,
      });
    }
  } else {
    for (const line of parts) {
      draft.pushConsole({
        id: consoleLineId++,
        stream: "token",
        text: line,
        ts,
        node,
        seq,
      });
    }
  }

  draft.pushConsole({
    id: consoleLineId++,
    stream: "token",
    text: tail,
    ts,
    node,
    seq,
  });
}

function formatDropped(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
