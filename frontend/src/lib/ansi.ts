/**
 * ANSI escape handling for the console pane (§8.5.5).
 *
 * Training scripts colour their output — `tqdm`, `rich`, `pytest` — and raw escapes in a
 * virtualized row break alignment: the escape occupies no visual width but does occupy
 * characters, so a fixed-height row wraps where it should not and the column of
 * timestamps beside it stops lining up.
 *
 * Stripping is therefore the default, with rendering behind a toggle. Both paths are here
 * rather than in the component because both are pure string work that runs per visible
 * row per frame (§9.3), and because the strip is worth a test of its own.
 */

/** The escape byte every sequence starts with. */
const ESC = "\u001b";

/**
 * Every escape sequence, not only SGR colours.
 *
 * CSI (`ESC [ … final`) covers colours, cursor movement and erase; OSC (`ESC ] … BEL|ST`)
 * covers the window-title sequences `rich` emits; the single-character forms cover the
 * rest. A regex that only handled `ESC [ 0-9;* m` would leave `tqdm`'s carriage-return
 * cursor dance intact, which is the case that actually breaks a log viewer.
 */
const ESCAPES = new RegExp(
  `${ESC}(?:\\[[0-?]*[ -/]*[@-~]|\\][^\\u0007${ESC}]*(?:\\u0007|${ESC}\\\\)|[@-Z\\\\-_])`,
  "g",
);

/** Strip every escape sequence, leaving the text a person meant to read. */
export function stripAnsi(text: string): string {
  // The scan is skipped entirely for the overwhelming majority of lines, which carry no
  // escape at all. `indexOf` on a short string is far cheaper than running the regex.
  return text.indexOf(ESC) === -1 ? text : text.replace(ESCAPES, "");
}

export interface AnsiSpan {
  text: string;
  /** A Tailwind class, or undefined for the default foreground. */
  className?: string;
  bold?: boolean;
  dim?: boolean;
}

/**
 * The eight basic SGR colours, mapped onto the §10 palette rather than to raw hex.
 *
 * A console that renders its own eight colours alongside the platform's five status tones
 * would be two colour languages in one pane. Mapping the ANSI palette onto the tokens
 * keeps `stderr`-red and ANSI-red the same red, which is what makes the pane readable.
 */
const SGR_COLOURS: Record<number, string> = {
  30: "text-idle",
  31: "text-fail",
  32: "text-ok",
  33: "text-warn",
  34: "text-running",
  35: "text-[#c792ea]",
  36: "text-[#89ddff]",
  37: "text-fg",
  90: "text-idle",
  91: "text-fail",
  92: "text-ok",
  93: "text-warn",
  94: "text-running",
  95: "text-[#c792ea]",
  96: "text-[#89ddff]",
  97: "text-fg",
};

/**
 * Split a line into styled spans.
 *
 * Only SGR (`m`) sequences produce styling; every other escape is dropped, because cursor
 * movement in a virtualized row has nowhere to move to. State is carried forward across
 * spans within the line but not between lines: a program that opens a colour and never
 * closes it must not tint the rest of the run.
 */
export function parseAnsi(text: string): AnsiSpan[] {
  if (text.indexOf(ESC) === -1) return [{ text }];

  const spans: AnsiSpan[] = [];
  let className: string | undefined;
  let bold = false;
  let dim = false;
  let cursor = 0;

  ESCAPES.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = ESCAPES.exec(text)) !== null) {
    if (match.index > cursor) {
      spans.push({ text: text.slice(cursor, match.index), className, bold, dim });
    }
    cursor = match.index + match[0].length;

    const sequence = match[0];
    if (!sequence.endsWith("m")) continue;
    for (const code of sequence.slice(2, -1).split(";")) {
      const value = Number(code || "0");
      if (value === 0) {
        className = undefined;
        bold = false;
        dim = false;
      } else if (value === 1) bold = true;
      else if (value === 2) dim = true;
      else if (value === 22) {
        bold = false;
        dim = false;
      } else if (SGR_COLOURS[value]) className = SGR_COLOURS[value];
    }
  }

  if (cursor < text.length) {
    spans.push({ text: text.slice(cursor), className, bold, dim });
  }
  return spans.length > 0 ? spans : [{ text: "" }];
}

/**
 * Compile a search term, tolerating a partially typed regex.
 *
 * §8.5.5: an invalid partial regex must not throw during typing. `foo(` is what a valid
 * pattern looks like halfway through being written, and a viewer that throws on it makes
 * the regex toggle unusable.
 */
export function compileSearch(
  term: string,
  useRegex: boolean,
): { test: (line: string) => boolean; invalid: boolean } {
  if (!term) return { test: () => true, invalid: false };
  if (!useRegex) {
    const needle = term.toLowerCase();
    return { test: (line) => line.toLowerCase().includes(needle), invalid: false };
  }
  try {
    const pattern = new RegExp(term, "i");
    return { test: (line) => pattern.test(line), invalid: false };
  } catch {
    // Match nothing rather than everything: a half-typed pattern showing every line looks
    // like the filter is broken, and showing none reads as "not yet".
    return { test: () => false, invalid: true };
  }
}
