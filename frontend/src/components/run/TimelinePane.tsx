"use client";

import clsx from "clsx";
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { PlanStepRow, TimelineEntry, Tone } from "@/lib/types";
import { useRunShallow, selectTimeline } from "@/hooks/useRunSlice";
import { useSecondTicker } from "@/hooks/useTicker";
import { Banner, Chip, Empty, Panel } from "@/components/ui/primitives";
import { VirtualList, type VirtualListHandle } from "@/components/ui/virtual-list";
import { CriteriaLedger } from "./CriteriaLedger";
import { useDeckFocus } from "./DeckFocus";
import { TimelineRow, TIMELINE_ROW_HEIGHT } from "./TimelineRow";

/**
 * Pane 2 — the execution timeline, with the criteria ledger pinned beneath it (§8.5.4).
 *
 * Two stacked regions in one pane. The trace scrolls; the ledger does not, because it is
 * never long — a plan carries a handful of criteria — and it is the answer to "is this
 * working", so it must not scroll out of view.
 *
 * Entries are grouped under their plan step when `plan_step_id` is present, with the step
 * title as a sticky sub-header. Grouping is why the list only virtualizes above the §9.2
 * threshold: below it, the flat structure and the group headers coexist cheaply; above
 * it, the trace is long enough that grouping is noise anyway and the virtualized flat
 * list wins.
 */

/** §9.2: virtualize above 200 entries; below that, measurement costs more than it saves. */
const VIRTUALIZE_ABOVE = 200;

export function TimelinePane({ collapsed }: { collapsed?: boolean }) {
  const { timeline, planSteps, gateHistory, historyComplete, oldestAvailable } =
    useRunShallow(selectTimeline);
  const focus = useDeckFocus();
  const now = useSecondTicker();
  const listRef = useRef<VirtualListHandle | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  const stepsById = useMemo(
    () => new Map(planSteps.map((step) => [step.id, step])),
    [planSteps],
  );

  const groups = useMemo(() => groupByStep(timeline, stepsById), [timeline, stepsById]);

  const onSelect = useCallback(
    (entry: TimelineEntry) => {
      // Clicking a `sandbox_exec` entry filters the console to its container; clicking
      // anything else filters to the node (§8.5.4).
      if (entry.executionId) focus.focusExecution(entry.executionId, entry.node);
      else focus.focusEntry(entry.id, entry.node);
    },
    [focus],
  );

  // The graph pane sets `entryId` when a node is clicked; scroll it into view.
  useEffect(() => {
    if (!focus.entryId) return;
    const index = timeline.findIndex((entry) => entry.id === focus.entryId);
    if (index < 0) return;
    if (timeline.length > VIRTUALIZE_ABOVE) {
      listRef.current?.scrollToIndex(index, "center");
      return;
    }
    scrollerRef.current
      ?.querySelector(`[data-entry="${focus.entryId}"]`)
      ?.scrollIntoView({ block: "center" });
  }, [focus.entryId, timeline]);

  if (collapsed) return null;

  const elapsedFor = (entry: TimelineEntry) =>
    entry.status === "running" && now > 0
      ? Math.max(0, (now - Date.parse(entry.startedAt)) / 1000)
      : 0;

  return (
    <Panel
      title="Timeline"
      count={`${timeline.length} step${timeline.length === 1 ? "" : "s"}`}
      className="min-h-0 border-l border-line"
      bodyClassName="min-h-0 flex flex-col"
      right={
        gateHistory.length > 0 && (
          <Chip tone="warn" dot={false}>
            {gateHistory.length} gate{gateHistory.length === 1 ? "" : "s"} decided
          </Chip>
        )
      }
    >
      {!historyComplete && (
        <Banner tone="warn">
          Earlier history was dropped from the server&apos;s retention window
          {oldestAvailable != null && <> (events before seq {oldestAvailable})</>}. This
          trace starts where the retained window does.
        </Banner>
      )}

      <div className="min-h-0 flex-1" ref={scrollerRef}>
        {timeline.length === 0 ? (
          <Empty>No node has started yet.</Empty>
        ) : timeline.length > VIRTUALIZE_ABOVE ? (
          <VirtualList
            items={timeline}
            rowHeight={TIMELINE_ROW_HEIGHT}
            handleRef={listRef}
            ariaLabel="Execution trace"
            followTail
            renderRow={(entry) => (
              <TimelineRow
                entry={entry}
                elapsedSeconds={elapsedFor(entry)}
                selected={focus.entryId === entry.id}
                onSelect={onSelect}
              />
            )}
          />
        ) : (
          <ol className="h-full overflow-auto">
            {groups.map((group) => (
              <li key={group.key}>
                {group.step && (
                  <div className="sticky top-0 z-10 flex items-center gap-2 border-y border-line bg-surface px-3 py-0.5 text-[10px] uppercase tracking-widest text-muted">
                    <span className="truncate">
                      {group.step.index}. {group.step.title}
                    </span>
                    <Chip
                      tone={stepTone(group.step.status)}
                      dot={false}
                      className="ml-auto"
                    >
                      {group.step.status}
                    </Chip>
                  </div>
                )}
                <ol>
                  {group.entries.map((entry) => (
                    <li key={entry.id} data-entry={entry.id}>
                      <TimelineRow
                        entry={entry}
                        elapsedSeconds={elapsedFor(entry)}
                        selected={focus.entryId === entry.id}
                        onSelect={onSelect}
                      />
                    </li>
                  ))}
                </ol>
              </li>
            ))}

            {gateHistory.map((decision, index) => (
              <li
                key={`${decision.gate}-${index}`}
                className={clsx(
                  "flex items-center gap-2 px-3 text-xs leading-6",
                  decision.decision === "approve" ? "text-ok" : "text-fail",
                )}
                style={{ height: TIMELINE_ROW_HEIGHT }}
                title={decision.notes || undefined}
              >
                <span className="w-3 shrink-0 text-center" aria-hidden>
                  ⏸
                </span>
                <span className="font-mono">gate {decision.gate}</span>
                <span className="text-muted">
                  — {decision.decision}d by operator, via {decision.via}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>

      <CriteriaLedger />
    </Panel>
  );
}

interface Group {
  key: string;
  step: PlanStepRow | null;
  entries: TimelineEntry[];
}

/**
 * Group consecutive entries by their plan step.
 *
 * Consecutive, not by-key: loop 1 can return to a step after visiting others, and
 * collapsing those into one group would claim the run did the step once when it did it
 * twice. Entries with no `plan_step_id` — the deterministic nodes, `init` and
 * `finalizer` — fall into an unheaded group rather than being hidden.
 */
function groupByStep(
  timeline: readonly TimelineEntry[],
  steps: Map<string, PlanStepRow>,
): Group[] {
  const groups: Group[] = [];
  for (const entry of timeline) {
    const stepId = entry.planStepId ?? null;
    const last = groups[groups.length - 1];
    if (last && last.key.startsWith(`${stepId ?? "-"}::`)) {
      last.entries.push(entry);
      continue;
    }
    groups.push({
      key: `${stepId ?? "-"}::${entry.id}`,
      step: stepId ? (steps.get(stepId) ?? null) : null,
      entries: [entry],
    });
  }
  return groups;
}

function stepTone(status: PlanStepRow["status"]): Tone {
  switch (status) {
    case "SUCCEEDED":
      return "ok";
    case "RUNNING":
      return "running";
    case "FAILED":
      return "fail";
    case "SKIPPED":
      return "warn";
    default:
      return "idle";
  }
}
