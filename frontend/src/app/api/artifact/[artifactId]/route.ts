import { NextResponse } from "next/server";

/**
 * The authenticated artifact proxy (§8.5.6).
 *
 * An `<img>` tag cannot carry an `Authorization` header, and the alternative — putting the
 * token in a query string — places a credential in browser history, in the proxy logs of
 * anything between here and the API, and in any referrer the image happens to generate.
 * So the preview source is this Route Handler: it runs on the server, attaches the
 * **server-only** `PLATFORM_API_TOKEN` (§12.3 — no `NEXT_PUBLIC_` prefix, deliberately),
 * and streams the bytes back.
 *
 * `GET /artifacts/{id}` is not implemented yet (§15, B3). Rather than 404 in a way that
 * looks like a missing artifact, this returns `501` with a body naming the endpoint, so a
 * broken plot preview says what is missing instead of looking like a broken plot.
 */

const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/$/, "");

/** Server-only. If it is unset the proxy still works against a token-less dev box. */
const TOKEN = process.env.PLATFORM_API_TOKEN ?? "";

/**
 * What a proxied artifact may be.
 *
 * An allow-list, not a pass-through: this handler streams bytes the platform's own agents
 * produced, and a run that wrote an HTML file would otherwise be served same-origin with
 * whatever script it contained. Anything not listed is sent as an attachment instead.
 */
const INLINE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/svg+xml",
  "application/json",
  "text/plain",
  "text/csv",
  "text/markdown",
]);

export async function GET(
  request: Request,
  { params }: { params: Promise<{ artifactId: string }> },
) {
  const { artifactId } = await params;
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(artifactId)) {
    return NextResponse.json({ error: "malformed artifact id" }, { status: 400 });
  }

  const download = new URL(request.url).searchParams.get("download") === "1";
  const upstream = `${API_BASE}/api/v1/artifacts/${artifactId}${download ? "/download" : ""}`;

  let response: Response;
  try {
    response = await fetch(upstream, {
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : undefined,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { error: `The API at ${API_BASE} could not be reached.` },
      { status: 502 },
    );
  }

  if (response.status === 404 || response.status === 405) {
    return NextResponse.json(
      {
        error:
          "GET /api/v1/artifacts/{id} is not implemented yet (docs/FRONTEND.md §15, B3).",
      },
      { status: 501 },
    );
  }
  if (!response.ok || !response.body) {
    return NextResponse.json(
      { error: `The API answered ${response.status}.` },
      { status: response.status },
    );
  }

  const contentType = response.headers.get("content-type") ?? "application/octet-stream";
  const inline = INLINE_TYPES.has(contentType.split(";")[0]!.trim());

  return new NextResponse(response.body, {
    status: 200,
    headers: {
      "Content-Type": contentType,
      "Content-Disposition": inline ? "inline" : "attachment",
      // A run's artifacts are immutable once written, so this is safe and it keeps a deck
      // with six plot tiles from re-fetching them on every re-render.
      "Cache-Control": "private, max-age=300",
      // Belt and braces beside the allow-list: even an artifact that slipped through as
      // HTML cannot execute.
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
