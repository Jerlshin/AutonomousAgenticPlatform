"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { formatClock, elapsedSeconds } from "@/lib/format";
import { qk } from "@/lib/queryKeys";
import { isActiveStatus, statusLabel, toneForStatus } from "@/lib/status";
import type { TaskRead } from "@/lib/rest";
import { Dot } from "@/components/ui/primitives";
import { useRunStream } from "@/hooks/useRunStream";

/**
 * One active run on the dashboard, with its live phase and percent (§8.2).
 *
 * The `subscribe` filter earns its complexity in exactly this component and nowhere else.
 * A dashboard row wants the run's phase, not its token stream; `RUN_ONLY` skips several
 * hundred `token.delta` frames per run per minute, which is the difference between a
 * smooth list and a stuttering one. It is a module constant because §7.2 requires the
 * filter to be a stable reference — a fresh array literal per render would reconnect the
 * socket on every render.
 *
 * Above the concurrency cap (§8.2) the row polls instead. The per-run WebSocket quota is
 * 8 and the deck itself uses one, so a dashboard that opened a socket per active run
 * would lock the operator out of the view they were about to click into.
 */

/** `run.` for lifecycle, `node.started` for the current node. Unioned with the control floor. */
const RUN_ONLY = ["run.", "node.started"] as const;

export function LiveRunRow({ task, live }: { task: TaskRead; live: boolean }) {
  return live ? <StreamedRow task={task} /> : <PolledRow task={task} />;
}

function StreamedRow({ task }: { task: TaskRead }) {
  const stream = useRunStream(task.id, { types: RUN_ONLY });
  return (
    <Row
      task={task}
      phase={stream.phase}
      node={stream.activeNode}
      percent={stream.run?.percent ?? null}
      detail={stream.run?.status_detail ?? null}
      source="live"
    />
  );
}

function PolledRow({ task }: { task: TaskRead }) {
  // 10 s, and only while the row is active. Polling a list of finished tasks is waste
  // (§6.2), and the row stops polling the moment its status leaves the active set.
  const { data } = useQuery({
    queryKey: qk.run(task.id),
    queryFn: () => api.getRun(task.id),
    refetchInterval: (query) =>
      isActiveStatus(query.state.data?.status, query.state.data?.status_detail)
        ? 10_000
        : false,
    staleTime: 5_000,
  });

  return (
    <Row
      task={task}
      phase={data?.phase ?? null}
      node={data?.current_node ?? null}
      percent={data?.percent ?? null}
      detail={data?.status_detail ?? null}
      source="polled"
    />
  );
}

function Row({
  task,
  phase,
  node,
  percent,
  detail,
  source,
}: {
  task: TaskRead;
  phase: string | null;
  node: string | null;
  percent: number | null;
  detail: string | null;
  source: "live" | "polled";
}) {
  const tone = toneForStatus(task.status, detail);
  const label = statusLabel(task.status, detail);

  return (
    <li className="flex items-center gap-2 border-b border-line/60 px-3 py-1.5 text-xs last:border-b-0">
      <Dot tone={tone} pulse={tone === "running"} label={label} />
      <Link
        href={`/runs/${task.id}`}
        className="min-w-0 flex-1 truncate transition-colors hover:text-running"
        title={task.title}
      >
        {task.title}
      </Link>
      <span className="w-20 shrink-0 truncate text-right text-muted" title={node ?? undefined}>
        {phase ?? label}
      </span>
      <span className="tnum w-10 shrink-0 text-right text-muted">
        {percent == null ? "—" : `${Math.round(percent)}%`}
      </span>
      <span
        className="tnum w-14 shrink-0 text-right text-idle"
        title={`Started ${task.created_at}`}
      >
        {formatClock(elapsedSeconds(task.created_at))}
      </span>
      <span
        className="w-3 shrink-0 text-center text-[10px] text-idle"
        // Which rows are live and which are polling is not cosmetic: a polled row is up
        // to ten seconds stale, and an operator comparing two rows deserves to know.
        title={
          source === "live"
            ? "Live over the run's WebSocket."
            : "Polled every 10 s — the concurrent-stream cap (4) is already taken."
        }
      >
        {source === "live" ? "◉" : "◌"}
      </span>
    </li>
  );
}
