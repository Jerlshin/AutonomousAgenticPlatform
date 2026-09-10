"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { z } from "zod";
import { api, ApiError } from "@/lib/api";
import { CAPABILITIES, REASONS } from "@/lib/capabilities";
import { AGENT_ROLES, SANDBOX_PROFILES, TASK_KINDS } from "@/lib/domain";
import { qk, TASKS_PREFIX } from "@/lib/queryKeys";
import type { GateName } from "@/lib/types";
import { useUiStore } from "@/stores/uiStore";
import { Banner, Button, Panel } from "@/components/ui/primitives";
import { Field, Select, TextArea, TextInput } from "@/components/ui/select";
import { BudgetFields, budgetProblem, type BudgetKey, type Budgets } from "./BudgetFields";
import { GateSelector } from "./GateSelector";

/**
 * `/tasks/new` (§8.4).
 *
 * One form, submitted as **two calls** — `POST /tasks`, then `POST /tasks/{id}/runs` —
 * because that is the shape of the API. A failure between them leaves a created task with
 * no run, and the form says so and offers **Start run** rather than silently retrying the
 * pair: a silent retry against a task that was created is how one submission becomes two.
 *
 * The HOW / LIMITS / SUPERVISION sections render **read-only** until
 * `CAPABILITIES.runConfig` is true. `POST /tasks/{id}/runs` accepts only an
 * `Idempotency-Key` header today (§2.3), so editable controls whose values are discarded
 * would be worse than showing none — they would document a configuration the run does not
 * have.
 */

const DRAFT_KEY = "tasks/new";

const schema = z.object({
  title: z.string().trim().min(3, "At least 3 characters.").max(255, "At most 255."),
  prompt: z.string().trim().min(5, "At least 5 characters."),
});

/** Under this many characters, a prompt rarely carries a dataset, a metric and a deliverable. */
const THIN_PROMPT = 40;

