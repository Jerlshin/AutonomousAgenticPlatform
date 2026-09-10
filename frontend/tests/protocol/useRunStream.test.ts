/**
 * The §3.8 protocol conformance checklist, one test per line.
 *
 * docs/FRONTEND.md §13: "This is the highest-value suite in the app: reconnection bugs
 * are invisible in development and constant in use." Each `describe` below names the
 * checklist item it discharges, so a reviewer can map the list to the file and back.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  frames,
  installMockSocket,
  latestSocket,
  MockWebSocket,
} from "./mockSocket";

const ticketMint = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      getRun: vi.fn(async () => {
        throw new Error("no REST in the protocol suite");
      }),
      wsTicket: vi.fn(async (runId: string) => {
        ticketMint(runId);
        return {
          ticket: `ticket-${ticketMint.mock.calls.length}`,
          run_id: runId,
          expires_in: 60,
          ws_url: "",
        };
      }),
      cancelRun: vi.fn(async () => ({ success: true, message: "ok", data: null })),
      approveGate: vi.fn(async () => ({ success: true, message: "ok", data: null })),
    },
  };
});

const { useRunStream, CONTROL_FLOOR } = await import("@/hooks/useRunStream");

const RUN = frames.runId;

let restore: () => void;

beforeEach(() => {
  vi.useFakeTimers();
  ticketMint.mockClear();
  restore = installMockSocket();
});

afterEach(() => {
  restore();
  vi.useRealTimers();
});

/** Mount the hook and let the ticket promise settle so a socket exists. */
async function mount(options: Parameters<typeof useRunStream>[1] = {}) {
  const view = renderHook(() => useRunStream(RUN, options));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  return view;
}

