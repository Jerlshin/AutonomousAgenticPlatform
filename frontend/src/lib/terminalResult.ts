/**
 * Reading `RunRead.result` — the terminal truth (§2.3).
 *
 * "Live truth is the event stream, terminal truth is `result`, and REST alone shows
 * neither." `result` stores the `run.completed` payload verbatim: `{status,
 * deliverables[], bundle_url, evaluation, mlflow, usage}`. OpenAPI types it as a bare
 * `object`, so every consumer would otherwise re-derive its shape with its own inline cast
 * — which is how one view ends up reading `criteria_results` and another `criteria`, and
 * neither notices when the payload changes.
 *
 * So the narrowing happens once, here, and it is *narrowing* rather than casting: the
 * payload crosses a JSON boundary from a backend that may be a version ahead, and a
 * missing key must read as absent rather than crash a report. Where the shape is right,
 * the data comes back typed; where it is not, it comes back `null` and every caller
 * already has a path for that.
 *
 * The mirrored source is `worker/jobs._terminal_payload`.
 */

import type { CriterionRow, VerdictRow } from "./types";

export interface TerminalDeliverable {
  artifact_id: string | null;
  name: string;
  artifact_type: "code" | "model" | "plot" | "report" | "metrics" | "log" | "bundle";
  path: string;
  sha256: string;
  size_bytes: number;
  mime_type: string;
}

export interface TerminalMlflow {
  experiment_name: string;
  run_id: string;
  parent_run_id: string | null;
  ui_url: string;
  logged_metrics: Record<string, number>;
}

export interface TerminalResult {
  status: string | null;
  deliverables: TerminalDeliverable[];
  bundle_url: string | null;
  evaluation: TerminalEvaluation | null;
  mlflow: TerminalMlflow | null;
  usage: TerminalUsage | null;
  /** Present only on the failure payload. */
  error: string | null;
  last_node: string | null;
}

export interface TerminalEvaluation {
  decision: string;
  passed: boolean;
  score: number;
  criteria_results: TerminalCriterion[];
  rubric: { dimension: string; score: number; justification: string }[];
  rubric_mean: number | null;
  summary: string;
  replan_directive: string | null;
  refine_directive: string | null;
}

export interface TerminalCriterion {
  criterion_id: string;
  metric: string;
  comparator: string;
  threshold: number;
  observed: number | null;
  passed: boolean;
  required: boolean;
  weight: number;
  note: string;
}

export interface TerminalUsage {
  tokens_in: number;
  tokens_out: number;
  llm_calls: number;
  node_visits: number;
  sandbox_executions: number;
  started_at: string | null;
}

// ── Narrowing helpers ────────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function strOrNull(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

const DELIVERABLE_TYPES = new Set([
  "code",
  "model",
  "plot",
  "report",
  "metrics",
  "log",
  "bundle",
]);

function readDeliverable(value: unknown): TerminalDeliverable | null {
  if (!isRecord(value)) return null;
  const name = str(value.name);
  if (name === "") return null;
  const type = str(value.artifact_type);
  return {
    artifact_id: strOrNull(value.artifact_id),
    name,
    artifact_type: (DELIVERABLE_TYPES.has(type)
      ? type
      : "log") as TerminalDeliverable["artifact_type"],
    path: str(value.path),
    sha256: str(value.sha256),
    size_bytes: num(value.size_bytes),
    mime_type: str(value.mime_type, "application/octet-stream"),
  };
}

function readCriterion(value: unknown): TerminalCriterion | null {
  if (!isRecord(value)) return null;
  const metric = str(value.metric);
  if (metric === "") return null;
  return {
    criterion_id: str(value.criterion_id, metric),
    metric,
    comparator: str(value.comparator, ">="),
    threshold: num(value.threshold),
    observed: numOrNull(value.observed),
    passed: bool(value.passed),
    required: bool(value.required, true),
    weight: num(value.weight, 1),
    note: str(value.note),
  };
}