export function TaskForm() {
  const router = useRouter();
  const client = useQueryClient();
  const savedDraft = useUiStore((state) => state.taskDrafts[DRAFT_KEY]);
  const saveDraft = useUiStore((state) => state.saveDraft);
  const clearDraft = useUiStore((state) => state.clearDraft);

  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState(savedDraft ?? "");
  const [taskKind, setTaskKind] = useState<string>(TASK_KINDS[0]);
  const [tags, setTags] = useState<string[]>([]);
  const [tagDraft, setTagDraft] = useState("");
  const [profile, setProfile] = useState<string>("train");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [budgets, setBudgets] = useState<Budgets>({});
  const [gates, setGates] = useState<GateName[]>([]);
  const [touched, setTouched] = useState(false);
  const [orphan, setOrphan] = useState<{ id: string; title: string } | null>(null);

  // Debounced draft persistence, so a refresh does not lose a long prompt (§8.4).
  useEffect(() => {
    const handle = setTimeout(() => {
      if (prompt.trim()) saveDraft(DRAFT_KEY, prompt);
    }, 400);
    return () => clearTimeout(handle);
  }, [prompt, saveDraft]);

  const parsed = schema.safeParse({ title, prompt });
  const errors = useMemo(() => {
    if (parsed.success) return {} as Record<string, string>;
    return Object.fromEntries(
      parsed.error.issues.map((issue) => [String(issue.path[0]), issue.message]),
    );
  }, [parsed]);

  const budgetErrors = (Object.keys(budgets) as BudgetKey[]).some(
    (key) => budgetProblem(key, budgets[key]!) !== undefined,
  );

  const configLocked = !CAPABILITIES.runConfig;
  const dirty =
    taskKind !== TASK_KINDS[0] ||
    tags.length > 0 ||
    profile !== "train" ||
    Object.keys(overrides).length > 0 ||
    Object.keys(budgets).length > 0 ||
    gates.length > 0;

  const submit = useMutation({
    mutationFn: async (start: boolean) => {
      const task = await api.createTask({ title: title.trim(), prompt: prompt.trim() });
      if (!start) return { task, runId: null as string | null };
      try {
        // One key per submission, held for this mutation's lifetime.
        const accepted = await api.startRun(task.id, crypto.randomUUID());
        return { task, runId: accepted.run_id };
      } catch (cause) {
        // The task exists; only the run failed. Reporting it as one failure would leave
        // an orphaned task the operator does not know they created.
        const error = new OrphanedTaskError(task.id, task.title, cause);
        throw error;
      }
    },
    onSuccess: ({ task, runId }) => {
      clearDraft(DRAFT_KEY);
      void client.invalidateQueries({ queryKey: TASKS_PREFIX });
      void client.invalidateQueries({ queryKey: qk.task(task.id) });
      router.push(runId ? `/runs/${runId}` : `/tasks/${task.id}`);
    },
    onError: (cause: unknown) => {
      if (cause instanceof OrphanedTaskError) {
        clearDraft(DRAFT_KEY);
        setOrphan({ id: cause.taskId, title: cause.taskTitle });
      }
    },
  });

  const busy = submit.isPending;
  const blocked = !parsed.success || budgetErrors || busy;

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(cause) => {
        cause.preventDefault();
        setTouched(true);
        if (!blocked) submit.mutate(true);
      }}
    >
      {orphan && (
        <Banner tone="warn">
          The task <strong>{orphan.title}</strong> was created, but starting its run
          failed:{" "}
          {submit.error instanceof OrphanedTaskError ? submit.error.detail : "unknown"}.
          Nothing was retried automatically —{" "}
          <a href={`/tasks/${orphan.id}`} className="underline">
            open it and start the run
          </a>
          .
        </Banner>
      )}
      {submit.isError && !orphan && (
        <Banner tone="fail">
          {submit.error instanceof ApiError
            ? submit.error.message
            : "The task could not be created."}
        </Banner>
      )}

      <Panel title="What" className="border border-line">
        <div className="flex flex-col gap-3 p-3">
          <Field
            label="Title"
            htmlFor="title"
            error={touched ? errors.title : undefined}
            hint="How this run will be listed. 3–255 characters."
          >
            <TextInput
              id="title"
              value={title}
              onChange={setTitle}
              placeholder="Breast cancer classifier beating 95% accuracy"
              onBlur={() => setTouched(true)}
            />
          </Field>

          <Field
            label="Task kind"
            htmlFor="task-kind"
            overridden={!configLocked && taskKind !== TASK_KINDS[0]}
            hint={
              configLocked
                ? "Not accepted by POST /tasks yet; the Planner infers it from the prompt."
                : "Chooses the MLflow experiment and the metrics.json contract."
            }
          >
            <Select
              id="task-kind"
              value={taskKind}
              onChange={setTaskKind}
              disabled={configLocked}
              title={configLocked ? REASONS.runConfig : undefined}
              label="Task kind"
              options={TASK_KINDS.map((kind) => ({ value: kind, label: kind }))}
            />
          </Field>

          <Field
            label="Prompt"
            htmlFor="prompt"
            error={touched ? errors.prompt : undefined}
            hint={
              prompt.trim().length > 0 && prompt.trim().length < THIN_PROMPT ? (
                <span className="text-warn">
                  ⚠ A thin prompt produces a thin plan. State the dataset, the target
                  metric, and the deliverables.
                </span>
              ) : (
                "State the dataset, the target metric, and the deliverables. The Planner turns this into machine-checkable success criteria."
              )
            }
          >
            <TextArea
              id="prompt"
              rows={8}
              value={prompt}
              onChange={setPrompt}
              placeholder="Train a classifier on the Wisconsin breast cancer dataset. Beat 95% held-out accuracy and 0.94 macro F1. Produce main.py, metrics.json, a confusion matrix plot and a short report."
            />
          </Field>

          <Field
            label="Tags"
            htmlFor="tags"
            overridden={!configLocked && tags.length > 0}
            hint={
              configLocked
                ? "Not accepted by POST /tasks yet (docs/FRONTEND.md §15, B6)."
                : "At most 10, 32 characters each."
            }
          >
            <div className="flex flex-wrap items-center gap-1.5">
              {tags.map((tag) => (
                <span
                  key={tag}
                  className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-[11px]"
                >
                  {tag}
                  <button
                    type="button"
                    aria-label={`Remove ${tag}`}
                    disabled={configLocked}
                    onClick={() => setTags(tags.filter((held) => held !== tag))}
                    className="text-muted hover:text-fail"
                  >
                    ×
                  </button>
                </span>
              ))}
              <TextInput
                id="tags"
                value={tagDraft}
                disabled={configLocked || tags.length >= 10}
                onChange={setTagDraft}
                placeholder="add a tag, then Enter"
                className="w-40"
                onKeyDown={(cause) => {
                  if (cause.key !== "Enter") return;
                  cause.preventDefault();
                  const tag = tagDraft.trim().slice(0, 32);
                  if (tag && !tags.includes(tag)) setTags([...tags, tag]);
                  setTagDraft("");
                }}
              />
            </div>
          </Field>
        </div>
      </Panel>

      {configLocked && (
        <Banner tone="idle">
          Run configuration is not yet accepted by{" "}
          <code className="font-mono">POST /tasks/{"{id}"}/runs</code>; the values below
          are the platform defaults this run will use.
        </Banner>
      )}

      <Panel title="How" className="border border-line">
        <div className="flex flex-col gap-3 p-3">
          <Field label="Sandbox profile" overridden={!configLocked && profile !== "train"}>
            <div className="flex flex-col gap-1">
              {SANDBOX_PROFILES.map((option) => (
                <label
                  key={option.value}
                  className="flex items-baseline gap-2 text-xs"
                  title={option.hint}
                >
                  <input
                    type="radio"
                    name="sandbox-profile"
                    value={option.value}
                    checked={profile === option.value}
                    disabled={configLocked}
                    onChange={() => setProfile(option.value)}
                    className="accent-running"
                  />
                  <span className="font-mono">{option.label}</span>
                  <span className="text-muted">{option.summary}</span>
                </label>
              ))}
            </div>
          </Field>

          <Field
            label="Model overrides"
            hint="Leave blank to use the platform routing. The run's actual routing is reported by run.started."
          >
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
              {AGENT_ROLES.map((role) => (
                <div key={role.role} className="flex items-center gap-2">
                  <span className="w-24 shrink-0 font-mono text-[11px] text-muted">
                    {role.role}
                  </span>
                  <TextInput
                    label={`Model for ${role.role}`}
                    value={overrides[role.role] ?? ""}
                    disabled={configLocked}
                    placeholder={`default: ${role.model}`}
                    onChange={(next) => {
                      const trimmed = next.trim();
                      if (!trimmed) {
                        const { [role.role]: _cleared, ...rest } = overrides;
                        setOverrides(rest);
                        return;
                      }
                      setOverrides({ ...overrides, [role.role]: trimmed });
                    }}
                  />
                </div>
              ))}
            </div>
          </Field>
        </div>
      </Panel>

      <Panel title="Limits" className="border border-line">
        <div className="p-3">
          <BudgetFields values={budgets} onChange={setBudgets} disabled={configLocked} />
        </div>
      </Panel>

      <Panel title="Supervision" className="border border-line">
        <div className="p-3">
          <GateSelector selected={gates} onChange={setGates} disabled={configLocked} />
        </div>
      </Panel>

      <div className="flex items-center justify-end gap-2 pb-4">
        {dirty && !configLocked && (
          <Button
            onClick={() => {
              setTaskKind(TASK_KINDS[0]);
              setTags([]);
              setProfile("train");
              setOverrides({});
              setBudgets({});
              setGates([]);
            }}
          >
            Reset to defaults
          </Button>
        )}
        <Button
          disabled={blocked}
          onClick={() => {
            setTouched(true);
            if (!blocked) submit.mutate(false);
          }}
        >
          Create only
        </Button>
        <Button type="submit" tone="primary" disabled={blocked} size="md">
          {busy ? "Submitting…" : "Create & run"}
        </Button>
      </div>
    </form>
  );
}

/**
 * The task was created; only the run failed.
 *
 * A distinct error type rather than a flag, so the `onError` handler cannot forget which
 * half succeeded — that is the state the operator most needs told about, and the one a
 * generic "submission failed" hides.
 */
class OrphanedTaskError extends Error {
  readonly detail: string;

  constructor(
    readonly taskId: string,
    readonly taskTitle: string,
    cause: unknown,
  ) {
    super("the task was created but its run could not be started");
    this.name = "OrphanedTaskError";
    this.detail =
      cause instanceof ApiError ? cause.message : "the API did not accept the run";
  }
}
