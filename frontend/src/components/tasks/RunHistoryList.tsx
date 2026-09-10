"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { CAPABILITIES, reasonUnless } from "@/lib/capabilities";
import { formatAbsolute, formatClock, formatRelative } from "@/lib/format";
import { qk, TASKS_PREFIX } from "@/lib/queryKeys";
import { isActiveStatus, statusLabel, toneForOutcome, toneForStatus } from "@/lib/status";
import { requiredCriteriaScore } from "@/lib/terminalResult";
import {
  Banner,
  Button,
  Chip,
  Dot,
  Empty,
  ErrorState,
  Panel,
  Skeleton,
} from "@/components/ui/primitives";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/**
 * A task's run history, and the control that starts a new one (§8.3).
 *
 * Two behaviours here are specified rather than incidental:
 *
 * * **the idempotency key is generated once, at click time**, and held for the mutation's
 *   lifetime. Generating it inside the request function would produce a fresh key on
 *   every React retry, which is the one thing the header exists to prevent — and a second
 *   run is a second container and a second GPU-hour.
 * * **a `202` navigates straight to the run.** The operator's next question is always
 *   "what is it doing", and making them find the link is friction with no upside.
 *
 * A `409` means a run is already active; the message is surfaced and the existing run is
 * offered, because "start" failing with no explanation is indistinguishable from the
 * button being broken.
 */
export function RunHistoryList({ taskId }: { taskId: string }) {
  const router = useRouter();
  const client = useQueryClient();
  const [conflict, setConflict] = useState<string | null>(null);

  const { data, isPending, isError, refetch } = useQuery({
    queryKey: qk.taskRuns(taskId),
    queryFn: () => api.listTaskRuns(taskId),
    staleTime: 30_000,
  });

  const start = useMutation({
    mutationFn: () => {
      // One key per click, captured here. `crypto.randomUUID` is available in every
      // browser this app targets and in Node 19+, so no dependency is needed for it.
      const key = crypto.randomUUID();
      return api.startRun(taskId, key);
    },
    onSuccess: (accepted) => {
      setConflict(null);
      void client.invalidateQueries({ queryKey: qk.task(taskId) });
      void client.invalidateQueries({ queryKey: qk.taskRuns(taskId) });
      void client.invalidateQueries({ queryKey: TASKS_PREFIX });
      router.push(`/runs/${accepted.run_id}`);
    },
    onError: (cause: unknown) => {
      setConflict(
        cause instanceof ApiError
          ? cause.message
          : "The run could not be started.",
      );
    },
  });

  const runs = data?.runs ?? [];
  const activeRun = runs.find((run) => isActiveStatus(run.status, run.status_detail));

  return (
    <Panel
      title="Runs"
      count={data?.total ?? 0}
      className="border border-line"
      right={
        <Button
          tone="primary"
          disabled={start.isPending}
          onClick={() => start.mutate()}
          title={
            start.isPending
              ? "Waiting for the API to accept the run…"
              : "Enqueue a run for this task"
          }
        >
          {start.isPending ? "Starting…" : "Start run"}
        </Button>
      }
    >
      {conflict && (
        <Banner tone="warn">
          {conflict}
          {activeRun && (
            <>
              {" "}
              <Link href={`/runs/${activeRun.run_id}`} className="underline">
                Open the run that is already active
              </Link>
              .
            </>
          )}
        </Banner>
      )}

      {isError ? (
        <ErrorState
          title="Could not load this task's runs"
          onRetry={() => void refetch()}
        />
      ) : isPending ? (
        <Skeleton rows={3} />
      ) : runs.length === 0 ? (
        <Empty>This task has never been run.</Empty>
      ) : (
        <Table caption="Every run of this task">
          <THead>
            <TH>Run</TH>
            <TH>Status</TH>
            <TH>Outcome</TH>
            <TH numeric>Criteria</TH>
            <TH numeric>Duration</TH>
            <TH numeric>Started</TH>
            <TH />
          </THead>
          <TBody>
            {runs.map((run) => {
              const score = requiredCriteriaScore(run.result);
              return (
                <TR key={run.run_id}>
                  <TD mono title={run.run_id}>
                    {run.run_id.slice(0, 8)}
                  </TD>
                  <TD>
                    <span className="inline-flex items-center gap-1.5">
                      <Dot
                        tone={toneForStatus(run.status, run.status_detail, run.outcome)}
                        pulse={isActiveStatus(run.status, run.status_detail)}
                      />
                      {statusLabel(run.status, run.status_detail)}
                    </span>
                  </TD>
                  <TD>
                    {run.outcome ? (
                      <Chip tone={toneForOutcome(run.outcome)} dot={false}>
                        {run.outcome}
                      </Chip>
                    ) : (
                      <span className="text-idle">—</span>
                    )}
                  </TD>
                  <TD numeric title="Required criteria met, from the run's evaluation">
                    {score ?? "—"}
                  </TD>
                  <TD numeric>
                    {formatClock(
                      (Date.parse(run.updated_at) - Date.parse(run.created_at)) / 1000,
                    )}
                  </TD>
                  <TD numeric title={formatAbsolute(run.created_at)}>
                    {formatRelative(run.created_at)}
                  </TD>
                  <TD className="whitespace-nowrap text-right">
                    <Link
                      href={`/runs/${run.run_id}`}
                      className="rounded border border-line px-1.5 py-0.5 text-[11px] transition-colors hover:bg-raised"
                    >
                      Deck
                    </Link>{" "}
                    <Link
                      href={`/runs/${run.run_id}/code`}
                      className="rounded border border-line px-1.5 py-0.5 text-[11px] transition-colors hover:bg-raised"
                    >
                      Code
                    </Link>{" "}
                    <Link
                      href={`/runs/${run.run_id}/report`}
                      className="rounded border border-line px-1.5 py-0.5 text-[11px] transition-colors hover:bg-raised"
                      title={reasonUnless("runReport")}
                    >
                      Report
                    </Link>
                  </TD>
                </TR>
              );
            })}
          </TBody>
        </Table>
      )}

      {!CAPABILITIES.runSteps && runs.length > 0 && (
        <p className="border-t border-line px-3 py-1.5 text-[11px] text-idle">
          Per-run step detail for a historical run needs{" "}
          <code className="font-mono">GET /runs/{"{id}"}/steps</code>, which is not
          implemented yet. Open the deck for a live run to see its timeline.
        </p>
      )}
    </Panel>
  );
}
