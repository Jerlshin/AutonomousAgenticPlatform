"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { compareRuns, computeKpis } from "@/lib/benchmarks";
import { qk } from "@/lib/queryKeys";
import {
  Banner,
  Button,
  Chip,
  Empty,
  ErrorState,
  Panel,
  Skeleton,
} from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { KpiScorecard } from "./KpiScorecard";
import { SuiteResultsTable } from "./SuiteResultsTable";

/**
 * `/benchmarks` (§8.10): suite scorecards, per-case results, run-over-run comparison.
 *
 * The polling rule is the subtle part. `POST /benchmarks/{suite}/run` returns `202` and
 * the cases that *will* run; the suite then executes in a background task with no
 * progress channel, so the only way to know it finished is that the recorded result count
 * stops growing. §8.10 specifies exactly that: disable the trigger while in flight and
 * poll `benchmarkResults` at 30 s until the count stabilises.
 *
 * Each case executes the whole graph, so a suite takes minutes. A tighter poll would not
 * make it finish sooner — it would just add requests to a box already running the models.
 */

const POLL_MS = 30_000;

/** How many stable polls end the watch. Two, so one slow case does not look like the end. */
const STABLE_POLLS_TO_STOP = 2;

export function BenchmarksClient() {
  const [suite, setSuite] = useState("");

  const suites = useQuery({
    queryKey: qk.benchmarks,
    queryFn: () => api.listBenchmarks(),
    staleTime: 60_000,
  });

  // Pick the first suite once they load, without stomping an operator's choice.
  useEffect(() => {
    if (suite === "" && suites.data && suites.data.suites.length > 0) {
      setSuite(suites.data.suites[0]!.name);
    }
  }, [suite, suites.data]);

  if (suites.isPending) {
    return (
      <Panel title="Benchmarks" className="border border-line">
        <Skeleton rows={6} />
      </Panel>
    );
  }

  if (suites.isError) {
    return (
      <ErrorState
        title="Could not load the benchmark suites"
        message="The API did not answer GET /api/v1/benchmarks."
        onRetry={() => void suites.refetch()}
      />
    );
  }

  const list = suites.data?.suites ?? [];
  if (list.length === 0) {
    return (
      <Panel title="Benchmarks" className="border border-line">
        <Empty>
          No suites are defined. Suites are loaded from{" "}
          <code className="font-mono">benchmarks/suites/</code> on the API host.
        </Empty>
      </Panel>
    );
  }

  const selected = list.find((entry) => entry.name === suite) ?? list[0]!;

  return (
    <div className="flex flex-col gap-3">
      <SuiteHeader
        suites={list.map((entry) => ({
          value: entry.name,
          label: `${entry.name} · v${entry.version}`,
        }))}
        suite={selected.name}
        onSuiteChange={setSuite}
        description={selected.description ?? ""}
        caseCount={selected.cases?.length ?? 0}
      />
      <SuiteBoard
        key={selected.name}
        suite={selected.name}
        trapIds={
          new Set(
            (selected.cases ?? [])
              .filter((entry) => entry.trap === true)
              .map((entry) => entry.id),
          )
        }
      />
    </div>
  );
}

function SuiteHeader({
  suites,
  suite,
  onSuiteChange,
  description,
  caseCount,
}: {
  suites: { value: string; label: string }[];
  suite: string;
  onSuiteChange: (value: string) => void;
  description: string;
  caseCount: number;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Select
        label="Benchmark suite"
        value={suite}
        onChange={onSuiteChange}
        options={suites}
        className="text-sm"
      />
      <span className="text-xs text-muted">{description}</span>
      <span className="ml-auto text-[11px] text-idle">
        {caseCount} case{caseCount === 1 ? "" : "s"} defined
      </span>
    </div>
  );
}

