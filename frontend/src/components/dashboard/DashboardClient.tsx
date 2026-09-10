"use client";

import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useMemo } from "react";
import { api } from "@/lib/api";
import { CAPABILITIES, REASONS } from "@/lib/capabilities";
import { formatClock, formatRelative, formatAbsolute } from "@/lib/format";
import { outcomeOf } from "@/lib/outcome";
import { qk } from "@/lib/queryKeys";
import { isActiveStatus, statusLabel, toneForOutcome, toneForStatus } from "@/lib/status";
import type { TaskRead } from "@/lib/rest";
import {
  Banner,
  Chip,
  Dot,
  Empty,
  ErrorState,
  Panel,
  Skeleton,
  Stat,
} from "@/components/ui/primitives";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { DependencyStrip } from "./DependencyStrip";
import { LiveRunRow } from "./LiveRunRow";

/**
 * The global dashboard (§8.2).
 *
 * It answers, in one screen and without scrolling: is the platform healthy, what is
 * running right now, what happened recently, and is anything stuck.
 *
 * Every figure on it is derived client-side from the most recent hundred tasks, because
 * there is no aggregate statistics endpoint yet (§15, B9). That is stated on the tiles
 * rather than left implicit — a success rate whose denominator is invisible is a number
 * people quote at each other for months.
 */

/** §8.2: at most four rows hold a socket. The per-run quota is 8 and the deck uses one. */
const MAX_LIVE_ROWS = 4;

/** How many tasks the derived statistics are computed over. */
const SAMPLE = 100;

// Recharts is ~90 KB and the trend is below the fold. Dynamically imported so it stays
// out of the dashboard's first-load budget (§9.4).
const OutcomeTrend = dynamic(() => import("@/components/charts/OutcomeTrend"), {
  ssr: false,
  loading: () => <Skeleton rows={4} className="h-40" />,
});

