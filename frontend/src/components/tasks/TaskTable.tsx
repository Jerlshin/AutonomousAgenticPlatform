"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { formatAbsolute, formatClock, formatRelative } from "@/lib/format";
import { outcomeOf } from "@/lib/outcome";
import { qk } from "@/lib/queryKeys";
import { isActiveStatus, statusLabel, toneForOutcome, toneForStatus } from "@/lib/status";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  Button,
  Chip,
  Dot,
  Empty,
  ErrorState,
  Panel,
  Skeleton,
} from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { TextInput } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { VirtualList } from "@/components/ui/virtual-list";

/**
 * The task list (§8.3).
 *
 * `total` grows unbounded, so the API paginates at 20 and the table virtualizes above
 * `VIRTUALIZE_ABOVE` rows (§9.2). Below that threshold measurement costs more than it
 * saves, and a virtualized twelve-row table is a `<table>` that a screen reader can no
 * longer read as one — so the plain table is the default and virtualization is what
 * happens when a page is large enough to need it.
 *
 * `task_kind` and `tags` filters are specified but stay hidden until `TaskCreate` carries
 * those fields (§15, B6). Rendering a filter over data the API never returns would filter
 * everything out and look like an empty database.
 */

const PAGE_SIZE = 20;
const VIRTUALIZE_ABOVE = 200;
const ROW_HEIGHT = 33;

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "PENDING", label: "Pending" },
  { value: "RUNNING", label: "Running" },
  { value: "COMPLETED", label: "Completed" },
  { value: "FAILED", label: "Failed" },
  { value: "CANCELLED", label: "Cancelled" },
] as const;

export function TaskTable() {
  const [page, setPage] = useState(0);
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const query = useDebouncedValue(search, 150);

  const { data, isPending, isError, refetch, isPlaceholderData } = useQuery({
    queryKey: qk.tasks({ skip: page * PAGE_SIZE, limit: PAGE_SIZE }),
    queryFn: () => api.listTasks(page * PAGE_SIZE, PAGE_SIZE),
    staleTime: 10_000,
    refetchInterval: (q) =>
      (q.state.data?.tasks ?? []).some((task) => isActiveStatus(task.status))
        ? 15_000
        : false,
    placeholderData: (previous) => previous,
  });

  // Filtered client-side over the fetched page: the API has no status or title
  // parameters, and pretending otherwise would silently filter one page and call it a
  // search over the whole table.
  const rows = useMemo(() => {
    const tasks = data?.tasks ?? [];
    const needle = query.trim().toLowerCase();
    return tasks.filter(
      (task) =>
        (!status || task.status === status) &&
        (!needle || task.title.toLowerCase().includes(needle)),
    );
  }, [data, status, query]);

  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  if (isError) {
    return (
      <ErrorState
        title="Could not load tasks"
        message="The API did not answer GET /api/v1/tasks."
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <Panel
      title="Tasks"
      count={`${rows.length}${rows.length === total ? "" : ` of ${total}`}`}
      className="min-h-0 flex-1 border border-line"
      bodyClassName="min-h-0 flex flex-col"
      right={
        <>
          <TextInput
            value={search}
            onChange={setSearch}
            type="search"
            label="Filter by title"
            placeholder="Filter titles…"
            className="w-40"
          />
          <Select
            value={status}
            onChange={setStatus}
            options={STATUS_OPTIONS}
            label="Filter by status"
          />
          <Link
            href="/tasks/new"
            className="rounded border border-running/50 bg-running/15 px-2 py-1 text-xs text-running transition-colors hover:bg-running/25"
          >
            New task
          </Link>
        </>
      }
    >
      {isPending ? (
        <Skeleton rows={8} />
      ) : rows.length === 0 ? (
        <Empty
          action={
            <Link
              href="/tasks/new"
              className="rounded border border-line px-2 py-1 text-xs transition-colors hover:bg-raised"
            >
              Create one
            </Link>
          }
        >
          {total === 0
            ? "No tasks yet."
            : "No task on this page matches the current filters."}
        </Empty>
      ) : rows.length > VIRTUALIZE_ABOVE ? (
        <div className="min-h-0 flex-1">
          <VirtualList
            items={rows}
            rowHeight={ROW_HEIGHT}
            ariaLabel="Tasks"
            renderRow={(task) => <VirtualRow task={task} />}
          />
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto">
          <Table caption="Every task on this page">
            <THead>
              <TH>Title</TH>
              <TH>Status</TH>
              <TH>Outcome</TH>
              <TH numeric>Duration</TH>
              <TH numeric>Created</TH>
              <TH numeric>Updated</TH>
              <TH />
            </THead>
            <TBody>
              {rows.map((task) => {
                const outcome = outcomeOf(task);
                return (
                  <TR key={task.id}>
                    <TD className="max-w-0">
                      <Link
                        href={`/tasks/${task.id}`}
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
                          pulse={isActiveStatus(task.status)}
                          label={statusLabel(task.status)}
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
                    <TD numeric title={formatAbsolute(task.created_at)}>
                      {formatRelative(task.created_at)}
                    </TD>
                    <TD numeric title={formatAbsolute(task.updated_at)}>
                      {formatRelative(task.updated_at)}
                    </TD>
                    <TD className="text-right">
                      <Link
                        href={`/runs/${task.id}`}
                        className="rounded border border-line px-1.5 py-0.5 text-[11px] transition-colors hover:bg-raised"
                      >
                        Open run
                      </Link>
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        </div>
      )}

      <footer className="flex shrink-0 items-center justify-between border-t border-line px-3 py-1.5 text-[11px] text-muted">
        <span className="tnum">
          Page {page + 1} of {pages}
          {isPlaceholderData && " · loading…"}
        </span>
        <span className="flex gap-1.5">
          <Button disabled={page === 0} onClick={() => setPage((n) => Math.max(0, n - 1))}>
            Previous
          </Button>
          <Button
            disabled={page + 1 >= pages}
            onClick={() => setPage((n) => n + 1)}
          >
            Next
          </Button>
        </span>
      </footer>
    </Panel>
  );
}

/** The virtualized row. Outside a `<table>`, so it is a grid of spans with explicit roles. */
function VirtualRow({ task }: { task: { id: string; title: string; status: string; updated_at: string } }) {
  return (
    <div className="flex items-center gap-2 border-b border-line/60 px-3 text-xs" style={{ height: ROW_HEIGHT }}>
      <Dot tone={toneForStatus(task.status)} pulse={isActiveStatus(task.status)} />
      <Link
        href={`/tasks/${task.id}`}
        className="min-w-0 flex-1 truncate transition-colors hover:text-running"
      >
        {task.title}
      </Link>
      <span className="w-24 shrink-0 text-right text-muted">{statusLabel(task.status)}</span>
      <span className="tnum w-20 shrink-0 text-right text-idle">
        {formatRelative(task.updated_at)}
      </span>
    </div>
  );
}
