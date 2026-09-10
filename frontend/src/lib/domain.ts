/**
 * Static domain tables, transcribed with their source named (§4.5).
 *
 * None of these is a payload the backend sends, which is why they may be hand-written:
 * they are the *vocabulary* a form offers and a label reads. Each carries the section it
 * came from, because the sharpest remaining edge in the typed contract is a table like
 * this drifting silently — §13 covers the graph topology with a fixture for exactly that
 * reason, and these are the ones small enough not to need one.
 */

/** `MLOPS.md` §3.2 — the `task_kind` enum in the metrics.json schema. */
export const TASK_KINDS = [
  "tabular-classification",
  "tabular-regression",
  "image-classification",
  "text-classification",
  "timeseries-forecasting",
  "clustering",
  "dimensionality-reduction",
  "analysis",
] as const;

export type TaskKind = (typeof TASK_KINDS)[number];

/** `ARCHITECTURE.md` §10.3 — the sandbox execution profiles. */
export const SANDBOX_PROFILES = [
  {
    value: "exec",
    label: "exec",
    summary: "2 CPU · 2 GiB · 120 s · network none",
    hint: "Quick scripts and analysis. Not enough for training.",
  },
  {
    value: "train",
    label: "train",
    summary: "4 CPU · 6 GiB · 900 s · network none",
    hint: "The default. Enough for a scikit-learn fit on a tabular dataset.",
  },
  {
    value: "train-tracked",
    label: "train-tracked",
    summary: "4 CPU · 6 GiB · 900 s · network mlflow only",
    hint: "As train, plus egress to MLflow so the container logs its own run.",
  },
] as const;

export type SandboxProfile = (typeof SANDBOX_PROFILES)[number]["value"];

/**
 * `ARCHITECTURE.md` §11.1 — the role-to-model table.
 *
 * These are *display* defaults. The authority at runtime is `run.started`'s
 * `model_routing`, which reflects the tier the worker actually resolved: a machine under
 * 16 GB substitutes the small ladder, and a graph labelled with the table's defaults
 * would then be lying about which weights produced the code.
 */
export const AGENT_ROLES = [
  { role: "planner", model: "qwen2.5:14b-instruct", note: "Decomposition and criteria" },
  { role: "researcher", model: "llama3.1:8b", note: "Retrieval and extraction" },
  { role: "coder", model: "qwen2.5-coder:7b", note: "Code generation" },
  { role: "debugger", model: "qwen2.5-coder:7b", note: "Diagnosis, shares the code prior" },
  { role: "evaluator", model: "llama3.1:8b", note: "Advisory rubric only" },
  { role: "reporter", model: "llama3.1:8b", note: "The written deliverable" },
] as const;

export type AgentRole = (typeof AGENT_ROLES)[number]["role"];

/** `ARCHITECTURE.md` §8.2 — the collections `POST /corpus/search` will accept. */
export const SEARCH_COLLECTIONS = ["rd_corpus", "code_exemplars", "run_memory"] as const;

/**
 * The collections `POST /corpus/documents` will accept.
 *
 * `run_memory` is deliberately absent: it is written exclusively by the Reporter after a
 * *successful* run (AGENTS.md §7.8), never by a person uploading a document, and the
 * schema refuses it. §8.9 requires the selector reflect that asymmetry rather than
 * offering an option the API rejects.
 */
export const INGEST_COLLECTIONS = ["rd_corpus", "code_exemplars"] as const;

export type SearchCollection = (typeof SEARCH_COLLECTIONS)[number];
export type IngestCollection = (typeof INGEST_COLLECTIONS)[number];

/**
 * Narrow a `<select>` value back to the collection union.
 *
 * The native control hands back a bare `string`, while `CorpusSearchRequest.collection`
 * and `CorpusDocumentCreate.collection` are generated enums. Casting at each call site
 * would silently accept a value the API rejects the moment an option list drifts; these
 * fall back to the first valid collection instead, so the request stays well-formed.
 */
export function asSearchCollection(value: string): SearchCollection {
  return (SEARCH_COLLECTIONS as readonly string[]).includes(value)
    ? (value as SearchCollection)
    : SEARCH_COLLECTIONS[0];
}

export function asIngestCollection(value: string): IngestCollection {
  return (INGEST_COLLECTIONS as readonly string[]).includes(value)
    ? (value as IngestCollection)
    : INGEST_COLLECTIONS[0];
}
