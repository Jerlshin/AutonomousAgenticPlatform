"use client";

/**
 * `useRunStream` — the only place in this app that touches a `WebSocket` (§3, §7).
 *
 * It owns ticket acquisition, the §9.8 reconnection algorithm, the sequence cursor, gap
 * recovery, the frame batcher and the two operator commands. Every pane reads the store
 * this fills; none of them knows a socket exists.
 *
 * ```
 * mount
 *   ├─ GET  /api/v1/runs/{id}            seed the header before the first frame
 *   ├─ POST /api/v1/ws/tickets           single-use, 60 s, run-scoped
 *   ├─ WS   /api/v1/ws/runs/{id}?ticket=…&after_seq={cursor}   subprotocol pluton.v1
 *   ├─ ← hello · replay … · replay.complete · live tail · ping → pong
 *   └─ unmount → close(1000)
 * ```
 *
 * Four details are easy to get wrong and expensive when you do, so each is called out at
 * the line that implements it:
 *
 * * the cursor advances only on `seq > 0`, because control frames carry `seq: 0` and
 *   treating a `ping` as progress skips real history on the next reconnect;
 * * the ticket is re-minted per attempt, because the server consumes it with `GETDEL` at
 *   accept time — a cached ticket works once and then fails every retry in the backoff
 *   loop, which looks exactly like a server that is down;
 * * any `subscribe` unions the caller's filter with the control floor, because the server
 *   applies the filter to `ping` too and a client that never sees a ping never sends a
 *   pong and is heartbeat-killed in ~40 s;
 * * store commits are batched to one animation frame, because a `token.delta` arrives
 *   several times a second and a commit per frame is a React reconciliation per frame.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import { api, ApiError, runSocketUrl } from "@/lib/api";
import { CloseCode, PROTOCOL, TERMINAL_EVENTS } from "@/lib/events.generated";
import type { RunEvent } from "@/lib/events.generated";
import { log } from "@/lib/log";
import type { RunRead } from "@/lib/rest";
import type {
  ArtifactRow,
  CodeRevisionRow,
  CommandResult,
  ConsoleLine,
  CriterionRow,
  GateDecision,
  GateName,
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
import {
  createRunStore,
  type BufferLimits,
  type RunStoreApi,
  type RunStreamState,
  type StreamStatus,
  type TerminalPayload,
} from "@/stores/runStore";

// ------------------------------------------------------------------------------------
//  Constants
// ------------------------------------------------------------------------------------

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 30_000;
const JITTER_MS = 250;

/**
 * `4429` means this tab is competing with other views of the same run for the 8-per-run
 * quota. Hammering at 500 ms guarantees it keeps losing.
 */
const QUOTA_BACKOFF_FLOOR_MS = 5_000;

/** Three consecutive `4401`s is a rejected credential, not a flaky one (§3.2). */
const MAX_AUTH_FAILURES = 3;

/**
 * The `subscribe` control floor (§3.5).
 *
 * The server applies a connection's filter to **every** outbound frame, control frames
 * included, and the heartbeat increments `missed_pongs` whether or not the `ping` was
 * actually written. A filter without these entries is therefore a connection that is
 * closed with `1001` roughly every forty seconds, forever. Prefix entries end in `.`;
 * the server matches both exact names and prefixes.
 */
export const CONTROL_FLOOR: readonly string[] = [
  "ping",
  "hello",
  "error",
  "replay.",
  "run.snapshot",
];

/** How many latency samples the §9.5 reservoir holds. */
const LATENCY_RESERVOIR = 200;

/** Background tabs throttle `requestAnimationFrame` to ~1 Hz; this is the fallback (§6.4). */
const HIDDEN_FLUSH_MS = 250;

// ------------------------------------------------------------------------------------
//  Public contract (§7.1–§7.3)
// ------------------------------------------------------------------------------------

export interface UseRunStreamOptions {
  /**
   * Server-side `subscribe` filter (§9.5). Exact types, or a family prefix ending in "."
   * or "*". The hook ALWAYS unions this with the control floor.
   *
   * MUST be a stable reference — a module constant. It is an effect dependency, and a
   * fresh array literal per render reconnects the socket on every render.
   */
  types?: readonly string[];

