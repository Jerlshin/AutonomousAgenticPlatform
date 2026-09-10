/**
 * Feature flags for backend surface that is specified but not implemented.
 *
 * docs/FRONTEND.md §2.4. Every capability that depends on missing backend work is gated
 * here and nowhere else, for two reasons: one place to flip when the endpoint lands, and
 * one symbol to grep for in review. A view that calls a non-existent endpoint
 * speculatively produces a 404 storm that makes real failures invisible.
 *
 * A gated-off feature MUST still render — disabled, with the reason. A greyed
 * **Download bundle** whose tooltip says `GET /runs/{id}/bundle` is not implemented yet
 * is honest; a button that 404s is not, and a silently missing button hides scope.
 *
 * The `REASONS` map exists so that reason is written once next to the flag rather than
 * re-typed into every tooltip.
 */

export const CAPABILITIES = {
  runSteps: false, // GET /runs/{id}/steps
  runArtifacts: false, // GET /runs/{id}/artifacts
  runBundle: false, // GET /runs/{id}/bundle
  runReport: false, // GET /runs/{id}/report
  artifactDownload: false, // GET /artifacts/{id}/download
  agentRegistry: false, // GET /agents
  runConfig: false, // POST /tasks/{id}/runs with a body
  stats: false, // aggregate dashboard statistics
} as const;

export type Capability = keyof typeof CAPABILITIES;

/** Operator-facing explanations. Shown in the tooltip of every disabled affordance. */
export const REASONS: Record<Capability, string> = {
  runSteps: "GET /runs/{id}/steps is not implemented yet (docs/FRONTEND.md §15, B7).",
  runArtifacts:
    "GET /runs/{id}/artifacts is not implemented yet (docs/FRONTEND.md §15, B3).",
  runBundle: "GET /runs/{id}/bundle is not implemented yet (docs/FRONTEND.md §15, B8).",
  runReport: "GET /runs/{id}/report is not implemented yet (docs/FRONTEND.md §15, B4).",
  artifactDownload:
    "GET /artifacts/{id}/download is not implemented yet (docs/FRONTEND.md §15, B3).",
  agentRegistry: "GET /agents is not implemented yet (docs/FRONTEND.md §15, B12).",
  runConfig:
    "POST /tasks/{id}/runs does not accept a configuration body yet (docs/FRONTEND.md §15, B5).",
  stats:
    "There is no aggregate statistics endpoint yet (docs/FRONTEND.md §15, B9); these figures are derived client-side.",
};

export function can(capability: Capability): boolean {
  return CAPABILITIES[capability];
}

/** `undefined` when the capability is available — the shape a `title` attribute wants. */
export function reasonUnless(capability: Capability): string | undefined {
  return CAPABILITIES[capability] ? undefined : REASONS[capability];
}
