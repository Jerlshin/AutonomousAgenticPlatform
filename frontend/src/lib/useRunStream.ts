"use client";

/**
 * `useRunStream` — the only place in this app that touches a `WebSocket` (§18.4).
 *
 * It owns ticket acquisition, the normative reconnection algorithm in §9.8, sequence
 * tracking and gap recovery. Every pane reads the store this fills; none of them knows a
 * socket exists.
 *
 * The reconnection algorithm, verbatim from §9.8:
 *
 * ```
 * last_seq ← 0 ; backoff ← 500 ms
 * loop:
 *     ticket ← POST /api/v1/ws/tickets {run_id}
 *     ws     ← connect(/api/v1/ws/runs/{run_id}?ticket=…&after_seq=last_seq)
 *     on open:      backoff ← 500 ms
 *     on message m: if m.seq > 0 then last_seq ← m.seq
 *     on close c:   if c ∈ {1000} and run is terminal then exit
 *                   if c ∈ {4400, 4403, 4404} then exit with error
 *                   sleep(backoff + jitter) ; backoff ← min(backoff × 2, 30 s)
 * ```
 *
 * Two details in it are easy to get wrong and expensive when you do. The cursor advances
 * only on `seq > 0`, because control frames carry `seq: 0` and treating a `ping` as
 * progress would skip real history on the next reconnect. And the ticket is re-minted on
 * every attempt, because the server consumes it at accept time — a cached ticket works
 * once and then fails every retry in the backoff loop, which looks exactly like a server
 * that is down.
 */

import { useCallback, useEffect, useRef } from "react";
import { api, runSocketUrl } from "./api";
import { PROTOCOL, type RunEvent } from "./events.generated";
import { useRunStore, type StreamStatus } from "./runStore";
import type { ArtifactRow, ConsoleLine, CriterionRow, RunDetail, TimelineEntry } from "./types";

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 30_000;
const JITTER_MS = 250;

/** §9.7: codes that mean "the client is wrong", so retrying cannot help. */
const FATAL_CLOSE_CODES = new Set([4400, 4403, 4404]);

export interface RunStream {
  run: RunDetail | null;
  events: RunEvent[];
  consoleLines: ConsoleLine[];
  timeline: TimelineEntry[];
  criteria: CriterionRow[];
  artifacts: ArtifactRow[];
  status: StreamStatus;
  lastSeq: number;
  activeNode: string | null;
  visitedNodes: string[];
  failedNodes: string[];
  terminal: boolean;
  error: string | null;
  cancel: (reason?: string) => void;
  approve: (gate: string, decision: "approve" | "reject", notes?: string) => void;
  resync: () => void;
}

export interface UseRunStreamOptions {
  /** Server-side `subscribe` filter (§9.5). Omit to receive everything. */
  types?: string[];
  /** Set false to leave the socket closed — the report view reads REST only. */
  enabled?: boolean;
}

export function useRunStream(
  runId: string,
  options: UseRunStreamOptions = {},
): RunStream {
  const { types, enabled = true } = options;

  const socketRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The cursor is a ref, not store state: the reconnect path reads it from inside a
  // closure that must not be re-created on every event, and a `lastSeq` from a stale
  // render would replay history the client already has.
  const cursorRef = useRef(0);
  const stoppedRef = useRef(false);

  const store = useRunStore();
  const { reset, setStatus, setRun, ingest } = store;

  const send = useCallback((message: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  }, []);

  useEffect(() => {
    if (!runId || !enabled) return;

    stoppedRef.current = false;
    cursorRef.current = 0;
    backoffRef.current = INITIAL_BACKOFF_MS;
    reset(runId);

    // The REST read is not redundant with `hello`. It gives the header a title, a prompt
    // and a status before the first frame arrives, which matters most on the slow path —
    // a queued run has nothing in its stream but `run.queued`.
    api
      .getRun(runId)
      .then((run) => {
        setRun(run);
        // A run that finished before this page opened still has its whole history in the
        // stream, so the cursor stays at 0 and the replay rebuilds everything.
      })
      .catch(() => undefined);

    const connect = async (): Promise<void> => {
      if (stoppedRef.current) return;
      setStatus(cursorRef.current > 0 ? "reconnecting" : "connecting");

      let ticket: string | undefined;
      try {
        // Best-effort: a token-less development box needs no ticket, and failing the
        // whole connection because ticket minting 404'd would make the dashboard useless
        // exactly where it is most used.
        ticket = (await api.wsTicket(runId)).ticket;
      } catch {
        ticket = undefined;
      }
      if (stoppedRef.current) return;

      const socket = new WebSocket(
        runSocketUrl(runId, { afterSeq: cursorRef.current, ticket }),
        [PROTOCOL],
      );
      socketRef.current = socket;

      socket.onopen = () => {
        backoffRef.current = INITIAL_BACKOFF_MS;
        setStatus("open");
        if (types?.length) send({ type: "subscribe", payload: { types } });
      };

      socket.onmessage = (message) => {
        let event: RunEvent;
        try {
          event = JSON.parse(message.data as string) as RunEvent;
        } catch {
          return;
        }
        // §9.2: a client seeing `v != 1` must close with 4400 rather than guess.
        if (event.v !== 1) {
          socket.close(4400, "unsupported protocol version");
          return;
        }
        if (event.type === "ping") {
          send({ type: "pong" });
          return;
        }
        if (event.seq > 0) cursorRef.current = event.seq;
        if (event.type === "replay.gap") {
          // The server follows this with a `run.snapshot`; the store rebuilds from that
          // rather than from history it can no longer be given.
          cursorRef.current = 0;
        }
        ingest(event);
      };

      socket.onclose = (close) => {
        socketRef.current = null;
        if (stoppedRef.current) return;

        const terminal = useRunStore.getState().terminal;
        if (FATAL_CLOSE_CODES.has(close.code)) {
          setStatus("closed");
          return;
        }
        if (close.code === 1000 && terminal) {
          setStatus("closed");
          return;
        }

        const delay = backoffRef.current + Math.random() * JITTER_MS;
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS);
        setStatus("reconnecting");
        retryRef.current = setTimeout(() => void connect(), delay);
      };

      socket.onerror = () => {
        // `onclose` always follows, and it carries the code the reconnect policy needs.
        // Handling both would double the backoff on every failure.
      };
    };

    void connect();

    return () => {
      stoppedRef.current = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      socketRef.current?.close(1000, "component unmounted");
      socketRef.current = null;
    };
    // `types` is compared by identity; callers pass a module-level constant.
  }, [runId, enabled, types, reset, setStatus, setRun, ingest, send]);

  const cancel = useCallback(
    (reason?: string) => {
      // Over the socket, not REST: the worker is subscribed to the control channel the
      // server publishes on, so this reaches it without a round trip through Postgres.
      send({ type: "cancel", payload: { reason } });
    },
    [send],
  );

  const approve = useCallback(
    (gate: string, decision: "approve" | "reject", notes?: string) => {
      send({ type: "approve", payload: { gate, decision, notes } });
    },
    [send],
  );

  const resync = useCallback(() => send({ type: "resync" }), [send]);

  return {
    run: store.run,
    events: store.events,
    consoleLines: store.consoleLines,
    timeline: store.timeline,
    criteria: store.criteria,
    artifacts: store.artifacts,
    status: store.status,
    lastSeq: store.lastSeq,
    activeNode: store.activeNode,
    visitedNodes: store.visitedNodes,
    failedNodes: store.failedNodes,
    terminal: store.terminal,
    error: store.error,
    cancel,
    approve,
    resync,
  };
}
