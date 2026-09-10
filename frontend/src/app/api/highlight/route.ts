import { createHighlighter, type Highlighter } from "shiki";
import { NextResponse } from "next/server";

/**
 * Server-side syntax highlighting (§8.7).
 *
 * Shiki is **server-only** by rule (§2.1): its grammars and themes are a megabyte of
 * JSON, and shipping them to highlight a two-hundred-line `main.py` would blow the run
 * view's bundle budget several times over. So the browser posts source here and receives
 * HTML.
 *
 * The highlighter is created once per server process and reused. `createHighlighter`
 * loads and compiles grammars, which costs a few hundred milliseconds; doing it per
 * request would make the diff viewer feel broken on a cold page. The promise — not the
 * resolved value — is cached, so concurrent first requests share one initialisation
 * rather than racing to start several.
 */

/** Only what the sandbox actually produces. Each grammar loaded is bytes and startup time. */
const LANGUAGES = ["python", "json", "markdown", "text"] as const;
type Language = (typeof LANGUAGES)[number];

/** Matches the §10 palette closely enough not to look like a different application. */
const THEME = "github-dark-default";

/** A cap, because this endpoint accepts a body from the browser. */
const MAX_SOURCE_BYTES = 512 * 1024;

let highlighterPromise: Promise<Highlighter> | null = null;

function highlighter(): Promise<Highlighter> {
  highlighterPromise ??= createHighlighter({
    themes: [THEME],
    langs: [...LANGUAGES],
  });
  return highlighterPromise;
}

export async function POST(request: Request) {
  let body: { code?: unknown; lang?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "the body must be JSON" }, { status: 400 });
  }

  const code = typeof body.code === "string" ? body.code : "";
  if (code.length > MAX_SOURCE_BYTES) {
    return NextResponse.json(
      { error: `the source exceeds ${MAX_SOURCE_BYTES} bytes` },
      { status: 413 },
    );
  }

  // An unknown language falls back to `text` rather than failing: a revision written in
  // something this route does not carry a grammar for should still be readable.
  const requested = typeof body.lang === "string" ? body.lang : "text";
  const lang: Language = (LANGUAGES as readonly string[]).includes(requested)
    ? (requested as Language)
    : "text";

  try {
    const shiki = await highlighter();
    const html = shiki.codeToHtml(code, {
      lang,
      theme: THEME,
      // The viewer supplies its own line numbers and diff tones, so Shiki's job is
      // tokens only.
      structure: "inline",
    });
    return NextResponse.json(
      { html, lang },
      {
        headers: {
          // Callers key by `sha256`, and a revision's content is immutable once written.
          "Cache-Control": "private, max-age=3600",
        },
      },
    );
  } catch (cause) {
    return NextResponse.json(
      { error: cause instanceof Error ? cause.message : "highlighting failed" },
      { status: 500 },
    );
  }
}
