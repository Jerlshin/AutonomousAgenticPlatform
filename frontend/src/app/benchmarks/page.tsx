import { BenchmarksClient } from "@/components/benchmarks/BenchmarksClient";

/**
 * `/benchmarks` (§8.10): a Server shell around the scorecard client.
 *
 * Everything on this page is a TanStack Query consumer with local selection state, so the
 * whole body is one client subtree — but the frame still renders on the server (§2.2).
 */
export default function BenchmarksPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4">
      <header>
        <h1 className="text-sm font-semibold text-fg">Benchmarks</h1>
        <p className="text-xs text-muted">
          Suite scorecards against the platform KPIs, and what changed since the last run.
        </p>
      </header>
      <BenchmarksClient />
    </div>
  );
}
