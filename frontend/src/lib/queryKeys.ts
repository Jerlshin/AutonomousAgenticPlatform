/**
 * The TanStack Query key registry.
 *
 * docs/FRONTEND.md §6.2: all keys MUST come from here. Ad-hoc key arrays scattered
 * through components make invalidation unreviewable — the question "what does completing
 * a run invalidate?" has to be answerable by reading one file, and the answer has to stay
 * true when a component is copied.
 *
 * Keys are `as const` tuples so `queryClient.invalidateQueries({queryKey: qk.task(id)})`
 * type-checks against what the query actually registered.
 */

export const qk = {
  health: ["health"] as const,
  healthDeep: ["health", "deep"] as const,

  tasks: (p: { skip: number; limit: number }) => ["tasks", p] as const,
  task: (id: string) => ["task", id] as const,
  taskRuns: (id: string) => ["task", id, "runs"] as const,

  run: (id: string) => ["run", id] as const,
  runEvents: (id: string, after: number) => ["run", id, "events", after] as const,

  corpusDocs: (p: { skip: number; limit: number; collection?: string }) =>
    ["corpus", "documents", p] as const,
  corpusSearch: (body: unknown) => ["corpus", "search", body] as const,

  benchmarks: ["benchmarks"] as const,
  benchmarkResults: (suite: string) => ["benchmarks", suite, "results"] as const,
} as const;

/** The prefix every task-list key starts with, for invalidating all pages at once. */
export const TASKS_PREFIX = ["tasks"] as const;
