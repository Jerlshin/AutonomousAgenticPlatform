"use client";

/**
 * Selector helpers over the run store (§6.5).
 *
 * The rule these enforce: **a pane subscribes to a slice, never to the store.** The bare
 * `useRunStore()` call subscribes to everything, so every event re-renders every pane —
 * precisely the failure Zustand was chosen to avoid.
 *
 * Two mechanical consequences, both of which are easy to get wrong:
 *
 * * multi-field selections go through `useShallow`, or the new object identity
 *   re-renders on every `set`;
 * * a selector that derives an array must be memoized *outside* the component, because
 *   one that `.filter()`s inside the component body returns a new array on every call
 *   and defeats the comparison it was written to satisfy.
 *
 * The selectors below are module constants for exactly that reason. Panes import the one
 * they need rather than writing an inline arrow.
 */

import {
  useRunStore,
  useRunStoreShallow,
} from "@/stores/RunStoreProvider";
import type { RunStreamState } from "@/stores/runStore";

/** Subscribe to one derived value. Identity-compared, so return a scalar or a stable ref. */
export function useRunSlice<T>(selector: (state: RunStreamState) => T): T {
  return useRunStore(selector);
}

/** Subscribe to several fields at once, shallow-compared. */
export function useRunShallow<T extends Record<string, unknown>>(
  selector: (state: RunStreamState) => T,
): T {
  return useRunStoreShallow(selector);
}

// ── Pane selectors ────────────────────────────────────────────────────────────

export const selectGraph = (state: RunStreamState) => ({
  activeNode: state.activeNode,
  nodeState: state.nodeState,
  edgeTraversals: state.edgeTraversals,
  lastEdge: state.lastEdge,
  phase: state.phase,
  terminal: state.terminal,
  modelRouting: state.modelRouting,
  pendingGate: state.pendingGate,
});

export const selectTimeline = (state: RunStreamState) => ({
  timeline: state.timeline,
  planSteps: state.planSteps,
  gateHistory: state.gateHistory,
  historyComplete: state.historyComplete,
  oldestAvailable: state.oldestAvailable,
});

export const selectCriteria = (state: RunStreamState) => ({
  criteria: state.criteria,
  verdict: state.verdict,
  planRevision: state.planRevision,
});

export const selectConsole = (state: RunStreamState) => ({
  consoleLines: state.consoleLines,
  droppedConsoleLines: state.droppedConsoleLines,
  truncations: state.truncations,
  executions: state.executions,
  terminal: state.terminal,
});

export const selectArtifacts = (state: RunStreamState) => ({
  artifacts: state.artifacts,
  metrics: state.metrics,
  mlflow: state.mlflow,
  bundleUrl: state.bundleUrl,
  terminalPayload: state.terminalPayload,
});

export const selectHeader = (state: RunStreamState) => ({
  run: state.run,
  status: state.status,
  phase: state.phase,
  phaseHistory: state.phaseHistory,
  terminal: state.terminal,
  outcome: state.outcome,
  activeNode: state.activeNode,
  usage: state.usage,
  budgets: state.budgets,
  queuePosition: state.queuePosition,
  historyComplete: state.historyComplete,
  error: state.error,
});

export const selectGate = (state: RunStreamState) => ({
  pendingGate: state.pendingGate,
  gateHistory: state.gateHistory,
  codeRevisions: state.codeRevisions,
  planSteps: state.planSteps,
  criteria: state.criteria,
});

export const selectCode = (state: RunStreamState) => ({
  codeRevisions: state.codeRevisions,
  diagnoses: state.diagnoses,
  executions: state.executions,
  historyComplete: state.historyComplete,
});

/**
 * Counts for the pane headers (§8.5.1: "pane headers MUST carry a live count").
 *
 * Separated from the data selectors so a header re-renders when a count changes without
 * subscribing to the array whose length produced it.
 */
export const selectCounts = (state: RunStreamState) => ({
  timeline: state.timeline.length,
  console: state.consoleLines.length,
  droppedConsole: state.droppedConsoleLines,
  artifacts: state.artifacts.length,
  criteria: state.criteria.length,
  events: state.events.length,
  droppedEvents: state.droppedEvents,
});