function readEvaluation(value: unknown): TerminalEvaluation | null {
  if (!isRecord(value)) return null;
  const rows = Array.isArray(value.criteria_results) ? value.criteria_results : [];
  const rubric = Array.isArray(value.rubric) ? value.rubric : [];
  return {
    decision: str(value.decision, "ACCEPT"),
    passed: bool(value.passed),
    score: num(value.score),
    criteria_results: rows
      .map(readCriterion)
      .filter((row): row is TerminalCriterion => row !== null),
    rubric: rubric.flatMap((entry) =>
      isRecord(entry)
        ? [
            {
              dimension: str(entry.dimension),
              score: num(entry.score),
              justification: str(entry.justification),
            },
          ]
        : [],
    ),
    rubric_mean: numOrNull(value.rubric_mean),
    summary: str(value.summary),
    replan_directive: strOrNull(value.replan_directive),
    refine_directive: strOrNull(value.refine_directive),
  };
}

function readMlflow(value: unknown): TerminalMlflow | null {
  if (!isRecord(value)) return null;
  const metrics = isRecord(value.logged_metrics) ? value.logged_metrics : {};
  const logged: Record<string, number> = {};
  for (const [key, entry] of Object.entries(metrics)) {
    if (typeof entry === "number" && Number.isFinite(entry)) logged[key] = entry;
  }
  return {
    experiment_name: str(value.experiment_name),
    run_id: str(value.run_id),
    parent_run_id: strOrNull(value.parent_run_id),
    ui_url: str(value.ui_url),
    logged_metrics: logged,
  };
}

function readUsage(value: unknown): TerminalUsage | null {
  if (!isRecord(value)) return null;
  return {
    tokens_in: num(value.tokens_in),
    tokens_out: num(value.tokens_out),
    llm_calls: num(value.llm_calls),
    node_visits: num(value.node_visits),
    sandbox_executions: num(value.sandbox_executions),
    started_at: strOrNull(value.started_at),
  };
}

/** The one entry point. `null` for a run that has not finished. */
export function readTerminalResult(result: unknown): TerminalResult | null {
  if (!isRecord(result)) return null;
  const deliverables = Array.isArray(result.deliverables) ? result.deliverables : [];
  return {
    status: strOrNull(result.status),
    deliverables: deliverables
      .map(readDeliverable)
      .filter((row): row is TerminalDeliverable => row !== null),
    bundle_url: strOrNull(result.bundle_url),
    evaluation: readEvaluation(result.evaluation),
    mlflow: readMlflow(result.mlflow),
    usage: readUsage(result.usage),
    error: strOrNull(result.error),
    last_node: strOrNull(result.last_node),
  };
}

// ── Projections onto the view models ─────────────────────────────────────────

/**
 * The evaluator's criteria as the app's `CriterionRow`.
 *
 * `verdictSeen` is `true` unconditionally: this data exists only because a verdict was
 * reached and persisted. That matters for how absence renders — after a verdict, an
 * `observed` of `null` is a *failure*, not a pending measurement (AGENTS.md §7.6), and a
 * row that came from `result` is always after the verdict.
 *
 * `tolerance` and `rationale` are not in the terminal payload — they belong to the
 * Planner's contract, which `result` does not carry — so they default rather than being
 * invented.
 */
export function criteriaFromResult(
  result: TerminalResult | null,
): CriterionRow[] {
  const rows = result?.evaluation?.criteria_results ?? [];
  return rows.map((row) => ({
    id: row.criterion_id,
    metric: row.metric,
    comparator: row.comparator,
    threshold: row.threshold,
    tolerance: 0,
    required: row.required,
    weight: row.weight,
    rationale: "",
    observed: row.observed,
    passed: row.passed,
    note: row.note,
    verdictSeen: true,
  }));
}

export function verdictFromResult(result: TerminalResult | null): VerdictRow | null {
  const evaluation = result?.evaluation;
  if (!evaluation) return null;
  return {
    decision: evaluation.decision as VerdictRow["decision"],
    passed: evaluation.passed,
    score: evaluation.score,
    rubric: evaluation.rubric,
    rubricMean: evaluation.rubric_mean,
    summary: evaluation.summary,
    directive: evaluation.replan_directive ?? evaluation.refine_directive,
  };
}

/** `3/4` — required criteria met over required criteria scored, or `null` if none. */
export function requiredCriteriaScore(result: unknown): string | null {
  const parsed = readTerminalResult(result);
  const required = (parsed?.evaluation?.criteria_results ?? []).filter(
    (row) => row.required,
  );
  if (required.length === 0) return null;
  return `${required.filter((row) => row.passed).length}/${required.length}`;
}
