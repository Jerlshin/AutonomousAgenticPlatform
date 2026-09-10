"use client";

import clsx from "clsx";
import { memo } from "react";
import { formatDuration } from "@/lib/format";
import { NODE_HEIGHT, NODE_WIDTH, type GraphNodeDef } from "@/lib/graphTopology";
import type { GraphNodeState } from "@/lib/types";

/**
 * One node of the live graph (§8.5.3).
 *
 * Rendered as an SVG `<g>` whose *attributes* change with state — never remounted. That
 * is the whole point of the fixed layout: a class swap on an element that already exists
 * transitions, and a subtree that is replaced flickers.
 *
 * State is never carried by colour alone (§10). Each node shows a glyph, and the visit
 * badge and duration are text, so the graph is legible in monochrome and to a screen
 * reader reading the off-screen state list the pane also renders.
 */

const STATUS_FILL: Record<GraphNodeState["status"] | "pending", string> = {
  pending: "fill-[var(--color-surface)] stroke-[var(--color-idle)]",
  running: "fill-[color-mix(in_srgb,var(--color-running)_18%,var(--color-surface))] stroke-[var(--color-running)]",
  succeeded: "fill-[color-mix(in_srgb,var(--color-ok)_12%,var(--color-surface))] stroke-[var(--color-ok)]",
  degraded: "fill-[color-mix(in_srgb,var(--color-warn)_12%,var(--color-surface))] stroke-[var(--color-warn)]",
  failed: "fill-[color-mix(in_srgb,var(--color-fail)_12%,var(--color-surface))] stroke-[var(--color-fail)]",
};

const STATUS_TEXT: Record<GraphNodeState["status"] | "pending", string> = {
  pending: "fill-[var(--color-idle)]",
  running: "fill-[var(--color-fg)]",
  succeeded: "fill-[var(--color-fg)]",
  degraded: "fill-[var(--color-warn)]",
  failed: "fill-[var(--color-fail)]",
};

const STATUS_GLYPH: Record<GraphNodeState["status"] | "pending", string> = {
  pending: "",
  running: "▶",
  succeeded: "✓",
  degraded: "⚠",
  failed: "✗",
};

export const GraphNode = memo(function GraphNode({
  def,
  state,
  active,
  selected,
  gateWaiting,
  onSelect,
  onHover,
}: {
  def: GraphNodeDef;
  state: GraphNodeState | undefined;
  active: boolean;
  selected: boolean;
  /** The `hitl_gate` marker pulses `warn` while a gate is open (§8.5.3). */
  gateWaiting: boolean;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
}) {
  const status = gateWaiting && def.kind === "gate" ? "degraded" : (state?.status ?? "pending");
  const visits = state?.visits ?? 0;
  const x = def.x - NODE_WIDTH / 2;
  const y = def.y - NODE_HEIGHT / 2;

  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={`${def.label}, ${describe(status, state, gateWaiting)}`}
      onClick={() => onSelect(def.id)}
      onKeyDown={(cause) => {
        if (cause.key === "Enter" || cause.key === " ") {
          cause.preventDefault();
          onSelect(def.id);
        }
      }}
      onMouseEnter={() => onHover(def.id)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(def.id)}
      onBlur={() => onHover(null)}
      className={clsx(
        "cursor-pointer outline-none",
        // The scale is applied to the group, so the node grows about its own centre
        // rather than sliding — and it transitions, because nothing here is remounted.
        "transition-transform duration-150",
        (active || gateWaiting) && "[transform-box:fill-box] [transform-origin:center]",
      )}
      style={active || gateWaiting ? { transform: "scale(1.04)" } : undefined}
    >
      {(active || gateWaiting) && (
        <rect
          x={x - 4}
          y={y - 4}
          width={NODE_WIDTH + 8}
          height={NODE_HEIGHT + 8}
          rx={6}
          className={clsx(
            "pulse fill-none",
            gateWaiting ? "stroke-[var(--color-warn)]" : "stroke-[var(--color-running)]",
          )}
          strokeWidth={1}
          opacity={0.5}
        />
      )}

      <rect
        x={x}
        y={y}
        width={NODE_WIDTH}
        height={NODE_HEIGHT}
        rx={4}
        strokeWidth={selected ? 2 : 1}
        strokeDasharray={def.synthetic ? "3 3" : undefined}
        className={clsx(
          "transition-colors duration-150",
          STATUS_FILL[status],
          status === "pending" && "opacity-40",
        )}
      />

      <text
        x={def.x}
        y={def.y + 4}
        textAnchor="middle"
        className={clsx("font-mono text-[10px] transition-colors", STATUS_TEXT[status])}
      >
        {STATUS_GLYPH[status] && (
          <tspan className="text-[9px]">{STATUS_GLYPH[status]} </tspan>
        )}
        {def.label}
      </text>

      {visits > 1 && (
        <>
          <circle
            cx={x + NODE_WIDTH - 5}
            cy={y + 5}
            r={8}
            className="fill-[var(--color-raised)] stroke-[var(--color-line)]"
            strokeWidth={1}
          />
          <text
            x={x + NODE_WIDTH - 5}
            y={y + 8}
            textAnchor="middle"
            className="fill-[var(--color-fg)] font-mono text-[8px]"
          >
            ×{visits}
          </text>
        </>
      )}

      {state?.lastDurationMs !== undefined && status !== "running" && (
        <text
          x={def.x}
          y={y + NODE_HEIGHT + 9}
          textAnchor="middle"
          className="fill-[var(--color-idle)] font-mono text-[8px]"
        >
          {formatDuration(state.lastDurationMs)}
        </text>
      )}
    </g>
  );
});

function describe(
  status: string,
  state: GraphNodeState | undefined,
  gateWaiting: boolean,
): string {
  if (gateWaiting) return "a gate is waiting for a decision";
  if (status === "pending") return "not visited";
  const visits = state?.visits ?? 1;
  const suffix = visits > 1 ? `, visited ${visits} times` : "";
  if (status === "running") return `running${suffix}`;
  if (status === "failed") return `failed${suffix}: ${state?.lastError ?? "unknown error"}`;
  if (status === "degraded") return `completed on its fallback path${suffix}`;
  return `succeeded${suffix}`;
}
