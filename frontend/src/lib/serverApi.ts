/**
 * The server-side REST client (§2.2, §12.3).
 *
 * Separate from `api.ts` for one reason, and it is a security reason rather than a
 * structural one: `api.ts` authenticates with `NEXT_PUBLIC_API_TOKEN`, which is compiled
 * into the browser bundle, and §2.2 forbids a Server Component from reading authenticated
 * REST with it. Server-side reads authenticate with `PLATFORM_API_TOKEN`, which has no
 * `NEXT_PUBLIC_` prefix and therefore never leaves the server.
 *
 * The guard below is what keeps that from being a comment nobody reads. Next.js inlines
 * only `NEXT_PUBLIC_*` variables, so importing this module into a Client Component would
 * not leak the token — it would silently send **unauthenticated** requests, which fails
 * as a puzzling 401 far from its cause. Failing loudly at the import instead costs
 * nothing and needs no dependency: the `server-only` package would do this at build time,
 * but §13 forbids an unbudgeted dependency for a check that is one line here.
 *
 * This is deliberately not a mirror of the whole `api` surface. Only the reads a Server
 * Component actually performs belong here; everything else stays client-side where the
 * cache, retries and live invalidation already live.
 */

import type { RunRead } from "./rest";

if (typeof window !== "undefined") {
  throw new Error(
    "serverApi.ts was imported from client code. It authenticates with the server-only " +
      "PLATFORM_API_TOKEN, which is undefined in the browser; use lib/api.ts instead.",
  );
}

const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/$/, "");

const TOKEN = process.env.PLATFORM_API_TOKEN ?? "";

export type ServerFetch<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string };

/**
 * A fetch that resolves to a result rather than throwing.
 *
 * Every caller here renders a page whose whole job is to explain what the platform does
 * and does not hold, so "the API answered 404" is content, not an exception — throwing
 * would replace a page that says what is missing with a generic error boundary that does
 * not.
 */
export async function serverRequest<T>(path: string): Promise<ServerFetch<T>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1${path}`, {
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : undefined,
      cache: "no-store",
    });
  } catch {
    return {
      ok: false,
      status: 0,
      error: `The API at ${API_BASE} could not be reached.`,
    };
  }

  if (!response.ok) {
    return {
      ok: false,
      status: response.status,
      error: `The API answered ${response.status}.`,
    };
  }

  try {
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, status: response.status, error: "The API returned malformed JSON." };
  }
}

/** Plain text, for `GET /runs/{id}/report` once B4 lands. */
export async function serverText(path: string): Promise<ServerFetch<string>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1${path}`, {
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : undefined,
      cache: "no-store",
    });
  } catch {
    return { ok: false, status: 0, error: `The API at ${API_BASE} could not be reached.` };
  }
  if (!response.ok) {
    return { ok: false, status: response.status, error: `The API answered ${response.status}.` };
  }
  return { ok: true, data: await response.text() };
}

export function getRunServerSide(runId: string): Promise<ServerFetch<RunRead>> {
  return serverRequest<RunRead>(`/runs/${runId}`);
}