export function DashboardClient() {
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: qk.tasks({ skip: 0, limit: SAMPLE }),
    queryFn: () => api.listTasks(0, SAMPLE),
    staleTime: 10_000,
    // Poll only while something is actually running (§6.2). A list of finished tasks
    // refetched every fifteen seconds is pure waste.
    refetchInterval: (query) =>
      (query.state.data?.tasks ?? []).some((task) => isActiveStatus(task.status))
        ? 15_000
        : false,
  });

  const tasks = useMemo(() => data?.tasks ?? [], [data]);
  const active = useMemo(
    () => tasks.filter((task) => isActiveStatus(task.status)),
    [tasks],
  );
  const stats = useMemo(() => derive(tasks), [tasks]);

  const recent = useMemo(
    () =>
      [...tasks]
        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
        .slice(0, 12),
    [tasks],
  );

  if (isError) {
    return (
      <div className="p-4">
        <ErrorState
          title="Could not load tasks"
          message="The API did not answer GET /api/v1/tasks."
          onRetry={() => void refetch()}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 overflow-auto p-4">
      {!CAPABILITIES.stats && (
        <Banner tone="idle">
          Platform statistics are derived from the most recent {SAMPLE} tasks.{" "}
          {REASONS.stats}
        </Banner>
      )}

      <section aria-label="Platform statistics" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Active runs"
          value={isPending ? "—" : stats.active}
          hint={stats.queued > 0 ? `${stats.queued} queued` : "nothing queued"}
          tone={stats.active > 0 ? "running" : "idle"}
        />
        <Stat
          label="Queue depth"
          value={isPending ? "—" : stats.queued}
          hint={`from the last ${SAMPLE} tasks`}
          tone={stats.queued > 0 ? "warn" : "idle"}
        />
        <Stat
          label="Success rate"
          value={
            stats.finished === 0
              ? "—"
              : `${Math.round((100 * stats.succeeded) / stats.finished)}%`
          }
          hint={`${stats.succeeded}/${stats.finished} finished`}
          tone={
            stats.finished === 0
              ? "idle"
              : stats.succeeded / stats.finished >= 0.7
                ? "ok"
                : "warn"
          }
        />
        <Stat
          label="Median time"
          value={stats.medianSeconds == null ? "—" : formatClock(stats.medianSeconds)}
          hint="created → updated, finished runs"
        />
      </section>

      <div className="grid gap-3 lg:grid-cols-[2fr_1fr]">
        <Panel
          title="Active & queued"
          count={active.length}
          className="border border-line"
        >
          {isPending ? (
            <Skeleton rows={3} />
          ) : active.length === 0 ? (
            <Empty
              action={
                <Link
                  href="/tasks/new"
                  className="rounded border border-line px-2 py-1 text-xs transition-colors hover:bg-raised"
                >
                  Submit a task
                </Link>
              }
            >
              Nothing is running.
            </Empty>
          ) : (
            <ul>
              {active.map((task, index) => (
                <LiveRunRow key={task.id} task={task} live={index < MAX_LIVE_ROWS} />
              ))}
            </ul>
          )}
        </Panel>

        <DependencyStrip />
      </div>

      <Panel
        title="Outcome trend"
        count="last 14 days"
        className="border border-line"
        bodyClassName="min-h-0"
      >
        {isPending ? <Skeleton rows={4} className="h-40" /> : <OutcomeTrend tasks={tasks} />}
      </Panel>

      <Panel title="Recent runs" count={recent.length} className="border border-line">
        {isPending ? (
          <Skeleton rows={5} />
        ) : recent.length === 0 ? (
          <Empty
            action={
              <Link
                href="/tasks/new"
                className="rounded border border-line px-2 py-1 text-xs transition-colors hover:bg-raised"
              >
                Submit the first one
              </Link>
            }
          >
            No runs yet.
          </Empty>
        ) : (
          <Table caption="The twelve most recently updated runs">
            <THead>
              <TH>Title</TH>
              <TH>Status</TH>
              <TH>Outcome</TH>
              <TH numeric>Duration</TH>
              <TH numeric>Updated</TH>
            </THead>
            <TBody>
              {recent.map((task) => {
                const outcome = outcomeOf(task);
                return (
                  <TR key={task.id}>
                    <TD className="max-w-0">
                      <Link
                        href={`/runs/${task.id}`}
                        className="block truncate transition-colors hover:text-running"
                        title={task.title}
                      >
                        {task.title}
                      </Link>
                    </TD>
                    <TD>
                      <span className="inline-flex items-center gap-1.5">
                        <Dot
                          tone={toneForStatus(task.status, null, outcome)}
                          pulse={task.status === "RUNNING"}
                        />
                        {statusLabel(task.status)}
                      </span>
                    </TD>
                    <TD>
                      {outcome ? (
                        <Chip tone={toneForOutcome(outcome)} dot={false}>
                          {outcome}
                        </Chip>
                      ) : (
                        <span className="text-idle">—</span>
                      )}
                    </TD>
                    <TD numeric>
                      {formatClock(
                        (Date.parse(task.updated_at) - Date.parse(task.created_at)) / 1000,
                      )}
                    </TD>
                    <TD numeric title={formatAbsolute(task.updated_at)}>
                      {formatRelative(task.updated_at)}
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
      </Panel>
    </div>
  );
}

interface Derived {
  active: number;
  queued: number;
  finished: number;
  succeeded: number;
  medianSeconds: number | null;
}

/**
 * The tiles, from the task list.
 *
 * `succeeded` counts the run's own outcome, not the durable column: a `COMPLETED` task
 * whose run ended `PARTIAL` met some criteria and missed others, and counting it as a
 * success is how a platform reports a 90% success rate it does not have.
 */
function derive(tasks: readonly TaskRead[]): Derived {
  let active = 0;
  let queued = 0;
  let succeeded = 0;
  const durations: number[] = [];

  for (const task of tasks) {
    if (task.status === "RUNNING") active += 1;
    else if (task.status === "PENDING") queued += 1;

    if (task.status === "COMPLETED" || task.status === "FAILED" || task.status === "CANCELLED") {
      if (outcomeOf(task) === "SUCCEEDED") succeeded += 1;
      const seconds = (Date.parse(task.updated_at) - Date.parse(task.created_at)) / 1000;
      if (Number.isFinite(seconds) && seconds > 0) durations.push(seconds);
    }
  }

  durations.sort((a, b) => a - b);
  const middle = Math.floor(durations.length / 2);

  return {
    active,
    queued,
    finished: durations.length,
    succeeded,
    medianSeconds: durations.length === 0 ? null : (durations[middle] ?? null),
  };
}
