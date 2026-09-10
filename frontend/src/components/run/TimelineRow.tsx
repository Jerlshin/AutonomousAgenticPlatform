"use client";

import clsx from "clsx";
import { memo } from "react";
import { formatDuration, formatTokens } from "@/lib/format";
import type { TimelineEntry, Tone } from "@/lib/types";
import { GLYPH } from "@/components/ui/primitives";

/**
 * One node visit in the trace (§8.5.4).
 *
 * ```
 *  ✓ coder rev 2     31.2s   1.7k tok   +6 −3 lines
 *  ✗ sandbox_exec     4.2s              ValueError: could not convert string to float
 *  ▶ sandbox_exec    running…           exec e2 · train profile
 * ```
 *
 * `memo`'d because the trace is virtualized above 200 entries and every commit would
 * otherwise re-render the whole visible window (§9.2). The live elapsed counter on a
 * running row is passed in as a prop rather than owned here: §9.3 requires one shared
 * 1 Hz interval for the whole deck, never one `setInterval` per row.
 */

const STATUS_TONE: Record<TimelineEntry["status"], Tone> = {
  running: "running",
  succeeded: "ok",
  failed: "fail",
  degraded: "warn",
  gate: "warn",
};

const STATUS_GLYPH: Record<TimelineEntry["status"], string> = {
  running: GLYPH.running,
  succeeded: GLYPH.ok,
  failed: GLYPH.fail,
  degraded: GLYPH.warn,
  gate: "⏸",
};

const TONE_TEXT: Record<Tone, string> = {
  running: "text-running",
  ok: "text-ok",
  warn: "text-warn",
  fail: "text-fail",
  idle: "text-idle",
};

export const TIMELINE_ROW_HEIGHT = 24;

export const TimelineRow = memo(function TimelineRow({
  entry,
  elapsedSeconds,
  selected,
  onSelect,
}: {
  entry: TimelineEntry;
  /** Seconds since this entry opened. Only meaningful while it is running. */
  elapsedSeconds: number;
  selected: boolean;
  onSelect: (entry: TimelineEntry) => void;
}) {
  const tone = STATUS_TONE[entry.status];
  const detail = entry.error ?? entry.summary ?? "";

  return (
    <button
      type="button"
      onClick={() => onSelect(entry)}
      title={
        entry.status === "failed" && entry.errorKind
          ? `${entry.errorKind}: ${entry.error ?? ""}`
          : detail || undefined
      }
      className={clsx(
        "flex w-full items-center gap-2 px-3 text-left text-xs leading-6 transition-colors duration-150",
        "hover:bg-raised",
        selected && "bg-raised",
      )}
      style={{ height: TIMELINE_ROW_HEIGHT }}
    >
      <span className={clsx("w-3 shrink-0 text-center", TONE_TEXT[tone])} aria-hidden>
        {STATUS_GLYPH[entry.status]}
      </span>

      <span className="w-32 shrink-0 truncate font-mono">
        {entry.node}
        {entry.revision !== undefined && (
          <span className="ml-1 text-muted">rev {entry.revision}</span>
        )}
        {entry.attempt !== undefined && entry.attempt > 1 && (
          <span className="ml-1 text-warn">
            ↻{entry.attempt}/{entry.maxAttempts ?? "?"}
          </span>
        )}
      </span>

      <span className="tnum w-16 shrink-0 text-right text-muted">
        {entry.status === "running"
          ? `${elapsedSeconds.toFixed(0)}s…`
          : formatDuration(entry.durationMs)}
      </span>

      <span className="tnum w-16 shrink-0 text-right text-idle">
        {entry.tokensIn === undefined && entry.tokensOut === undefined
          ? ""
          : `${formatTokens((entry.tokensIn ?? 0) + (entry.tokensOut ?? 0))} tok`}
      </span>

      <span
        className={clsx(
          "min-w-0 flex-1 truncate",
          entry.status === "failed" ? "text-fail" : "text-muted",
        )}
      >
        {entry.status === "degraded" && (
          <span className="mr-1 rounded bg-warn/15 px-1 text-[10px] text-warn">
            fell back
          </span>
        )}
        {detail}
      </span>
    </button>
  );
});