  /** `false` leaves the socket closed; REST-only views (the report page) pass false. */
  enabled?: boolean;

  /** Ring-buffer overrides. Default to the §3.7 caps; tests use small values. */
  eventLimit?: number;
  consoleLimit?: number;

  /** Called for every ingested event, after the fold. MUST be stable (`useCallback`). */
  onEvent?: (event: RunEvent) => void;

  /** Called once when the run reaches a terminal state, with the terminal payload. */
  onTerminal?: (outcome: RunOutcome, payload: TerminalPayload) => void;

  /** Called when a HITL gate opens. Used to raise the gate console (§8.6). */
  onGate?: (gate: PendingGate) => void;
}

/** §9.5 instrumentation, surfaced in the debug drawer. */
export interface StreamStats {
  /** Worker `XADD` → client receipt, in ms. Clock skew makes the absolute value soft. */
  latencyP50: number | null;
  latencyP95: number | null;
  samples: number;
  eventsReceived: number;
  commits: number;
  reconnects: number;
}

export interface RunStream {
  /**
   * The store this stream owns and writes.
   *
   * Not in §7.3's list, and it is here for one reason: §6.3 says "one store per mounted
   * run stream", so the panes need a handle to the *right* one. `RunDeck` puts it in
   * scope with `<RunStoreProvider store={stream.store}>` and the panes subscribe through
   * `useRunSlice`. Nothing reads run data off it directly.
   */
  store: RunStoreApi;

  // ── connection ──────────────────────────────────────────────────────────
  status: StreamStatus;
  connected: boolean;
  lastSeq: number;
  replayed: boolean;
  historyComplete: boolean;
  reconnectAttempts: number;
  nextRetryInMs: number | null;
  effectiveFilter: string[] | null;
  error: StreamError | null;

  // ── run state ───────────────────────────────────────────────────────────
  //
  // The scalars below are *subscribed*: a change re-renders the caller, which is what the
  // deck header needs. The array slices after them are **snapshots read on access** — see
  // the note on `snapshot()` — because subscribing `RunDeck` to `consoleLines` would
  // re-render the whole deck on every `token.delta`, which is the exact budget §9.1
  // forbids. Panes subscribe to their own slice through `useRunSlice` (§6.5).
  run: RunRead | null;
  phase: RunPhase | null;
  terminal: boolean;
  outcome: RunOutcome | null;
  activeNode: string | null;
  pendingGate: PendingGate | null;
  usage: RunStreamState["usage"];
  budgets: RunStreamState["budgets"];
  droppedEvents: number;
  droppedConsoleLines: number;

  readonly nodeState: RunStreamState["nodeState"];
  readonly edgeTraversals: Record<string, number>;
  readonly timeline: TimelineEntry[];
  readonly planSteps: PlanStepRow[];
  readonly criteria: CriterionRow[];
  readonly verdict: VerdictRow | null;
  readonly consoleLines: ConsoleLine[];
  readonly executions: SandboxExecutionRow[];
  readonly artifacts: ArtifactRow[];
  readonly metrics: MetricPoint[];
  readonly mlflow: RunStreamState["mlflow"];
  readonly codeRevisions: CodeRevisionRow[];
  readonly retrievalHits: RetrievalHitRow[];

  // ── instrumentation (§9.5) ──────────────────────────────────────────────
  stats: StreamStats;

  // ── commands (§7.5) ─────────────────────────────────────────────────────
  cancel(reason?: string): Promise<CommandResult>;
  approve(gate: GateName, decision: GateDecision, notes?: string): Promise<CommandResult>;
  resync(): void;
  /** Operator-initiated. Resets the backoff, so "try again" means now. */
  reconnectNow(): void;
}

// ------------------------------------------------------------------------------------
//  Hook
// ------------------------------------------------------------------------------------

