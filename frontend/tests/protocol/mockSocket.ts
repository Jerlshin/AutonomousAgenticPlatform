/**
 * A `WebSocket` test double, and the frame helpers the protocol suite drives it with.
 *
 * docs/FRONTEND.md §13 calls the protocol suite the highest-value suite in the app, for a
 * reason worth restating here: reconnection bugs are invisible in development — a
 * loopback socket never drops — and constant in use. The only way to assert against a
 * backoff schedule, a close-code branch or a replay gap is to own both ends of the wire.
 *
 * The double records everything a test needs to assert on: the URL (which carries
 * `after_seq` and the ticket), the requested subprotocols, and every frame the client
 * sent, so "did it answer the ping" and "did the subscribe include the control floor" are
 * questions with exact answers.
 */

import { vi } from "vitest";
import type { RunEvent } from "@/lib/events.generated";

export interface SentFrame {
  type: string;
  payload?: Record<string, unknown>;
}

export class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  /** Every instance ever constructed, in order. Reconnects append. */
  static instances: MockWebSocket[] = [];

  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;

  /** Raw strings the client sent, in order. Binary sends throw (§3.8). */
  readonly sent: string[] = [];
  /** The same frames parsed, for readable assertions. */
  readonly frames: SentFrame[] = [];

  /** How the client closed, when it was the client that closed. */
  closedByClient: { code: number; reason: string } | null = null;

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    MockWebSocket.instances.push(this);
  }

  // ---- the client's half -------------------------------------------------

  send(data: unknown): void {
    if (typeof data !== "string") {
      throw new Error(
        "the client sent a non-string frame; pluton.v1 is UTF-8 JSON only (§3.8)",
      );
    }
    this.sent.push(data);
    this.frames.push(JSON.parse(data) as SentFrame);
  }

  close(code = 1000, reason = ""): void {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.closedByClient = { code, reason };
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, reason, wasClean: true } as CloseEvent);
  }

  // ---- the server's half, driven by the test -----------------------------

  /** Accept the connection. */
  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  /** Deliver one frame. */
  deliver(event: unknown): void {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
  }

  /** Deliver a frame that is not JSON at all, to prove the client survives it. */
  deliverRaw(data: string): void {
    this.onmessage?.({ data } as MessageEvent);
  }

  /** Close from the server's side with a §9.7 code. */
  serverClose(code: number, reason = ""): void {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, reason, wasClean: code === 1000 } as CloseEvent);
  }

  /** The `after_seq` this attempt resumed from — the resume mechanism, made assertable. */
  get afterSeq(): number {
    return Number(new URL(this.url).searchParams.get("after_seq") ?? 0);
  }

  get ticket(): string | null {
    return new URL(this.url).searchParams.get("ticket");
  }
}

/** Install the double as the global `WebSocket`. Returns a restore function. */
export function installMockSocket(): () => void {
  const original = globalThis.WebSocket;
  MockWebSocket.instances = [];
  // The double implements the four handlers and the two methods the hook uses; the DOM's
  // full interface is not worth reproducing to satisfy a cast.
  globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket;
  return () => {
    globalThis.WebSocket = original;
    MockWebSocket.instances = [];
  };
}

/** The most recently constructed socket — the one a reconnect just opened. */
export function latestSocket(): MockWebSocket {
  const socket = MockWebSocket.instances.at(-1);
  if (!socket) throw new Error("no WebSocket was constructed");
  return socket;
}

// ------------------------------------------------------------------------------------
//  Frame builders
// ------------------------------------------------------------------------------------

const RUN_ID = "11111111-2222-3333-4444-555555555555";

export function envelope<T extends RunEvent["type"]>(
  type: T,
  seq: number,
  payload: Extract<RunEvent, { type: T }>["payload"],
  ts = new Date().toISOString(),
): RunEvent {
  return { v: 1, seq, run_id: RUN_ID, ts, type, payload } as RunEvent;
}

export const frames = {
  runId: RUN_ID,

  hello: (lastSeq = 0) =>
    envelope("hello", 0, {
      protocol: "pluton.v1",
      run: {},
      last_seq: lastSeq,
      heartbeat_s: 20,
    }),

  ping: () => envelope("ping", 0, {}),

  replayComplete: (through: number) => envelope("replay.complete", 0, { through_seq: through }),

  replayGap: (requestedAfter: number, oldestAvailable: number) =>
    envelope("replay.gap", 0, {
      requested_after: requestedAfter,
      oldest_available: oldestAvailable,
    }),

  nodeStarted: (seq: number, node: string, phase = "IMPLEMENT") =>
    envelope("node.started", seq, {
      node,
      agent: node,
      phase,
      model: null,
      plan_step_id: null,
      step_seq: null,
    }),

  nodeCompleted: (
    seq: number,
    node: string,
    extra: Partial<Extract<RunEvent, { type: "node.completed" }>["payload"]> = {},
  ) =>
    envelope("node.completed", seq, {
      node,
      duration_ms: 1000,
      tokens_in: 0,
      tokens_out: 0,
      llm_calls: 0,
      degraded: false,
      summary: "",
      step_seq: null,
      ...extra,
    }),

  stdout: (seq: number, line: string, executionId = "e1") =>
    envelope("sandbox.stdout", seq, { execution_id: executionId, line, ts: "" }),

  token: (seq: number, node: string, text: string) =>
    envelope("token.delta", seq, { node, text }),

  cancelled: (seq: number) =>
    envelope("run.cancelled", seq, {
      status: "CANCELLED",
      reason: "cancelled from the dashboard",
      cancelled_by: "operator",
      deliverables: [],
      bundle_url: null,
      evaluation: null,
      mlflow: null,
      usage: null,
    }),
};

/** A stub for `@/lib/api` that records ticket mints, for the "fresh ticket" assertions. */
export function makeApiStub() {
  const wsTicket = vi.fn(async (runId: string) => ({
    ticket: `ticket-${wsTicket.mock.calls.length}`,
    run_id: runId,
    expires_in: 60,
    ws_url: "",
  }));
  return {
    wsTicket,
    getRun: vi.fn(async () => null),
    cancelRun: vi.fn(async () => ({ success: true, message: "ok", data: null })),
    approveGate: vi.fn(async () => ({ success: true, message: "ok", data: null })),
  };
}
