/**
 * Gate metadata, transcribed from `AGENTS.md` §9.
 *
 * A static domain table, exempt from §4.1 under §4.5 — it describes what a gate *means*
 * to an operator, which is not a payload the backend sends. What the backend does send is
 * the gate's name, and that is the key here.
 *
 * `decisions` is split deliberately. `RunApproveRequest` validates
 * `^(approve|reject)$` today, so anything else in `AGENTS.md`'s Options column is listed
 * in `specifiedDecisions` and rendered nowhere: §8.6 requires those be *hidden*, not
 * shown-and-broken, until B10 widens the schema. A button that posts a decision the API
 * rejects with a 422 is worse than a button that is not there.
 */

import type { GateDecision, GateName } from "./types";

export interface GateMeta {
  gate: GateName;
  label: string;
  /** When the graph reaches it. */
  fires: string;
  /** What the dialog must render, from §9's "Payload shown" column. */
  shows: string;
  /** The consequence of ticking this gate, stated on the checkbox (§8.4). */
  consequence: string;
  /** Decisions `RunApproveRequest` accepts today. */
  decisions: readonly GateDecision[];
  /** Decisions §9 specifies that the API does not accept yet (§15, B10). */
  specifiedDecisions: readonly string[];
}

/** Default gate expiry. `HITL_GATE_TIMEOUT_S`, 30 minutes (AGENTS.md §9). */
export const GATE_TIMEOUT_S = 1800;

/**
 * The sentence every gate control must carry.
 *
 * An operator who ticks a gate and walks away has cancelled their own run, and that is
 * not something to discover from a report the next morning.
 */
export const GATE_TIMEOUT_NOTE =
  "Gates expire after 30 minutes, and expiry is treated as rejection: the run terminates CANCELLED.";

export const GATES: readonly GateMeta[] = [
  {
    gate: "after_plan",
    label: "after_plan",
    fires: "Plan validated, before any execution",
    shows: "Steps, criteria, dataset bindings, assumptions",
    consequence: "Pause after planning, before any work",
    decisions: ["approve", "reject"],
    specifiedDecisions: ["edit_criteria"],
  },
  {
    gate: "before_sandbox_exec",
    label: "before_sandbox_exec",
    fires: "Code generated, before container launch",
    shows: "Full source, validation report, profile, limits",
    consequence: "Review code before it runs",
    decisions: ["approve", "reject"],
    specifiedDecisions: ["approve_once"],
  },
  {
    gate: "before_model_registration",
    label: "before_model_registration",
    fires: "Criteria met, before the MLflow registry write",
    shows: "Metrics, model size, comparison to the current champion",
    consequence: "Approve registry writes",
    decisions: ["approve", "reject"],
    specifiedDecisions: ["skip_registration"],
  },
  {
    gate: "on_replan",
    label: "on_replan",
    fires: "The Evaluator returned REPLAN",
    shows: "Failure history, the proposed new direction",
    consequence: "Approve a change of approach",
    decisions: ["approve", "reject"],
    specifiedDecisions: ["abort"],
  },
];

const BY_NAME = new Map(GATES.map((meta) => [meta.gate, meta]));

export function gateMeta(gate: string): GateMeta | undefined {
  return BY_NAME.get(gate as GateName);
}
