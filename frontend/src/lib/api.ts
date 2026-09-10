/**
 * The REST client. One module, so the base URL and the bearer header are decided once.
 *
 * Everything here is a plain `fetch` wrapper rather than a generated client: the surface
 * the dashboard uses is a dozen endpoints, and TanStack Query already owns caching,
 * retries and invalidation, which is the part a client library would otherwise provide.
 * What the client does *not* own is the types — those come from `rest.ts`, which unpacks
 * the OpenAPI document (docs/FRONTEND.md §4.2), so a renamed response field is a compile
 * error here rather than an `undefined` in a table cell.
 *
 * Only endpoints that exist are declared. §2.4: a view MUST NOT call a non-existent
 * endpoint speculatively, and a client method for one is an invitation to.
 */

import type {
  BenchmarkResultsResponse,
  BenchmarkRunAccepted,
  BenchmarkRunRequest,
  BenchmarkSuiteListResponse,
  CorpusDocumentCreate,
  CorpusDocumentListResponse,
  CorpusDocumentRead,
  CorpusSearchRequest,
  CorpusSearchResponse,
  DeepHealthResponse,
  HealthCheckResponse,
  RunAccepted,
  RunApproveRequest,
  RunEventsResponse,
  RunListResponse,
  RunRead,
  StandardResponse,
  TaskCreate,
  TaskListResponse,
  TaskRead,
  WsTicketResponse,
} from "./rest";

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
    // FastAPI's 422 puts an array of field errors in `detail`; the first one is the one
    // an operator can act on, and the rest are usually the same mistake seen twice.
    if (Array.isArray(text) && text.length > 0) {
      const first = text[0] as { msg?: string; loc?: unknown[] };
      if (typeof first?.msg === "string") {
        const field = Array.isArray(first.loc) ? first.loc.slice(-1)[0] : undefined;
        return field ? `${String(field)}: ${first.msg}` : first.msg;
      }
    }
  }
  return `${response.status} ${response.statusText}`;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

export const api = {
  // ── Health ────────────────────────────────────────────────────────────────
  health: () => request<HealthCheckResponse>("/health"),

  /**
   * The dependency strip's source. Returns `503` with a body when a hard dependency is
   * down, so the caller must read the body rather than treat non-200 as "no data".
   */
  healthDeep: async (): Promise<DeepHealthResponse> => {
    const response = await fetch(`${API_BASE}/api/v1/health/deep`, {
      headers: headers(),
      cache: "no-store",
    });
    // 503 is the *expected* answer when something is unhealthy, and its body is the
    // per-service detail the strip renders. Throwing on it would hide exactly what the
    // caller asked for.
    if (response.status === 503) return (await response.json()) as DeepHealthResponse;
    if (!response.ok) throw new ApiError(response.status, response.statusText);
    return (await response.json()) as DeepHealthResponse;
  },

  // ── Tasks ─────────────────────────────────────────────────────────────────
  listTasks: (skip = 0, limit = 20) =>
    request<TaskListResponse>(`/tasks${query({ skip, limit })}`),

  getTask: (taskId: string) => request<TaskRead>(`/tasks/${taskId}`),

  createTask: (payload: TaskCreate) =>
    request<TaskRead>("/tasks", { method: "POST", body: JSON.stringify(payload) }),

  deleteTask: (taskId: string) =>
    request<StandardResponse>(`/tasks/${taskId}`, { method: "DELETE" }),

  /**
   * Start a run.
   *
   * The idempotency key is the caller's, generated once at click time and held for the
   * mutation's lifetime: a key generated inside this function would be fresh on every
   * React retry, which is the one thing the header exists to prevent (§8.3).
   *
   * §2.3: this endpoint accepts no configuration body today. When B5 lands, the body goes
   * here and `CAPABILITIES.runConfig` flips.
   */
  startRun: (taskId: string, idempotencyKey: string) =>
    request<RunAccepted>(`/tasks/${taskId}/runs`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  listTaskRuns: (taskId: string) =>
    request<RunListResponse>(`/tasks/${taskId}/runs`),

  // ── Runs ──────────────────────────────────────────────────────────────────
  getRun: (runId: string) => request<RunRead>(`/runs/${runId}`),

  /**
   * The same backlog the WebSocket replays, for clients that cannot hold a socket.
   *
   * The response carries `gap` and `oldest_available`; §3.4 requires that a successful
   * backfill with `gap: true` MUST NOT be treated as making history complete.
   */
  getRunEvents: (runId: string, afterSeq = 0, limit = 1000) =>
    request<RunEventsResponse>(
      `/runs/${runId}/events${query({ after_seq: afterSeq, limit })}`,
    ),

  cancelRun: (runId: string, reason?: string) =>
    request<StandardResponse>(`/runs/${runId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),

  resumeRun: (runId: string) =>
    request<StandardResponse>(`/runs/${runId}/resume`, { method: "POST" }),

  approveGate: (runId: string, payload: RunApproveRequest) =>
    request<StandardResponse>(`/runs/${runId}/approve`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // ── Corpus ────────────────────────────────────────────────────────────────
  listDocuments: (p: { skip?: number; limit?: number; collection?: string } = {}) =>
    request<CorpusDocumentListResponse>(
      `/corpus/documents${query({
        skip: p.skip ?? 0,
        limit: p.limit ?? 20,
        collection: p.collection,
      })}`,
    ),

  ingestDocument: (payload: CorpusDocumentCreate) =>
    request<CorpusDocumentRead>("/corpus/documents", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  deleteDocument: (docId: string) =>
    request<StandardResponse>(`/corpus/documents/${docId}`, { method: "DELETE" }),

  searchCorpus: (payload: CorpusSearchRequest) =>
    request<CorpusSearchResponse>("/corpus/search", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // ── Benchmarks ────────────────────────────────────────────────────────────
  listBenchmarks: () => request<BenchmarkSuiteListResponse>("/benchmarks"),

  benchmarkResults: (suite: string) =>
    request<BenchmarkResultsResponse>(`/benchmarks/${suite}/results`),

  runBenchmarkSuite: (suite: string, payload: BenchmarkRunRequest = { cases: null }) =>
    request<BenchmarkRunAccepted>(`/benchmarks/${suite}/run`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // ── Protocol ──────────────────────────────────────────────────────────────
  /**
   * Mint a WebSocket ticket (§9.3).
   *
   * Called on every connect *and* every reconnect, because a ticket is single-use: the
   * server consumes it with `GETDEL` at accept time, so a cached one would work exactly
   * once and then fail every retry in the backoff loop — a failure that looks exactly
   * like a server outage.
   */
  wsTicket: (runId: string) =>
    request<WsTicketResponse>("/ws/tickets", {
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

/**
 * A run's terminal states, as the durable column reports them.
 *
 * The socket must not reconnect after one of these on a `1000` close (§3.6), and the
 * `run` query switches to `staleTime: Infinity` once one holds.
 */
export function isTerminalStatus(status: string | null | undefined): boolean {
  return status === "COMPLETED" || status === "FAILED" || status === "CANCELLED";
}
