/**
 * The agent graph, hand laid out (§8.5.3).
 *
 * Transcribed from `AGENTS.md` §4.1 and `backend/app/engine/graph.py`. It is a static
 * domain table under §4.5 — a node's *coordinates* are a design decision, not a payload —
 * and `tests/contract/graphTopology.test.ts` asserts the node and edge sets here match
 * the fixture exported from the compiled backend graph, so the one thing that is a
 * backend fact stays checked by data rather than by vigilance.
 *
 * **Why fixed coordinates and not a layout engine.** The topology is static and known at
 * build time: ten nodes and a fixed edge set. A runtime layout — Mermaid, dagre, anything
 * that re-parses — replaces the DOM subtree per render, which at one `node.started` every
 * few seconds is a visible stutter and forbids any CSS transition. State changes here are
 * class and attribute changes on elements that already exist, so a node *lights up*
 * instead of the graph flickering. This is the deviation recorded as D1 in §16.
 *
 * The layout follows the run's own shape: the spine down the middle is the happy path
 * (`init → planner → coder → sandbox_exec → evaluator → reporter → finalizer`), the
 * left column is where a run goes when it needs to learn something (`researcher`) or fix
 * something (`debugger`), and the right column is where it goes when it succeeded
 * (`mlops`). Loop 1 — `coder → sandbox_exec → debugger → coder` — is therefore a visible
 * cycle on the left rather than a line crossing the diagram.
 */

import type { RunPhase } from "./types";

export interface GraphNodeDef {
  /** The backend's node name. The key everything else joins on. */
  id: string;
  label: string;
  phase: RunPhase;
  kind: "llm" | "deterministic" | "gate";
  /** Display default. Overridden by `run.started`'s `model_routing` when it arrives. */
  model?: string;
  /** Fixed layout coordinates, in the SVG's own units. */
  x: number;
  y: number;
  /**
   * Not a node of the compiled graph.
   *
   * `hitl_gate` is an `interrupt_before` marker (AGENTS.md §9), not a registered node, so
   * the contract test excludes it. It is drawn because §8.5.3 requires a gate to pulse
   * somewhere the operator can click.
   */
  synthetic?: boolean;
}

export interface GraphEdgeDef {
  from: string;
  to: string;
  kind: "normal" | "loop" | "failure" | "bailout";
  /** The routing predicate, from AGENTS.md §5. Shown on hover. */
  label?: string;
}

export const NODE_WIDTH = 92;
export const NODE_HEIGHT = 26;
export const VIEWBOX_WIDTH = 330;
export const VIEWBOX_HEIGHT = 540;

export const GRAPH_NODES: readonly GraphNodeDef[] = [
  { id: "init", label: "init", phase: "INIT", kind: "deterministic", x: 165, y: 26 },
  {
    id: "planner",
    label: "planner",
    phase: "PLANNING",
    kind: "llm",
    model: "qwen2.5:14b-instruct",
    x: 165,
    y: 88,
  },
  {
    id: "researcher",
    label: "researcher",
    phase: "RESEARCH",
    kind: "llm",
    model: "llama3.1:8b",
    x: 55,
    y: 150,
  },
  {
    id: "coder",
    label: "coder",
    phase: "IMPLEMENT",
    kind: "llm",
    model: "qwen2.5-coder:7b",
    x: 165,
    y: 212,
  },
  {
    id: "hitl_gate",
    label: "gate",
    phase: "IMPLEMENT",
    kind: "gate",
    x: 285,
    y: 243,
    synthetic: true,
  },
  {
    id: "sandbox_exec",
    label: "sandbox_exec",
    phase: "EXECUTE",
    kind: "deterministic",
    x: 165,
    y: 274,
  },
  {
    id: "debugger",
    label: "debugger",
    phase: "DEBUG",
    kind: "llm",
    model: "qwen2.5-coder:7b",
    x: 55,
    y: 336,
  },
  { id: "mlops", label: "mlops", phase: "TRACK", kind: "deterministic", x: 275, y: 336 },
  {
    id: "evaluator",
    label: "evaluator",
    phase: "EVALUATE",
    kind: "llm",
    model: "llama3.1:8b",
    x: 165,
    y: 398,
  },
  {
    id: "reporter",
    label: "reporter",
    phase: "REPORT",
    kind: "llm",
    model: "llama3.1:8b",
    x: 165,
    y: 460,
  },
  {
    id: "finalizer",
    label: "finalizer",
    phase: "COMPLETE",
    kind: "deterministic",
    x: 165,
    y: 514,
  },
];