export function useRunStream(
  runId: string,
  options: UseRunStreamOptions = {},
): RunStream {
  const { types, enabled = true, eventLimit, consoleLimit, onEvent, onTerminal, onGate } =
    options;

  // Created once per mounted hook, never shared. Two views of one run in one tab hold two
  // folds of it, which is correct: they may be scrolled to different places, filtered
  // differently, and — on the dashboard — watching different runs entirely.
  const [store] = useState(createRunStore);

  // ---- connection state that must not re-render on change ----------------
  //
  // All of it lives in refs. The reconnect closure has to read the *current* cursor, not
  // the value captured by the render that created it (§3.3), and a socket that
  // re-rendered the deck on every backoff tick would be its own performance problem.
  const socketRef = useRef<WebSocket | null>(null);
  const cursorRef = useRef(0);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const authFailuresRef = useRef(0);
  const stoppedRef = useRef(false);
  const activeRunIdRef = useRef<string | null>(null);
  const terminalNotifiedRef = useRef(false);
  const gateNotifiedRef = useRef(-1);

  // ---- the frame batcher (§6.4) ------------------------------------------
  const pendingRef = useRef<RunEvent[]>([]);
  const frameRef = useRef<number | null>(null);
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ---- instrumentation ---------------------------------------------------
  const latencyRef = useRef<number[]>([]);
  const receivedRef = useRef(0);
  const commitsRef = useRef(0);
  const reconnectsRef = useRef(0);

  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const [nextRetryInMs, setNextRetryInMs] = useState<number | null>(null);
  const [stats, setStats] = useState<StreamStats>({
    latencyP50: null,
    latencyP95: null,
    samples: 0,
    eventsReceived: 0,
    commits: 0,
    reconnects: 0,
  });

  // Callbacks are read through a ref so a caller that forgets `useCallback` re-renders
  // rather than reconnects. The effect below depends on the ref, which never changes.
  const handlersRef = useRef({ onEvent, onTerminal, onGate });
  handlersRef.current = { onEvent, onTerminal, onGate };

  const limits: Partial<BufferLimits> = useMemo(
    () => ({ events: eventLimit, console: consoleLimit }),
    [eventLimit, consoleLimit],
  );

  /** The filter actually sent: the caller's types unioned with the control floor. */
  const effectiveFilter = useMemo(() => {
    if (!types || types.length === 0) return null;
    return [...new Set([...types, ...CONTROL_FLOOR])];
  }, [types]);

  // ---- subscribed slices (scalars only — see the note on RunStream) -------
  const view = useStore(
    store,
    useShallow((state: RunStreamState) => ({
      status: state.status,
      lastSeq: state.lastSeq,
      replayed: state.replayed,
      historyComplete: state.historyComplete,
      error: state.error,
      run: state.run,
      phase: state.phase,
      terminal: state.terminal,
      outcome: state.outcome,
      activeNode: state.activeNode,
      pendingGate: state.pendingGate,
      usage: state.usage,
      budgets: state.budgets,
      droppedEvents: state.droppedEvents,
      droppedConsoleLines: state.droppedConsoleLines,
      storeFilter: state.effectiveFilter,
    })),
  );

  // ------------------------------------------------------------------------
  //  Batcher
  // ------------------------------------------------------------------------

  const flush = useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const batch = pendingRef.current.splice(0, pendingRef.current.length);
    if (batch.length === 0) return;

    store.getState().ingest(batch);
    commitsRef.current += 1;

    const { onEvent: notify, onTerminal: notifyTerminal, onGate: notifyGate } =
      handlersRef.current;
    for (const event of batch) {
      notify?.(event);
    }

    const state = store.getState();
    if (state.pendingGate && state.pendingGate.seq > gateNotifiedRef.current) {
      gateNotifiedRef.current = state.pendingGate.seq;
      notifyGate?.(state.pendingGate);
    }
    if (state.terminal && !terminalNotifiedRef.current && state.terminalPayload) {
      terminalNotifiedRef.current = true;
      notifyTerminal?.(state.outcome ?? "FAILED", state.terminalPayload);
    }
  }, [store]);

  const enqueue = useCallback(
    (event: RunEvent, immediate: boolean) => {
      pendingRef.current.push(event);
      if (immediate) {
        flush();
        return;
      }
      if (frameRef.current !== null || flushTimerRef.current !== null) return;

      // A backgrounded tab throttles rAF to roughly 1 Hz, and a run that finishes while
      // the tab is hidden should not need a foreground repaint to become correct (§6.4).
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        flushTimerRef.current = setTimeout(() => {
          flushTimerRef.current = null;
          flush();
        }, HIDDEN_FLUSH_MS);
        return;
      }
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        flush();
      });
    },
    [flush],
  );

  // ------------------------------------------------------------------------
  //  The connection
  // ------------------------------------------------------------------------

  useEffect(() => {
    if (!runId || !enabled) {
      store.getState().setStatus("idle");
      return;
    }

    const actions = store.getState();
    // I6: a `runId` change resets every buffer and the cursor before the first frame of
    // the new run is folded. A re-run of this effect for the *same* run — React 19 Strict
    // Mode double-mounts in development — must not wipe what has already been received.
    if (activeRunIdRef.current !== runId) {
      activeRunIdRef.current = runId;
      cursorRef.current = 0;
      terminalNotifiedRef.current = false;
      gateNotifiedRef.current = -1;
      latencyRef.current = [];
      actions.reset(runId, limits);
    }
    actions.setEffectiveFilter(effectiveFilter);

    let alive = true;
    stoppedRef.current = false;
    backoffRef.current = INITIAL_BACKOFF_MS;
    authFailuresRef.current = 0;

    const setStatus = (status: StreamStatus) => {
      if (alive) store.getState().setStatus(status);
    };
    const setError = (error: StreamError | null) => {
      if (alive) store.getState().setError(error);
    };

    const stop = (error: StreamError) => {
      stoppedRef.current = true;
      setError(error);
      setStatus("closed");
      setNextRetryInMs(null);
    };

    // The REST read is not redundant with `hello`. A QUEUED run has nothing in its stream
    // but `run.queued`, and the header would otherwise render empty until a worker picks
    // the job up — which on a busy box is minutes (§3.1).
    void api
      .getRun(runId)
      .then((run) => {
        if (alive) store.getState().setRun(run);
      })
      .catch((cause: unknown) => {
        // A 404 here is a real answer: the route should render not-found rather than a
        // deck waiting on a socket that will be closed with 4404 in a moment.
        if (cause instanceof ApiError && cause.status === 404) {
          stop({
            kind: "not_found",
            message: `Run ${runId} does not exist.`,
            code: 404,
            retryable: false,
          });
        }
        log.stream("seed failed", cause);
      });

    const schedule = (delayMs: number) => {
      if (stoppedRef.current || !alive) return;
      // §3.6: pause reconnection only while the tab is hidden *and* the run is terminal.
      // A backgrounded tab watching a live run is the normal case, and replay is exactly
      // what makes it work.
      if (
        typeof document !== "undefined" &&
        document.visibilityState === "hidden" &&
        store.getState().terminal
      ) {
        setStatus("closed");
        return;
      }
      const jittered = delayMs + Math.random() * JITTER_MS;
      setNextRetryInMs(Math.round(jittered));
      setStatus("reconnecting");
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        setNextRetryInMs(null);
        void connect();
      }, jittered);
    };

    const connect = async (): Promise<void> => {
      if (stoppedRef.current || !alive) return;
      setStatus(cursorRef.current > 0 ? "reconnecting" : "connecting");

      let ticket: string | undefined;
      try {
        ticket = (await api.wsTicket(runId)).ticket;
      } catch (cause) {
        // Best-effort by design (§3.2): a loopback development box runs without a token,
        // and failing hard there makes the dashboard useless exactly where it is used
        // most. Connect without a ticket and let the server decide.
        log.stream("ticket minting failed; connecting without one", cause);
        ticket = undefined;
      }
      if (stoppedRef.current || !alive) return;

      const socket = new WebSocket(
        runSocketUrl(runId, { afterSeq: cursorRef.current, ticket }),
        [PROTOCOL],
      );
      socketRef.current = socket;

      socket.onopen = () => {
        if (!alive) {
          socket.close(CloseCode.NORMAL, "stale connection");
          return;
        }
        // §3.6: the backoff resets on `open`, not on `hello`. A socket that opens and is
        // immediately closed by quota still counts as progress toward the ceiling.
        backoffRef.current = INITIAL_BACKOFF_MS;
        setStatus("open");
        setError(null);
        setNextRetryInMs(null);
        if (effectiveFilter) {
          socket.send(
            JSON.stringify({ type: "subscribe", payload: { types: effectiveFilter } }),
          );
        }
      };

      socket.onmessage = (message) => {
        if (!alive) return;
        let event: RunEvent;
        try {
          event = JSON.parse(message.data as string) as RunEvent;
        } catch {
          log.stream("dropped a frame that was not JSON");
          return;
        }

        // §9.2: a client seeing `v !== 1` must close with 4400 rather than guess. Guessing
        // at an unknown protocol version is worse than failing.
        if (event.v !== 1) {
          socket.close(CloseCode.PROTOCOL_ERROR, "unsupported protocol version");
          stop({
            kind: "protocol",
            message: `The server is speaking protocol v${String(event.v)}; this client only understands v1.`,
            code: CloseCode.PROTOCOL_ERROR,
            retryable: false,
          });
          return;
        }

        receivedRef.current += 1;

        if (event.type === "ping") {
          // Synchronously, never through the batcher: it costs one frame, and deferring
          // it risks crossing the 40 s missed-pong window on a loaded tab (§3.5).
          socket.send(JSON.stringify({ type: "pong" }));
          return;
        }

        if (event.type === "hello") {
          // Only a frame that reached the application layer proves the credential was
          // accepted; the socket itself opens before authentication runs.
          authFailuresRef.current = 0;
        }

        // §3.3: the cursor advances only on `seq > 0`, and monotonically — `seq` is
        // authoritative, arrival order is not.
        if (event.seq > 0) {
          cursorRef.current = Math.max(cursorRef.current, event.seq);
          sample(latencyRef.current, Date.now() - Date.parse(event.ts));
        }

        if (event.type === "replay.gap") {
          // §3.4: reset to 0 so the connection replays everything still retained, and
          // await the `run.snapshot` the server sends next.
          cursorRef.current = 0;
        }

        const immediate =
          event.type === "interrupt.requested" ||
          (TERMINAL_EVENTS as readonly string[]).includes(event.type);
        enqueue(event, immediate);
      };

      socket.onclose = (close) => {
        if (socketRef.current === socket) socketRef.current = null;
        if (!alive || stoppedRef.current) return;
        flush();

        const terminal = store.getState().terminal;
        log.stream("closed", close.code, close.reason, { terminal });

        switch (close.code) {
          case CloseCode.PROTOCOL_ERROR:
            stop({
              kind: "protocol",
              message:
                close.reason ||
                "The server rejected this client's protocol. Retrying cannot help — please report this.",
              code: close.code,
              retryable: false,
            });
            return;

          case CloseCode.FORBIDDEN:
            stop({
              kind: "forbidden",
              message: close.reason || "This credential is not permitted to watch this run.",
              code: close.code,
              retryable: false,
            });
            return;

          case CloseCode.NOT_FOUND:
            stop({
              kind: "not_found",
              message: close.reason || `Run ${runId} does not exist.`,
              code: close.code,
              retryable: false,
            });
            return;

          case CloseCode.UNAUTHENTICATED: {
            authFailuresRef.current += 1;
            if (authFailuresRef.current >= MAX_AUTH_FAILURES) {
              stop({
                kind: "auth",
                message:
                  "The API token or WebSocket ticket was rejected three times. Check NEXT_PUBLIC_API_TOKEN.",
                code: close.code,
                retryable: false,
              });
              return;
            }
            // Re-mint and reconnect immediately for the first two attempts: an expired
            // ticket is fixed by minting another one, not by waiting.
            setReconnectAttempts((n) => n + 1);
            reconnectsRef.current += 1;
            void connect();
            return;
          }

          case CloseCode.QUOTA_EXCEEDED:
            setError({
              kind: "quota",
              message:
                "Too many open views of this run (the limit is 8). Close another tab, or this one will keep retrying.",
              code: close.code,
              retryable: true,
            });
            backoffRef.current = Math.max(backoffRef.current, QUOTA_BACKOFF_FLOOR_MS);
            break;

          case CloseCode.NORMAL:
            // A `1000` on a live run means the server closed for its own reasons, not
            // that there is nothing left to watch (§3.6).
            if (terminal) {
              setStatus("closed");
              setNextRetryInMs(null);
              return;
            }
            break;

          case CloseCode.INTERNAL_ERROR:
            setError({
              kind: "server",
              message: close.reason || "The stream failed on the server. Reconnecting.",
              code: close.code,
              retryable: true,
            });
            break;

          default:
            // §3.6: anything not enumerated is treated as `1011`. A reconnect MUST NOT
            // clear the panes — the buffers hold what arrived, and replay refills the gap.
            setError({
              kind: close.code === CloseCode.GOING_AWAY ? "network" : "server",
              message:
                close.reason ||
                (close.code === CloseCode.GOING_AWAY
                  ? "The connection went away. Reconnecting."
                  : `The connection closed unexpectedly (${close.code}). Reconnecting.`),
              code: close.code,
              retryable: true,
            });
            break;
        }

        const delay = backoffRef.current;
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS);
        setReconnectAttempts((n) => n + 1);
        reconnectsRef.current += 1;
        schedule(delay);
      };

      socket.onerror = () => {
        // `onclose` always follows and carries the code the policy needs. Handling both
        // would double the backoff on every failure.
      };
    };

    void connect();

    // Captured now, not read in the cleanup: `pendingRef.current` is a plain array that
    // outlives this effect, and reading `.current` at teardown is the pattern the lint
    // rule exists to catch even where — as here — the identity never changes.
    const queue = pendingRef.current;

    return () => {
      alive = false;
      stoppedRef.current = true;
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      if (flushTimerRef.current !== null) {
        clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      queue.length = 0;
      // React 19 Strict Mode double-mounts in development; a leaked socket per mount
      // exhausts the 8-per-run quota after four remounts and then looks like a server bug.
      socketRef.current?.close(CloseCode.NORMAL, "component unmounted");
      socketRef.current = null;
    };
  }, [runId, enabled, effectiveFilter, limits, enqueue, flush, store]);

  // Publish the instrumentation on a slow interval rather than per event: the debug
  // drawer is a diagnostic, and a state update per frame to feed it would be the very
  // thing it exists to detect.
  useEffect(() => {
    const handle = setInterval(() => {
      const samples = latencyRef.current;
      setStats({
        latencyP50: percentile(samples, 50),
        latencyP95: percentile(samples, 95),
        samples: samples.length,
        eventsReceived: receivedRef.current,
        commits: commitsRef.current,
        reconnects: reconnectsRef.current,
      });
    }, 1000);
    return () => clearInterval(handle);
  }, []);

  // ------------------------------------------------------------------------
  //  Commands (§7.5)
  // ------------------------------------------------------------------------

  const sendOverSocket = useCallback((message: Record<string, unknown>): boolean => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(message));
    return true;
  }, []);

  const cancel = useCallback(
    async (reason?: string): Promise<CommandResult> => {
      // Over the socket first: the API publishes onto the run's Redis control channel the
      // worker is already subscribed to, so this reaches it without a round trip through
      // Postgres.
      if (sendOverSocket({ type: "cancel", payload: { reason } })) {
        return { ok: true, via: "socket" };
      }
      try {
        await api.cancelRun(runId, reason);
        return { ok: true, via: "rest" };
      } catch (cause) {
        return { ok: false, via: "rest", error: describe(cause) };
      }
    },
    [runId, sendOverSocket],
  );

  const approve = useCallback(
    async (
      gate: GateName,
      decision: GateDecision,
      notes?: string,
    ): Promise<CommandResult> => {
      const record = (via: "socket" | "rest") =>
        store.getState().recordGateDecision({
          gate,
          decision,
          notes: notes ?? "",
          decidedAt: new Date().toISOString(),
          via,
        });

      if (sendOverSocket({ type: "approve", payload: { gate, decision, notes } })) {
        record("socket");
        return { ok: true, via: "socket" };
      }
      // The socket being mid-reconnect is exactly the moment this matters most: an
      // operator rejecting a `before_sandbox_exec` gate must not have the click silently
      // dropped, which is what a fire-and-forget `send()` does when the socket is closed.
      try {
        await api.approveGate(runId, { gate, decision, notes: notes ?? null });
        record("rest");
        return { ok: true, via: "rest" };
      } catch (cause) {
        return { ok: false, via: "rest", error: describe(cause) };
      }
    },
    [runId, sendOverSocket, store],
  );

  const reconnectNow = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    backoffRef.current = INITIAL_BACKOFF_MS;
    setNextRetryInMs(null);
    // Closing triggers `onclose`, which schedules the next attempt with the backoff we
    // just reset — one code path for reconnecting rather than two that can disagree.
    socketRef.current?.close(CloseCode.NORMAL, "operator requested a reconnect");
  }, []);

  const resync = useCallback(() => {
    // Socket-only: `resync` has no REST equivalent. When closed it is a no-op that
    // reconnects instead, since a reconnect performs a superset of a resync (§7.5).
    if (!sendOverSocket({ type: "resync" })) reconnectNow();
  }, [sendOverSocket, reconnectNow]);

  // ------------------------------------------------------------------------
  //  Return value
  // ------------------------------------------------------------------------

  return useMemo<RunStream>(() => {
    /**
     * Array slices are read on access rather than subscribed.
     *
     * §7.3 returns the full folded projection, and §9.1 budgets one re-render per
     * `token.delta`. Both hold only if reading `stream.consoleLines` does not subscribe
     * the deck to the console. These getters are for imperative use — "download all held
     * lines", the debug drawer, a test — and panes still subscribe to their own slice
     * through `useRunSlice`, which is what actually drives their rendering.
     */
    const snapshot = () => store.getState();

    return {
      store,
      status: view.status,
      connected: view.status === "open",
      lastSeq: view.lastSeq,
      replayed: view.replayed,
      historyComplete: view.historyComplete,
      reconnectAttempts,
      nextRetryInMs,
      effectiveFilter: view.storeFilter,
      error: view.error,

      run: view.run,
      phase: view.phase,
      terminal: view.terminal,
      outcome: view.outcome,
      activeNode: view.activeNode,
      pendingGate: view.pendingGate,
      usage: view.usage,
      budgets: view.budgets,
      droppedEvents: view.droppedEvents,
      droppedConsoleLines: view.droppedConsoleLines,

      get nodeState() {
        return snapshot().nodeState;
      },
      get edgeTraversals() {
        return snapshot().edgeTraversals;
      },
      get timeline() {
        return snapshot().timeline;
      },
      get planSteps() {
        return snapshot().planSteps;
      },
      get criteria() {
        return snapshot().criteria;
      },
      get verdict() {
        return snapshot().verdict;
      },
      get consoleLines() {
        return snapshot().consoleLines;
      },
      get executions() {
        return snapshot().executions;
      },
      get artifacts() {
        return snapshot().artifacts;
      },
      get metrics() {
        return snapshot().metrics;
      },
      get mlflow() {
        return snapshot().mlflow;
      },
      get codeRevisions() {
        return snapshot().codeRevisions;
      },
      get retrievalHits() {
        return snapshot().retrievalHits;
      },

      stats,
      cancel,
      approve,
      resync,
      reconnectNow,
    };
  }, [
    store,
    view,
    reconnectAttempts,
    nextRetryInMs,
    stats,
    cancel,
    approve,
    resync,
    reconnectNow,
  ]);
}

// ------------------------------------------------------------------------------------
//  Helpers
// ------------------------------------------------------------------------------------

/** A bounded reservoir: the newest `LATENCY_RESERVOIR` samples, oldest evicted. */
function sample(reservoir: number[], value: number): void {
  if (!Number.isFinite(value)) return;
  reservoir.push(value);
  if (reservoir.length > LATENCY_RESERVOIR) reservoir.shift();
}

function percentile(samples: readonly number[], p: number): number | null {
  if (samples.length === 0) return null;
  const sorted = [...samples].sort((a, b) => a - b);
  const index = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil((p / 100) * sorted.length) - 1),
  );
  return sorted[index] ?? null;
}

/** A problem detail an operator can act on. A 409 gate that expired needs to say so. */
function describe(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return "The request failed.";
}
