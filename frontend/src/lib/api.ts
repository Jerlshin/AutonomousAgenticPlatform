/**
 * The REST client. One module, so the base URL and the bearer header are decided once.
 *
 * Everything here is a plain `fetch` wrapper rather than a generated client: the surface
 * the dashboard uses is a dozen endpoints, and TanStack Query already owns caching,
 * retries and invalidation, which is the part a client library would otherwise provide.
 */

import type {
  RunAccepted,
  RunDetail,
  TaskListResponse,
  TaskSummary,
  WsTicket,
} from "./types";

export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/$/, "");

const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";

/** An API error carrying the status, so callers can branch on 404 and 409 (§8.1). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function headers(extra?: HeadersInit): HeadersInit {
  const base: Record<string, string> = { "Content-Type": "application/json" };
  if (API_TOKEN) base.Authorization = `Bearer ${API_TOKEN}`;
  return { ...base, ...(extra as Record<string, string>) };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}/api/v1${path}`, {
    ...init,
    headers: headers(init?.headers),
    cache: "no-store",
  });

  if (!response.ok) {
    // RFC 9457 problem bodies and FastAPI's `{detail}` both land here; neither is
    // guaranteed to be JSON when a proxy is in the way, so the parse is best-effort.
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text().catch(() => undefined);
    }
    throw new ApiError(response.status, messageFrom(detail, response), detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function messageFrom(detail: unknown, response: Response): string {
  if (detail && typeof detail === "object") {
    const body = detail as Record<string, unknown>;
    const text = body.detail ?? body.title ?? body.message;
    if (typeof text === "string") return text;
  }
  return `${response.status} ${response.statusText}`;
}

export const api = {
  listTasks: (skip = 0, limit = 20) =>
    request<TaskListResponse>(`/tasks?skip=${skip}&limit=${limit}`),

  getTask: (taskId: string) => request<TaskSummary>(`/tasks/${taskId}`),

  createTask: (payload: { title: string; prompt: string }) =>
    request<TaskSummary>("/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  startRun: (taskId: string, idempotencyKey?: string) =>
    request<RunAccepted>(`/tasks/${taskId}/runs`, {
      method: "POST",
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    }),

  getRun: (runId: string) => request<RunDetail>(`/runs/${runId}`),

  cancelRun: (runId: string, reason?: string) =>
    request<{ message: string }>(`/runs/${runId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  resumeRun: (runId: string) =>
    request<{ message: string }>(`/runs/${runId}/resume`, { method: "POST" }),

  approveGate: (
    runId: string,
    gate: string,
    decision: "approve" | "reject",
    notes?: string,
  ) =>
    request<{ message: string }>(`/runs/${runId}/approve`, {
      method: "POST",
      body: JSON.stringify({ gate, decision, notes }),
    }),

  /**
   * Mint a WebSocket ticket (§9.3).
   *
   * Called on every connect *and* every reconnect, because a ticket is single-use: the
   * server consumes it at accept time, so a cached one would work exactly once and then
   * fail every retry in the backoff loop.
   */
  wsTicket: (runId: string) =>
    request<WsTicket>("/ws/tickets", {
      method: "POST",
      body: JSON.stringify({ run_id: runId }),
    }),
};

/** The absolute `ws://` URL for a run, with the resume cursor and ticket attached. */
export function runSocketUrl(
  runId: string,
  options: { afterSeq?: number; ticket?: string } = {},
): string {
  const url = new URL(`${API_BASE}/api/v1/ws/runs/${runId}`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (options.afterSeq) url.searchParams.set("after_seq", String(options.afterSeq));
  if (options.ticket) url.searchParams.set("ticket", options.ticket);
  return url.toString();
}
