"use client";

import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { formatAbsolute, formatRelative } from "@/lib/format";
import { qk } from "@/lib/queryKeys";
import { statusLabel, toneForStatus } from "@/lib/status";
import {
  Chip,
  Copyable,
  ErrorState,
  Panel,
  Skeleton,
} from "@/components/ui/primitives";
import { RunHistoryList } from "./RunHistoryList";

/** The task body and its run history (§8.3). */
export function TaskDetail({ taskId }: { taskId: string }) {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: qk.task(taskId),
    queryFn: () => api.getTask(taskId),
    staleTime: 10_000,
  });

  if (isError) {
    const missing = error instanceof ApiError && error.status === 404;
    return (
      <ErrorState
        title={missing ? "This task does not exist" : "Could not load this task"}
        message={
          missing
            ? "It may have been deleted, or the link may be from an older version of the dashboard."
            : error instanceof Error
              ? error.message
              : undefined
        }
        onRetry={missing ? undefined : () => void refetch()}
      />
    );
  }

  return (
    <>
      <Panel
        title={isPending ? "Task" : data.title}
        className="border border-line"
        right={
          !isPending && (
            <Chip tone={toneForStatus(data.status)}>{statusLabel(data.status)}</Chip>
          )
        }
      >
        {isPending ? (
          <Skeleton rows={4} />
        ) : (
          <div className="flex flex-col gap-3 p-3">
            <div>
              <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-muted">
                Prompt
              </h3>
              {/* Rendered whole and wrapped, never truncated: the prompt is the contract
                  the Planner turned into criteria, and a clipped one cannot be checked
                  against what the run actually did. */}
              <pre className="whitespace-pre-wrap rounded border border-line bg-ink px-3 py-2 font-mono text-xs leading-5 text-fg">
                {data.prompt}
              </pre>
            </div>

            <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs lg:grid-cols-4">
              <Meta label="Task id">
                <Copyable value={data.id}>{data.id.slice(0, 8)}</Copyable>
              </Meta>
              <Meta label="Created">
                <span title={formatAbsolute(data.created_at)}>
                  {formatRelative(data.created_at)}
                </span>
              </Meta>
              <Meta label="Updated">
                <span title={formatAbsolute(data.updated_at)}>
                  {formatRelative(data.updated_at)}
                </span>
              </Meta>
              <Meta label="Error">
                {data.error ? (
                  <span className="text-fail">{data.error}</span>
                ) : (
                  <span className="text-idle">none</span>
                )}
              </Meta>
            </dl>
          </div>
        )}
      </Panel>

      <RunHistoryList taskId={taskId} />
    </>
  );
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] font-semibold uppercase tracking-widest text-muted">
        {label}
      </dt>
      <dd className="truncate">{children}</dd>
    </div>
  );
}