/** Let the rAF batcher run and the store commit. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20);
  });
}

// ------------------------------------------------------------------------------------

describe("§3.8 — subprotocol pluton.v1 is requested on connect", () => {
  it("passes the protocol in the constructor", async () => {
    const view = await mount();
    expect(latestSocket().protocols).toEqual(["pluton.v1"]);
    view.unmount();
  });
});

describe("§3.8 — v !== 1 closes with 4400 and does not retry", () => {
  it("closes and stops", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());

    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({ v: 2, seq: 1, run_id: RUN, ts: "", type: "run.started", payload: {} }),
      } as MessageEvent);
    });

    expect(socket.closedByClient?.code).toBe(4400);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    // One socket, ever: a malformed client cannot be fixed by trying again.
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(view.result.current.error?.kind).toBe("protocol");
    expect(view.result.current.error?.retryable).toBe(false);
    view.unmount();
  });
});

describe("§3.8 — the cursor advances only on seq > 0, monotonically", () => {
  it("ignores control frames and out-of-order forwards", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());

    act(() => {
      socket.deliver(frames.hello(0));
      socket.deliver(frames.nodeStarted(7, "planner"));
      socket.deliver(frames.ping());
      socket.deliver(frames.replayComplete(7));
      // A stale forward. `seq` is authoritative, arrival order is not.
      socket.deliver(frames.nodeStarted(3, "planner"));
    });
    await settle();

    // Neither the `hello`, the `ping` nor the `replay.complete` moved it — they carry
    // `seq: 0`, and treating one as progress would skip real history on the next
    // reconnect. Nor did the stale `seq: 3`.
    expect(view.result.current.lastSeq).toBe(7);
    // The stale forward was not folded either: one `planner` visit, not two.
    expect(view.result.current.store.getState().timeline).toHaveLength(1);
    view.unmount();
  });

  it("resumes the next attempt from the highest seq seen", async () => {
    const view = await mount();
    act(() => latestSocket().open());
    act(() => {
      latestSocket().deliver(frames.nodeStarted(12, "coder"));
      latestSocket().deliver(frames.ping());
    });
    await settle();

    act(() => latestSocket().serverClose(1011, "internal"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(MockWebSocket.instances).toHaveLength(2);
    expect(latestSocket().afterSeq).toBe(12);
    view.unmount();
  });
});

describe("§3.8 — a fresh ticket is minted per attempt; failure does not abort", () => {
  it("mints once per connect and never reuses one", async () => {
    const view = await mount();
    expect(ticketMint).toHaveBeenCalledTimes(1);
    const first = latestSocket().ticket;

    act(() => latestSocket().open());
    act(() => latestSocket().serverClose(1001, "going away"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(ticketMint).toHaveBeenCalledTimes(2);
    expect(latestSocket().ticket).not.toBe(first);
    view.unmount();
  });

  it("connects without a ticket when minting fails", async () => {
    const api = await import("@/lib/api");
    vi.mocked(api.api.wsTicket).mockRejectedValueOnce(new Error("no token on this box"));

    const view = await mount();
    // A loopback development box needs no ticket; failing hard would make the dashboard
    // useless exactly where it is used most.
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latestSocket().ticket).toBeNull();
    view.unmount();
  });
});

describe("§3.8 — ping is answered with pong synchronously", () => {
  it("sends pong in the same tick, without waiting for a frame", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());

    act(() => socket.deliver(frames.ping()));
    // No timer advance: deferring the pong into the batcher risks crossing the 40 s
    // missed-pong window on a loaded tab.
    expect(socket.frames.map((frame) => frame.type)).toContain("pong");
    view.unmount();
  });
});

describe("§3.8 — any subscribe includes the control floor", () => {
  const NARROW = ["run.", "node.started"] as const;

  it("unions the caller's filter with ping, hello, error, replay. and run.snapshot", async () => {
    const view = await mount({ types: NARROW });
    const socket = latestSocket();
    act(() => socket.open());

    const subscribe = socket.frames.find((frame) => frame.type === "subscribe");
    expect(subscribe).toBeDefined();
    const types = subscribe?.payload?.types as string[];
    for (const entry of CONTROL_FLOOR) expect(types).toContain(entry);
    for (const entry of NARROW) expect(types).toContain(entry);
    view.unmount();
  });

  it("sends no subscribe at all when no filter was requested", async () => {
    const view = await mount();
    act(() => latestSocket().open());
    expect(latestSocket().frames.some((frame) => frame.type === "subscribe")).toBe(false);
    view.unmount();
  });

  it("exposes the effective filter it sent", async () => {
    const view = await mount({ types: NARROW });
    act(() => latestSocket().open());
    await settle();
    expect(view.result.current.effectiveFilter).toEqual(
      expect.arrayContaining([...NARROW, ...CONTROL_FLOOR]),
    );
    view.unmount();
  });
});

describe("§3.8 — replay.gap resets the cursor and marks history incomplete", () => {
  it("resumes from 0 and records what was lost", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());

    act(() => {
      socket.deliver(frames.nodeStarted(40, "coder"));
      socket.deliver(frames.replayGap(40, 120));
    });
    await settle();

    expect(view.result.current.historyComplete).toBe(false);
    expect(view.result.current.store.getState().oldestAvailable).toBe(120);

    act(() => socket.serverClose(1011));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    // The whole retained window, not the part after a cursor the server can no longer
    // honour.
    expect(latestSocket().afterSeq).toBe(0);
    view.unmount();
  });
});

describe("§3.8 — backoff is 500 ms → ×2 → 30 s with jitter, reset on open", () => {
  it("doubles per failure and resets once a socket opens", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0); // remove the jitter from the assertion
    const view = await mount();

    const delays: number[] = [];
    for (let attempt = 0; attempt < 4; attempt++) {
      const before = MockWebSocket.instances.length;
      act(() => latestSocket().serverClose(1011));
      // Walk forward until the next socket appears, recording how long it took.
      let waited = 0;
      while (MockWebSocket.instances.length === before && waited < 60_000) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
        });
        waited += 50;
      }
      delays.push(waited);
    }

    expect(delays[0]).toBeLessThanOrEqual(550);
    expect(delays[1]).toBeGreaterThan(delays[0]!);
    expect(delays[2]).toBeGreaterThan(delays[1]!);

    // A successful open puts the ceiling back on the floor.
    act(() => latestSocket().open());
    const before = MockWebSocket.instances.length;
    act(() => latestSocket().serverClose(1011));
    let waited = 0;
    while (MockWebSocket.instances.length === before && waited < 60_000) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      waited += 50;
    }
    expect(waited).toBeLessThanOrEqual(550);
    view.unmount();
  });

  it("never exceeds the 30 s ceiling", async () => {
    vi.spyOn(Math, "random").mockReturnValue(1);
    const view = await mount();
    for (let attempt = 0; attempt < 12; attempt++) {
      act(() => latestSocket().serverClose(1011));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(40_000);
      });
    }
    // 30 s + 250 ms of jitter is the ceiling; the header renders this number.
    expect(view.result.current.nextRetryInMs ?? 0).toBeLessThanOrEqual(30_250);
    view.unmount();
  });
});

describe("§3.8 — close-code policy", () => {
  it.each([
    [4400, "protocol"],
    [4403, "forbidden"],
    [4404, "not_found"],
  ])("stops on %i with a %s error", async (code, kind) => {
    const view = await mount();
    act(() => latestSocket().open());
    act(() => latestSocket().serverClose(code));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(view.result.current.error?.kind).toBe(kind);
    expect(view.result.current.error?.retryable).toBe(false);
    view.unmount();
  });

  it("re-mints immediately on 4401 and stops after three", async () => {
    const view = await mount();
    for (let attempt = 0; attempt < 3; attempt++) {
      act(() => latestSocket().open());
      act(() => latestSocket().serverClose(4401, "ticket expired"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10);
      });
    }
    // Two immediate re-mints, then a stop. Retrying a rejected credential forever is a
    // busy loop whose user-visible symptom is "nothing happens".
    expect(MockWebSocket.instances).toHaveLength(3);
    expect(view.result.current.error?.kind).toBe("auth");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockWebSocket.instances).toHaveLength(3);
    view.unmount();
  });

  it("backs off from a 5 s floor on 4429", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    const view = await mount();
    act(() => latestSocket().open());
    act(() => latestSocket().serverClose(4429, "quota"));
    await settle();

    expect(view.result.current.error?.kind).toBe("quota");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    // Still one socket: hammering at 500 ms guarantees this tab keeps losing the race.
    expect(MockWebSocket.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    view.unmount();
  });

  it("stops on 1000 only when the run is terminal", async () => {
    const view = await mount();
    act(() => latestSocket().open());
    act(() => latestSocket().serverClose(1000, "server closed"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    // A `1000` on a live run means the server closed for its own reasons.
    expect(MockWebSocket.instances).toHaveLength(2);

    act(() => latestSocket().open());
    act(() => latestSocket().deliver(frames.cancelled(9)));
    await settle();
    act(() => latestSocket().serverClose(1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    view.unmount();
  });
});

describe("§3.8 — unmount clears timers and closes the socket", () => {
  it("closes with 1000 and schedules nothing further", async () => {
    const view = await mount();
    act(() => latestSocket().open());
    const socket = latestSocket();

    view.unmount();
    expect(socket.closedByClient?.code).toBe(1000);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("leaks no socket across a Strict Mode style double mount", async () => {
    const first = await mount();
    first.unmount();
    const second = await mount();
    // Two constructed, at most one still open — four remounts otherwise exhaust the
    // 8-per-run quota and then look like a server bug.
    const open = MockWebSocket.instances.filter(
      (socket) => socket.readyState !== MockWebSocket.CLOSED,
    );
    expect(open.length).toBeLessThanOrEqual(1);
    second.unmount();
  });
});

describe("§3.8 — every client message is a JSON object with a type", () => {
  it("sends only JSON strings, never binary", async () => {
    const view = await mount({ types: ["run."] });
    const socket = latestSocket();
    act(() => socket.open());
    act(() => socket.deliver(frames.ping()));
    await act(async () => {
      await view.result.current.cancel("stop");
    });
    act(() => {
      view.result.current.resync();
    });

    expect(socket.sent.length).toBeGreaterThan(0);
    for (const raw of socket.sent) {
      expect(typeof raw).toBe("string");
      const parsed = JSON.parse(raw) as { type?: unknown };
      expect(typeof parsed.type).toBe("string");
    }
    view.unmount();
  });

  it("survives a frame that is not JSON at all", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());
    act(() => socket.deliverRaw("<html>a proxy error page</html>"));
    await settle();
    // I7: the hook never throws; a garbage frame is dropped, not fatal.
    expect(view.result.current.error).toBeNull();
    expect(socket.closedByClient).toBeNull();
    view.unmount();
  });
});

describe("§7.5 — commands fall back to REST when the socket is closed", () => {
  it("cancels over the socket when open", async () => {
    const view = await mount();
    act(() => latestSocket().open());
    let result: Awaited<ReturnType<typeof view.result.current.cancel>>;
    await act(async () => {
      result = await view.result.current.cancel("operator");
    });
    expect(result!.via).toBe("socket");
    view.unmount();
  });

  it("cancels over REST when the socket is not open", async () => {
    const api = await import("@/lib/api");
    const view = await mount();
    // Never opened: the socket is CONNECTING, which is exactly the mid-reconnect case.
    let result: Awaited<ReturnType<typeof view.result.current.cancel>>;
    await act(async () => {
      result = await view.result.current.cancel("operator");
    });
    expect(result!.via).toBe("rest");
    expect(api.api.cancelRun).toHaveBeenCalledWith(RUN, "operator");
    view.unmount();
  });

  it("reports a rejected approval instead of dropping the click", async () => {
    const api = await import("@/lib/api");
    vi.mocked(api.api.approveGate).mockRejectedValueOnce(
      new api.ApiError(409, "this gate has expired"),
    );
    const view = await mount();
    let result: Awaited<ReturnType<typeof view.result.current.approve>>;
    await act(async () => {
      result = await view.result.current.approve("after_plan", "approve");
    });
    expect(result!.ok).toBe(false);
    expect(result!.error).toBe("this gate has expired");
    view.unmount();
  });

  it("records a delivered decision in the gate history", async () => {
    const view = await mount();
    act(() => latestSocket().open());
    await act(async () => {
      await view.result.current.approve("before_sandbox_exec", "reject", "unsafe import");
    });
    const history = view.result.current.store.getState().gateHistory;
    expect(history).toHaveLength(1);
    expect(history[0]).toMatchObject({
      gate: "before_sandbox_exec",
      decision: "reject",
      via: "socket",
    });
    view.unmount();
  });
});

describe("§6.4 — store commits are batched by animation frame", () => {
  it("folds a burst of two hundred frames in a single commit", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());

    const before = view.result.current.stats.commits;
    act(() => {
      for (let seq = 1; seq <= 200; seq++) socket.deliver(frames.stdout(seq, `line ${seq}`));
    });
    // Nothing has reached the store yet — the queue is waiting for a frame.
    expect(view.result.current.store.getState().consoleLines).toHaveLength(0);
    await settle();

    expect(view.result.current.store.getState().consoleLines).toHaveLength(200);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(view.result.current.stats.commits - before).toBe(1);
    view.unmount();
  });

  it("takes the immediate path for a gate, which is an operator-attention event", async () => {
    const view = await mount();
    const socket = latestSocket();
    act(() => socket.open());

    act(() =>
      socket.deliver({
        v: 1,
        seq: 5,
        run_id: RUN,
        ts: new Date().toISOString(),
        type: "interrupt.requested",
        payload: {
          gate: "before_sandbox_exec",
          prompt: "review this code",
          options: ["approve", "reject"],
          expires_at: "",
          context: {},
        },
      }),
    );
    // No frame advance: a gate notification must not wait on a repaint.
    expect(view.result.current.store.getState().pendingGate?.gate).toBe(
      "before_sandbox_exec",
    );
    view.unmount();
  });
});