export const GRAPH_EDGES: readonly GraphEdgeDef[] = [
  { from: "init", to: "planner", kind: "normal" },
  { from: "planner", to: "researcher", kind: "normal", label: "context needed" },
  { from: "planner", to: "coder", kind: "normal", label: "implement step" },
  { from: "planner", to: "evaluator", kind: "normal", label: "evaluate step" },
  { from: "planner", to: "reporter", kind: "bailout", label: "no plan" },
  { from: "researcher", to: "researcher", kind: "loop", label: "insufficient context" },
  { from: "researcher", to: "coder", kind: "normal" },
  { from: "coder", to: "sandbox_exec", kind: "normal" },
  { from: "coder", to: "reporter", kind: "bailout", label: "budget spent" },
  { from: "sandbox_exec", to: "debugger", kind: "failure", label: "non-CLEAN" },
  { from: "sandbox_exec", to: "mlops", kind: "normal", label: "CLEAN · train step" },
  { from: "sandbox_exec", to: "evaluator", kind: "normal", label: "CLEAN" },
  { from: "sandbox_exec", to: "reporter", kind: "bailout", label: "budget spent" },
  { from: "debugger", to: "coder", kind: "loop", label: "loop 1" },
  { from: "debugger", to: "researcher", kind: "loop", label: "needs an API" },
  { from: "debugger", to: "planner", kind: "loop", label: "stagnation · replan" },
  { from: "debugger", to: "reporter", kind: "bailout", label: "budget spent" },
  { from: "mlops", to: "evaluator", kind: "normal" },
  { from: "evaluator", to: "coder", kind: "loop", label: "REFINE · loop 2" },
  { from: "evaluator", to: "planner", kind: "loop", label: "REPLAN · loop 3" },
  { from: "evaluator", to: "reporter", kind: "normal", label: "ACCEPT · ABORT" },
  { from: "reporter", to: "finalizer", kind: "normal" },
];

const BY_ID = new Map(GRAPH_NODES.map((node) => [node.id, node]));

export function graphNode(id: string): GraphNodeDef | undefined {
  return BY_ID.get(id);
}

/** The edge key the store uses in `edgeTraversals`. */
export function edgeKey(edge: { from: string; to: string }): string {
  return `${edge.from}→${edge.to}`;
}

/**
 * The SVG path for one edge.
 *
 * Four shapes, because a graph with three cycles drawn entirely in straight lines is a
 * diagram nobody can read:
 *
 * * **normal** — a gentle vertical bezier down the spine;
 * * **loop** — a back edge, bowed out to whichever side has room, so
 *   `debugger → coder` and `evaluator → planner` do not overlap the spine they run
 *   beside;
 * * **failure** and **bailout** — the same geometry as normal and loop respectively; they
 *   differ only in tone, because *where* control went and *why* are separate questions.
 */
export function edgePath(edge: GraphEdgeDef): string {
  const from = BY_ID.get(edge.from);
  const to = BY_ID.get(edge.to);
  if (!from || !to) return "";

  const half = NODE_HEIGHT / 2;

  // A self-loop: a small arc off the node's left shoulder.
  if (edge.from === edge.to) {
    const x = from.x - NODE_WIDTH / 2;
    const y = from.y;
    return `M ${x} ${y - 6} C ${x - 34} ${y - 20}, ${x - 34} ${y + 20}, ${x} ${y + 6}`;
  }

  const downward = to.y > from.y;
  const startY = downward ? from.y + half : from.y - half;
  const endY = downward ? to.y - half : to.y + half;

  if (downward && Math.abs(to.x - from.x) < 4) {
    // Straight down the spine.
    return `M ${from.x} ${startY} L ${to.x} ${endY}`;
  }

  if (downward) {
    // A branch off the spine: bow through the midpoint so the curve leaves the node
    // vertically and arrives vertically, rather than clipping the node's corner.
    const midY = (startY + endY) / 2;
    return `M ${from.x} ${startY} C ${from.x} ${midY}, ${to.x} ${midY}, ${to.x} ${endY}`;
  }

  // A back edge. Bow out to the side furthest from the spine so the three loops stay
  // distinguishable from each other and from the forward path they parallel.
  const side = from.x <= VIEWBOX_WIDTH / 2 ? -1 : 1;
  const bow = side * (46 + Math.abs(from.y - to.y) / 12);
  const control = (from.x + to.x) / 2 + bow;
  return `M ${from.x} ${startY} C ${control} ${startY - 10}, ${control} ${endY + 10}, ${to.x} ${endY}`;
}
