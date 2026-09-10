"use client";

import clsx from "clsx";
import Link from "next/link";
import { useMemo } from "react";
import { formatClock, formatTokens, elapsedSeconds } from "@/lib/format";
import { statusLabel, toneForBudget, toneForStatus } from "@/lib/status";
import type { RunStream } from "@/hooks/useRunStream";
import { useRunShallow, selectHeader } from "@/hooks/useRunSlice";
import { useSecondTicker } from "@/hooks/useTicker";
import { Chip, Dot } from "@/components/ui/primitives";
import { Tooltip } from "@/components/ui/tooltip";
import { OperatorControls } from "./OperatorControls";

/**
 * The deck header (§8.5.2).
 *
 * ```
 * ● RUNNING · EXECUTE · 04:12 · 62%                    [Cancel] [Resync] [⋮]
 * breast-cancer-classifier   debug 1/4 · replans 0/2 · tokens 41k/250k · ws ●
 * ```
 *
 * Three details are specified rather than incidental:
 *
 * * **status comes from two places and neither is redundant.** `status_detail` carries
 *   the §5.3 states the durable column does not have — `QUEUED`, `PARTIAL`,
 *   `AWAITING_INPUT`, `INTERRUPTED` — and wins when present because it is the more
 *   current of the two.
 * * **elapsed ticks from one shared 1 Hz interval**, never a timer per component (§9.3),
 *   and freezes at terminal: a clock still counting on a finished run is a lie.
 * * **progress has exactly one definition at a time.** The summary's `percent` when there
 *   is one, otherwise completed plan steps over total. Mixing the two produces a bar that
 *   goes backwards when the second definition takes over.
 */
export function RunHeader({ stream }: { stream: RunStream }) {
  const {
    run,
    phase,
    phaseHistory,
    terminal,
    outcome,
    activeNode,
    usage,
    budgets,
    queuePosition,
    historyComplete,
    error,
  } = useRunShallow(selectHeader);
  const planSteps = useRunShallow((state) => ({
    total: state.planSteps.length,
    done: state.planSteps.filter((step) => step.status === "SUCCEEDED").length,
  }));
  const now = useSecondTicker();

  const label = statusLabel(run?.status, run?.status_detail);
  const tone = toneForStatus(run?.status, run?.status_detail, outcome);

  const elapsed = useMemo(() => {
    if (!run?.created_at) return 0;
    // Frozen at terminal: `updated_at` is when the run actually stopped.
    return terminal
      ? elapsedSeconds(run.created_at, run.updated_at)
      : elapsedSeconds(run.created_at, null, now || Date.now());
  }, [run?.created_at, run?.updated_at, terminal, now]);

  // One definition, never both (§8.5.2).
  const percent =
    run?.percent != null
      ? run.percent
      : planSteps.total > 0
        ? (100 * planSteps.done) / planSteps.total
        : null;
  const percentSource = run?.percent != null ? "the run summary" : "completed plan steps";

  return (
    <header className="flex shrink-0 flex-col gap-1 border-b border-line bg-surface px-4 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="flex items-center gap-2 text-sm">
          <Dot tone={tone} pulse={!terminal && (tone === "running" || tone === "warn")} label={label} />
          <span className="font-semibold">{label}</span>
          {phase && (
            <Tooltip
              side="bottom"
              content={
                phaseHistory.length === 0 ? (
                  <span>No phase transition has been recorded yet.</span>
                ) : (
                  <span className="flex flex-col gap-0.5">
                    {phaseHistory.map((entry, index) => {
                      const next = phaseHistory[index + 1];
                      const seconds = elapsedSeconds(
                        entry.ts,
                        next?.ts ?? (terminal ? run?.updated_at : null),
                        now || Date.now(),
                      );
                      return (
                        <span key={`${entry.phase}-${entry.ts}`} className="flex gap-2">
                          <span className="font-mono">{entry.phase}</span>
                          <span className="tnum text-muted">{formatClock(seconds)}</span>
                        </span>
                      );
                    })}
                  </span>
                )
              }
            >
              <span className="cursor-help text-muted underline decoration-dotted">
                {phase}
              </span>
            </Tooltip>
          )}
          {activeNode && !terminal && (
            <span className="font-mono text-running">{activeNode}</span>
          )}
          <span className="tnum text-muted" title="Elapsed since the run was created">
            {formatClock(elapsed)}
          </span>
          {percent != null && (
            <span
              className="tnum text-muted"
              title={`Progress, from ${percentSource}`}
            >
              {Math.round(percent)}%
            </span>
          )}
          {queuePosition != null && queuePosition > 0 && (
            <Chip tone="idle">queued at {queuePosition}</Chip>
          )}
        </span>

        <span className="ml-auto flex items-center gap-1.5">
          {run?.run_id && <DeckLinks runId={run.run_id} />}
          <ConnectionChip stream={stream} historyComplete={historyComplete} />
          <OperatorControls stream={stream} />
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <h1 className="truncate font-semibold" title={run?.title}>
          {run?.title ?? "Run"}
        </h1>

        <BudgetChip
          label="debug"
          used={run?.debug_iterations ?? 0}
          limit={budgets.max_debug_iterations?.limit ?? 4}
        />
        <BudgetChip
          label="replans"
          used={run?.replan_count ?? 0}
          limit={budgets.max_replans?.limit ?? 2}
        />
        <BudgetChip
          label="nodes"
          used={usage.nodeVisits || (run?.node_visits ?? 0)}
          limit={budgets.max_node_visits?.limit ?? 60}
        />
        <BudgetChip
          label="tokens"
          used={
            usage.tokensIn + usage.tokensOut ||
            (run?.tokens_in ?? 0) + (run?.tokens_out ?? 0)
          }
          limit={budgets.max_tokens?.limit ?? 250_000}
          format={formatTokens}
        />

        {/* Every other budget the backend warned about, so a threshold this header does
            not hard-code still reaches the operator. */}
        {Object.entries(budgets)
          .filter(([resource]) => !KNOWN_BUDGETS.has(resource))
          .map(([resource, budget]) => (
            <Chip key={resource} tone={toneForBudget(budget.percent)}>
              {resource} {Math.round(budget.percent)}%
            </Chip>
          ))}
      </div>

      {error && !error.retryable && (
        <p className="text-xs text-fail" role="alert">
          ✗ {error.message}
        </p>
      )}

      {/* Run status changes announce through a polite live region (§11). The console is
          deliberately not one — announcing thousands of lines is hostile. */}
      <span className="sr-only" role="status" aria-live="polite">
        {label}
        {phase ? `, phase ${phase}` : ""}
        {activeNode ? `, ${activeNode} executing` : ""}
      </span>
    </header>
  );
}

