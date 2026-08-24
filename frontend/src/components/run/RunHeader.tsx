"use client";

import { Button, Chip, Dot, formatTokens, type Tone } from "@/components/ui/primitives";
import type { RunStream } from "@/lib/useRunStream";

/**
 * The header strip (§18.3): status, phase, elapsed, budgets and the cancel control.
 *
 * Status comes from two places and neither is redundant. `run.status_detail` carries the
 * §5.3 states the durable column does not have — QUEUED, PARTIAL, INTERRUPTED — and the
 * column carries what survived the last restart. The detail wins when present because it
 * is the more current of the two.
 */
export function RunHeader({ stream }: { stream: RunStream }) {
  const { run, status, terminal, activeNode } = stream;
  const label = run?.status_detail ?? run?.status ?? "…";

  return (
    <header className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-surface px-4 py-2">
      <h1 className="text-sm font-semibold">{run?.title ?? "Run"}</h1>

      <span className="flex items-center gap-2 text-xs">
        <Dot tone={toneFor(label)} pulse={!terminal && label === "RUNNING"} />
        <span className="font-medium">{label}</span>
        {run?.phase && <span className="text-muted">· {run.phase}</span>}
        {activeNode && <span className="text-running">· {activeNode}</span>}
        {run?.percent != null && (
          <span className="text-muted">· {Math.round(run.percent)}%</span>
        )}
      </span>

      <span className="flex items-center gap-2">
        <Chip tone="idle">
          debug {run?.debug_iterations ?? 0} · replans {run?.replan_count ?? 0}
        </Chip>
        <Chip tone="idle">
          tokens {formatTokens((run?.tokens_in ?? 0) + (run?.tokens_out ?? 0))}
        </Chip>
        <Chip tone={connectionTone(status)}>{status}</Chip>
      </span>

      <span className="ml-auto flex items-center gap-2">
        <Button
          tone="danger"
          disabled={terminal || status !== "open"}
          onClick={() => stream.cancel("cancelled from the dashboard")}
        >
          Cancel
        </Button>
        <Button onClick={stream.resync} disabled={status !== "open"}>
          Resync
        </Button>
      </span>

      {stream.error && (
        <p className="w-full text-xs text-fail">{stream.error}</p>
      )}
    </header>
  );
}

function toneFor(status: string): Tone {
  switch (status) {
    case "RUNNING":
      return "running";
    case "COMPLETED":
    case "SUCCEEDED":
      return "ok";
    case "PARTIAL":
    case "INTERRUPTED":
    case "AWAITING_INPUT":
      return "warn";
    case "FAILED":
    case "CANCELLED":
      return "fail";
    default:
      return "idle";
  }
}

function connectionTone(status: string): Tone {
  if (status === "open") return "ok";
  if (status === "closed") return "idle";
  return "warn";
}
