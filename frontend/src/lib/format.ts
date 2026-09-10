/**
 * Formatting helpers.
 *
 * docs/FRONTEND.md §9.3: these run per visible row per frame, so they are pure and
 * allocation-light — no `Intl` object constructed per call, no template chains, no regex.
 * The `Intl` formatters that are worth using are built once at module scope.
 *
 * §10 sets two rules these enforce rather than merely follow:
 *
 * * **Metric precision is four decimals and never rounded for display.** The report is
 *   generated from the same state; a UI showing `0.97` next to a report saying `0.9737`
 *   looks like a discrepancy in the platform rather than in its formatting.
 * * **Numbers are tabular.** That is a CSS concern (`tabular-nums`), but these functions
 *   produce fixed-width output where they can so the column does not jitter each tick.
 */

/** `8412` → `8.4s`. Durations in the timeline are read at a glance, not measured. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  if (minutes < 60) return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${(minutes % 60).toString().padStart(2, "0")}m`;
}

/** `252` → `04:12`. The header's elapsed clock, which ticks and must not jitter. */
export function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  const mm = minutes.toString().padStart(2, "0");
  const ss = secs.toString().padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** `41234` → `41.2k`. Token counts are compared, not summed, by the reader. */
export function formatTokens(count: number | null | undefined): string {
  if (count == null || !Number.isFinite(count)) return "—";
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(2)}M`;
}

/**
 * A metric value at the precision the report uses.
 *
 * Four significant decimals, and trailing zeros are kept: `0.9700` and `0.97` are the
 * same number but not the same claim about how it was measured.
 */
export function formatMetric(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  if (magnitude !== 0 && (magnitude >= 1e6 || magnitude < 1e-4)) {
    return value.toExponential(4);
  }
  return value.toFixed(4);
}

/** `0.9737` → `97.4%`. For gauges and rates only, never for a criterion's observation. */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

const COMPARATOR_GLYPH: Record<string, string> = {
  gte: "≥",
  lte: "≤",
  gt: ">",
  lt: "<",
  eq: "=",
  approx: "≈",
};

/**
 * `gte`, `0.95` → `≥ 0.9500`.
 *
 * §8.5.4 requires the comparator be rendered literally: "target 0.95" hides whether the
 * bound is inclusive, and `approx` carries a tolerance that changes what passing means.
 */
export function formatComparator(
  comparator: string,
  threshold: number,
  tolerance = 0,
): string {
  const glyph = COMPARATOR_GLYPH[comparator] ?? comparator;
  const bound = formatMetric(threshold);
  if (comparator === "approx" && tolerance > 0) {
    return `${glyph} ${bound} ±${formatMetric(tolerance)}`;
  }
  return `${glyph} ${bound}`;
}

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const ABSOLUTE = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "medium",
});

const DIVISIONS: readonly [number, Intl.RelativeTimeFormatUnit][] = [
  [60, "second"],
  [60, "minute"],
  [24, "hour"],
  [7, "day"],
  [4.348, "week"],
  [12, "month"],
  [Number.POSITIVE_INFINITY, "year"],
];

/**
 * `"4m ago"`. `date-fns` is not a dependency here — §12.1 permits `Intl.RelativeTimeFormat`
 * instead, and one formatter built at module scope is cheaper than the library's parse.
 */
export function formatRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "—";
  let delta = (parsed - now) / 1000;
  for (const [span, unit] of DIVISIONS) {
    if (Math.abs(delta) < span) return RELATIVE.format(Math.round(delta), unit);
    delta /= span;
  }
  return RELATIVE.format(Math.round(delta), "year");
}

/** The `title` attribute beside every relative timestamp (§8.3). */
export function formatAbsolute(iso: string | null | undefined): string {
  if (!iso) return "";
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? "" : ABSOLUTE.format(parsed);
}

/** `HH:MM:SS.mmm` from an event `ts`, for the console's optional timestamp column. */
export function formatWallClock(iso: string): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "--:--:--.---";
  const date = new Date(parsed);
  const pad = (n: number, width = 2) => n.toString().padStart(width, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(
    date.getMilliseconds(),
    3,
  )}`;
}

/** The first twelve characters of a hash — enough to identify, short enough to read. */
export function shortHash(sha256: string | null | undefined): string {
  return sha256 ? sha256.slice(0, 12) : "—";
}

/** `"b41e7c2a-…"` → `"b41e7c2a"`. The prefix MLflow's run naming uses (MLOPS.md §4.1). */
export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}

/** Seconds between two ISO timestamps, or from one to now. Negative clamps to zero. */
export function elapsedSeconds(
  startIso: string | null | undefined,
  endIso?: string | null,
  now = Date.now(),
): number {
  if (!startIso) return 0;
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) return 0;
  const end = endIso ? Date.parse(endIso) : now;
  return Math.max(0, ((Number.isNaN(end) ? now : end) - start) / 1000);
}