/** One suite's results, KPIs and trigger. Remounted per suite so the poll state resets. */
function SuiteBoard({ suite, trapIds }: { suite: string; trapIds: Set<string> }) {
  const queryClient = useQueryClient();
  const [watching, setWatching] = useState(false);
  const stableRef = useRef({ total: -1, polls: 0 });

  const results = useQuery({
    queryKey: qk.benchmarkResults(suite),
    queryFn: () => api.benchmarkResults(suite),
    staleTime: 10_000,
    refetchInterval: watching ? POLL_MS : false,
    // A suite that has never run answers 404. That is an empty state, not a failure, so
    // it must not be retried into a 404 storm (§2.4).
    retry: (count, error) =>
      error instanceof ApiError && error.status === 404 ? false : count < 2,
  });

  const total = results.data?.total ?? -1;

  // Stop watching once the recorded count holds still across consecutive polls.
  useEffect(() => {
    if (!watching || total < 0) return;
    const stable = stableRef.current;
    if (total === stable.total) {
      stable.polls += 1;
      if (stable.polls >= STABLE_POLLS_TO_STOP) setWatching(false);
    } else {
      stable.total = total;
      stable.polls = 0;
    }
  }, [watching, total, results.dataUpdatedAt]);

  const trigger = useMutation({
    mutationFn: () => api.runBenchmarkSuite(suite),
    onSuccess: () => {
      stableRef.current = { total: -1, polls: 0 };
      setWatching(true);
      void queryClient.invalidateQueries({ queryKey: qk.benchmarkResults(suite) });
    },
  });

  const comparison = useMemo(
    () => compareRuns(results.data?.results ?? []),
    [results.data],
  );

  // The previous execution's KPIs, recomputed client-side — the API scores only the
  // latest per case. See the header of lib/benchmarks.ts.
  const previousKpis = useMemo(
    () => (comparison.hasPrevious ? computeKpis(comparison.previous) : null),
    [comparison],
  );

  const notRun =
    results.isError &&
    results.error instanceof ApiError &&
    results.error.status === 404;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          onClick={() => trigger.mutate()}
          disabled={trigger.isPending || watching}
          tone="primary"
          title={
            watching
              ? "A suite execution is in flight; the results below refresh every 30 seconds."
              : `Execute every case in ${suite}`
          }
        >
          {trigger.isPending
            ? "Starting…"
            : watching
              ? "Running…"
              : "Run suite"}
        </Button>

        {watching && (
          <Chip tone="running">
            polling every {POLL_MS / 1000}s · {results.data?.total ?? 0} recorded
          </Chip>
        )}

        {!watching && results.data && (
          <span className="text-[11px] text-idle">
            {results.data.total} recorded result
            {results.data.total === 1 ? "" : "s"}
          </span>
        )}

        <Button
          onClick={() => void results.refetch()}
          disabled={results.isFetching}
          className="ml-auto"
        >
          {results.isFetching ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {trigger.isError && (
        <Banner tone="fail" className="rounded border">
          Could not start the suite:{" "}
          {trigger.error instanceof Error ? trigger.error.message : "unknown error"}
        </Banner>
      )}

      {trigger.isSuccess && watching && (
        <Banner tone="running" className="rounded border">
          {trigger.data.message} Each case executes the full graph, so this takes minutes;
          the table below refreshes on its own.
        </Banner>
      )}

      {notRun ? (
        <Panel title="Results" className="border border-line">
          <Empty>
            This suite has never been run, so there are no recorded results to score.
          </Empty>
        </Panel>
      ) : results.isPending ? (
        <Panel title="Results" className="border border-line">
          <Skeleton rows={6} />
        </Panel>
      ) : results.isError ? (
        <ErrorState
          title="Could not load results"
          message={`The API did not answer GET /api/v1/benchmarks/${suite}/results.`}
          onRetry={() => void results.refetch()}
        />
      ) : (
        <>
          <KpiScorecard
            kpis={results.data.kpis}
            previous={previousKpis}
            traps={comparison.latest.filter(
              (row) => trapIds.has(row.case_id) || row.case_id.endsWith("-trap"),
            )}
          />

          {comparison.regressions.length > 0 && (
            <Banner tone="fail" className="rounded border">
              {comparison.regressions.length} case
              {comparison.regressions.length === 1 ? "" : "s"} regressed since the previous
              execution:{" "}
              <span className="font-mono">
                {comparison.regressions.map((entry) => entry.caseId).join(", ")}
              </span>
            </Banner>
          )}

          {!comparison.hasPrevious && (
            <p className="text-[11px] text-idle">
              This suite has one recorded execution, so there is nothing to compare against
              yet. Run it again to see per-case transitions and KPI deltas.
            </p>
          )}

          <SuiteResultsTable cases={comparison.cases} trapIds={trapIds} />
        </>
      )}
    </div>
  );
}
