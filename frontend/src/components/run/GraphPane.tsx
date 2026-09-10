"use client";

import clsx from "clsx";
import { useCallback, useMemo, useState } from "react";
import { formatDuration } from "@/lib/format";
import {
  edgeKey,
  edgePath,
  GRAPH_EDGES,
  GRAPH_NODES,
  graphNode,
  VIEWBOX_HEIGHT,
  VIEWBOX_WIDTH,
  type GraphEdgeDef,
} from "@/lib/graphTopology";
import { useRunShallow, selectGraph } from "@/hooks/useRunSlice";
import { useRunStore } from "@/stores/RunStoreProvider";
import { Chip, Panel } from "@/components/ui/primitives";
import { useDeckFocus } from "./DeckFocus";
import { GraphNode } from "./GraphNode";

/**
 * Pane 1 — the agent graph visualizer (§8.5.3).
 *
 * The signature view: which node is executing now, which have completed, which failed,
 * how many times each was visited, and which edge was traversed last.
 *
 * It renders correctly with **zero events**. The topology is known before the run starts,
 * and an empty graph in `idle` tone is a better first paint than a spinner — an operator
 * opening a QUEUED run should see the shape of what is about to happen.
 *
 * Accessibility is not an afterthought here, because an SVG is otherwise completely
 * opaque: the figure carries `role="img"` with an `aria-label` naming the active node and
 * phase, and an off-screen ordered list gives a screen reader every node's state in text.
 */
export function GraphPane({ collapsed }: { collapsed?: boolean }) {
  const { activeNode, nodeState, edgeTraversals, lastEdge, phase, terminal, modelRouting, pendingGate } =
    useRunShallow(selectGraph);
  const timeline = useRunStore((state) => state.timeline);
  const focus = useDeckFocus();
  const [hovered, setHovered] = useState<string | null>(null);

  const onSelect = useCallback(
    (id: string) => {
      if (id === "hitl_gate") {
        // Clicking the gate marker reopens the gate console (§8.5.3) — including one the
        // operator dismissed to go and read the console, which §8.6 explicitly allows.
        focus.raiseGate();
        return;
      }
      // Clicking a node scrolls the timeline to that node's most recent entry and filters
      // the console to it (§8.5.3).
      const entry = [...timeline].reverse().find((row) => row.node === id);
      if (entry) focus.focusEntry(entry.id, id);
      else focus.focusNode(id);
    },
    [focus, timeline],
  );

  const cumulative = useMemo(() => {
    const totals = new Map<string, number>();
    for (const [node, state] of Object.entries(nodeState)) {
      totals.set(node, state.cumulativeMs);
    }
    return totals;
  }, [nodeState]);

  if (collapsed) return null;

  const hoveredNode = hovered ? graphNode(hovered) : undefined;
  const hoveredState = hovered ? nodeState[hovered] : undefined;

  return (
    <Panel
      title="Agent graph"
      count={`${Object.keys(nodeState).length}/${GRAPH_NODES.filter((n) => !n.synthetic).length} visited`}
      className="min-h-0"
      bodyClassName="min-h-0 relative flex flex-col"
      right={phase && <Chip tone={terminal ? "idle" : "running"} dot={false}>{phase}</Chip>}
    >
      <div className="min-h-0 flex-1 overflow-hidden p-2">
        <svg
          viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label={
            terminal
              ? `Agent graph. The run has ended${phase ? ` in phase ${phase}` : ""}.`
              : activeNode
                ? `Agent graph. ${activeNode} is executing${phase ? ` in phase ${phase}` : ""}.`
                : "Agent graph. No node is executing."
          }
          className="h-full w-full"
        >
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--color-line)" />
            </marker>
          </defs>

          <g aria-hidden>
            {GRAPH_EDGES.map((edge) => (
              <Edge
                key={`${edge.from}-${edge.to}`}
                edge={edge}
                traversals={edgeTraversals[edgeKey(edge)] ?? 0}
                isLast={lastEdge === edgeKey(edge)}
              />
            ))}
          </g>

          {GRAPH_NODES.map((def) => (
            <GraphNode
              key={def.id}
              def={def}
              state={nodeState[def.id]}
              active={activeNode === def.id}
              selected={focus.node === def.id}
              gateWaiting={def.id === "hitl_gate" && pendingGate !== null}
              onSelect={onSelect}
              onHover={setHovered}
            />
          ))}
        </svg>
      </div>

      {hoveredNode && (
        <div className="pointer-events-none absolute inset-x-2 bottom-2 rounded border border-line bg-raised px-2 py-1 text-[11px] shadow-xl">
          <span className="font-mono text-fg">{hoveredNode.label}</span>
          <span className="ml-2 text-muted">{hoveredNode.kind}</span>
          {hoveredNode.kind === "llm" && (
            <span className="ml-2 font-mono text-idle">
              {/* The run's own routing wins over the display default: a machine under
                  16 GB substitutes the small ladder, and labelling the node with the
                  table's default would misreport which weights wrote the code. */}
              {modelRouting[hoveredNode.id] ?? hoveredNode.model ?? "—"}
            </span>
          )}
          <span className="ml-2 text-muted">
            {hoveredState
              ? `${hoveredState.visits} visit${hoveredState.visits === 1 ? "" : "s"} · ${formatDuration(
                  cumulative.get(hoveredNode.id) ?? 0,
                )} total`
              : "not visited"}
          </span>
          {hoveredState?.lastSummary && (
            <p className="truncate text-muted">{hoveredState.lastSummary}</p>
          )}
          {hoveredState?.lastError && (
            <p className="truncate text-fail">{hoveredState.lastError}</p>
          )}
        </div>
      )}

      {/* The pane is otherwise opaque to a screen reader: an SVG's shapes carry no
          meaning without this. §8.5.3 requires it explicitly. */}
      <ol className="sr-only">
        {GRAPH_NODES.filter((def) => !def.synthetic).map((def) => {
          const state = nodeState[def.id];
          return (
            <li key={def.id}>
              {def.label}: {state ? state.status : "not visited"}
              {state && state.visits > 1 ? `, ${state.visits} visits` : ""}
              {state?.lastError ? `, error: ${state.lastError}` : ""}
            </li>
          );
        })}
      </ol>
    </Panel>
  );
}

const EDGE_TONE: Record<GraphEdgeDef["kind"], string> = {
  normal: "stroke-[var(--color-line)]",
  loop: "stroke-[var(--color-running)]",
  failure: "stroke-[var(--color-fail)]",
  bailout: "stroke-[var(--color-idle)]",
};

/**
 * One edge.
 *
 * Tinted by traversal count so a loop that ran three times reads differently from one
 * that never ran, and the most recent edge animates its dash offset **once** — a
 * continuously animating edge would be the second permanent animation on screen, which
 * §10 does not allow.
 */
function Edge({
  edge,
  traversals,
  isLast,
}: {
  edge: GraphEdgeDef;
  traversals: number;
  isLast: boolean;
}) {
  const path = edgePath(edge);
  if (!path) return null;

  return (
    <path
      d={path}
      fill="none"
      markerEnd="url(#arrow)"
      strokeWidth={traversals > 0 ? Math.min(1 + traversals * 0.35, 2.5) : 0.75}
      strokeDasharray={edge.kind === "bailout" ? "2 3" : undefined}
      className={clsx(
        "transition-[stroke-width,opacity] duration-150",
        EDGE_TONE[edge.kind],
        traversals === 0 && "opacity-25",
        isLast && "edge-active",
      )}
    >
      {edge.label && <title>{`${edge.from} → ${edge.to}: ${edge.label}`}</title>}
    </path>
  );
}
