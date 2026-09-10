"use client";

import clsx from "clsx";
import { useMemo, useState } from "react";
import {
  diffLines,
  diffStats,
  parseUnifiedDiff,
  toSideBySide,
  type DiffLine,
} from "@/lib/diff";
import { Segmented } from "@/components/ui/tabs";

/**
 * The revision diff (§8.7).
 *
 * Side-by-side by default, unified behind a toggle. The diff itself comes from
 * `lib/diff.ts`; the server's own unified diff is used verbatim when the two revisions
 * are adjacent and it carried one, because that is the diff the backend actually produced
 * and recomputing it would let the two disagree over whitespace or context size.
 *
 * `highlightRange` is the correlation §8.7 requires: the failing region from
 * `ErrorRecord.offending_source` (± 5 lines) is marked in the *previous* revision, so
 * "what went wrong" and "what changed" are on the same screen. A diff without that is
 * just a patch.
 *
 * Syntax highlighting is deliberately absent from this component. Shiki is server-only
 * (§2.1) — its grammars must not ship to the browser — so highlighting happens in
 * `app/api/highlight/route.ts` and the code page composes the two. In the gate dialog,
 * where the decision is about *what changed*, the diff tones carry the meaning.
 */

export function DiffViewer({
  before,
  after,
  unifiedDiff,
  highlightRange,
  maxHeight = "max-h-[60vh]",
}: {
  before: string;
  after: string;
  /** The server's diff, when the two revisions are adjacent. */
  unifiedDiff?: string;
  /** 1-based, inclusive, in the "before" text. The region the error pointed at. */
  highlightRange?: { start: number; end: number };
  maxHeight?: string;
}) {
  const [mode, setMode] = useState<"split" | "unified">("split");

  const lines = useMemo<DiffLine[]>(() => {
    // The server's diff is a fallback, not a fast path, and only when neither side's
    // source is held: if the operator picked revisions 1 and 3, the payload's diff
    // describes a different pair, and rendering it would silently answer a question
    // nobody asked.
    if (!before && !after && unifiedDiff) {
      const parsed = parseUnifiedDiff(unifiedDiff);
      if (parsed) return parsed;
    }
    return diffLines(before, after);
  }, [before, after, unifiedDiff]);

  const stats = useMemo(() => diffStats(lines), [lines]);
  const rows = useMemo(() => toSideBySide(lines), [lines]);

  if (lines.length === 0) {
    return (
      <p className="p-2 text-[11px] text-idle">
        Neither revision&apos;s source is held in this tab.
      </p>
    );
  }

  const inRange = (line: DiffLine) =>
    highlightRange !== undefined &&
    line.beforeLine !== null &&
    line.beforeLine >= highlightRange.start &&
    line.beforeLine <= highlightRange.end;

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 pb-1">
        <Segmented
          label="Diff layout"
          value={mode}
          onChange={setMode}
          options={[
            { value: "split", label: "split" },
            { value: "unified", label: "unified" },
          ]}
        />
        <span className="tnum font-mono text-[11px]">
          <span className="text-ok">+{stats.added}</span>{" "}
          <span className="text-fail">−{stats.removed}</span>{" "}
          <span className="text-idle">={stats.unchanged}</span>
        </span>
        {highlightRange && (
          <span className="text-[11px] text-warn">
            ⚠ lines {highlightRange.start}–{highlightRange.end} are where the previous
            revision failed
          </span>
        )}
      </div>

      <div className={clsx("min-h-0 overflow-auto rounded border border-line", maxHeight)}>
        {mode === "unified" ? (
          <table className="w-full border-collapse font-mono text-[11px] leading-5">
            <tbody>
              {lines.map((line, index) => (
                <tr
                  key={index}
                  className={clsx(
                    line.op === "insert" && "bg-ok/10",
                    line.op === "delete" && "bg-fail/10",
                    inRange(line) && line.op !== "insert" && "outline outline-1 outline-warn/40",
                  )}
                >
                  <td className="w-10 select-none px-1 text-right text-idle">
                    {line.beforeLine ?? ""}
                  </td>
                  <td className="w-10 select-none px-1 text-right text-idle">
                    {line.afterLine ?? ""}
                  </td>
                  <td className="w-4 select-none text-center text-idle">
                    {line.op === "insert" ? "+" : line.op === "delete" ? "−" : " "}
                  </td>
                  <td className="whitespace-pre px-1">{line.text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="w-full table-fixed border-collapse font-mono text-[11px] leading-5">
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  <td className="w-10 select-none px-1 text-right text-idle">
                    {row.before?.beforeLine ?? ""}
                  </td>
                  <td
                    className={clsx(
                      "w-1/2 whitespace-pre px-1",
                      row.before?.op === "delete" && "bg-fail/10",
                      row.before && inRange(row.before) && "outline outline-1 outline-warn/40",
                    )}
                  >
                    {row.before?.text ?? ""}
                  </td>
                  <td className="w-10 select-none px-1 text-right text-idle">
                    {row.after?.afterLine ?? ""}
                  </td>
                  <td
                    className={clsx(
                      "w-1/2 whitespace-pre px-1",
                      row.after?.op === "insert" && "bg-ok/10",
                    )}
                  >
                    {row.after?.text ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