const KNOWN_BUDGETS = new Set([
  "max_debug_iterations",
  "max_replans",
  "max_node_visits",
  "max_tokens",
]);

/**
 * A budget readout.
 *
 * Turns `warn` at 80% — the threshold `budget.warning` itself fires at, so the chip and
 * the event agree — and `fail` at 100%.
 */
function BudgetChip({
  label,
  used,
  limit,
  format = (value: number) => String(value),
}: {
  label: string;
  used: number;
  limit: number;
  format?: (value: number) => string;
}) {
  const percent = limit > 0 ? (100 * used) / limit : 0;
  return (
    <Chip
      tone={toneForBudget(percent)}
      dot={percent >= 80}
      title={`${label}: ${used} of ${limit} (${Math.round(percent)}%)`}
    >
      {label} {format(used)}/{format(limit)}
    </Chip>
  );
}

/**
 * The socket's own state.
 *
 * A reconnect MUST NOT blank the panes (§7.6): the buffers hold what arrived and replay
 * refills the gap, so this badge is the *only* thing that changes when the connection
 * drops. It also carries the incomplete-history marker, because a deck whose buffers have
 * a hole in them should not look identical to one that does not.
 */
function ConnectionChip({
  stream,
  historyComplete,
}: {
  stream: RunStream;
  historyComplete: boolean;
}) {
  const { status, nextRetryInMs, reconnectAttempts, replayed, lastSeq } = stream;

  const tone =
    status === "open" ? "ok" : status === "closed" ? "idle" : "warn";
  const text =
    status === "open"
      ? replayed
        ? "live"
        : "replaying…"
      : status === "reconnecting"
        ? nextRetryInMs
          ? `reconnecting in ${Math.ceil(nextRetryInMs / 1000)}s…`
          : "reconnecting…"
        : status === "connecting"
          ? "connecting…"
          : "closed";

  return (
    <span className="flex items-center gap-1">
      <Chip
        tone={tone}
        title={`seq ${lastSeq}${reconnectAttempts > 0 ? ` · ${reconnectAttempts} reconnects` : ""}${
          stream.effectiveFilter ? ` · filter ${stream.effectiveFilter.join(", ")}` : ""
        }`}
      >
        <span className={clsx(status !== "open" && status !== "closed" && "pulse")}>
          ws
        </span>{" "}
        {text}
      </Chip>
      {!historyComplete && (
        <Chip
          tone="warn"
          dot={false}
          title="Part of this run's history is not held — either the server's retention window dropped it or the client's ring buffer evicted it."
        >
          partial history
        </Chip>
      )}
    </span>
  );
}

/**
 * The deck's two sibling views (§8.7, §8.8).
 *
 * Links rather than panes: the diff viewer and the report are documents to read, and
 * §8.5.1 gives the deck's four panes the whole viewport with no page scroll. Putting
 * either inside the grid would mean a pane that scrolls a document while the run is
 * moving, which is the layout the deck geometry exists to prevent.
 */
function DeckLinks({ runId }: { runId: string }) {
  return (
    <span className="flex items-center gap-1">
      <Link
        href={`/runs/${runId}/code`}
        className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-raised hover:text-fg"
        title="Revision diff viewer (§8.7)"
      >
        Code
      </Link>
      <Link
        href={`/runs/${runId}/report`}
        className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-raised hover:text-fg"
        title="Rendered REPORT.md (§8.8)"
      >
        Report
      </Link>
    </span>
  );
}
