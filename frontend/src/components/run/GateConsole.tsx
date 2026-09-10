"use client";

import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { gateMeta, GATE_TIMEOUT_NOTE } from "@/lib/gates";
import { formatClock, formatMetric } from "@/lib/format";
import type { CommandResult, GateDecision, PendingGate } from "@/lib/types";
import { useRunShallow, selectGate } from "@/hooks/useRunSlice";
import { useSecondTicker } from "@/hooks/useTicker";
import { Badge, Banner, Button, Chip } from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/dialog";
import { TextArea } from "@/components/ui/select";
import { DiffViewer } from "./DiffViewer";

/**
 * The human-in-the-loop gate console (§8.6).
 *
 * A gate is the one moment the platform is *waiting on a person*, so it is the one thing
 * in this app that may interrupt. It renders as an `alertdialog` over the deck, plus a
 * persistent header banner and a pulsing `hitl_gate` node in the graph.
 *
 * Five rules from §8.6, each of which prevents a specific bad outcome:
 *
 * * **the countdown is visible and its consequence is stated.** Expiry is treated as
 *   rejection and terminates the run `CANCELLED` (AGENTS.md §9). A silent countdown that
 *   ends a run is indefensible.
 * * **the dialog is dismissible without deciding.** An operator needs to read the console
 *   before approving. Dismissal leaves the banner and the graph pulse — closing a window
 *   does not resolve a gate.
 * * **the dialog closes when the run leaves `AWAITING_INPUT`, not on the response.**
 *   `approve()` resolves on *delivery*; the authoritative consequence arrives as the next
 *   node starting.
 * * **decisions the API does not accept are hidden, not shown-and-broken.**
 *   `RunApproveRequest` validates `^(approve|reject)$`; `edit_criteria`, `approve_once`,
 *   `skip_registration` and `abort` are named as pending backend work (§15, B10) rather
 *   than rendered as buttons that 422.
 * * **a rejected submission surfaces its problem detail.** A gate that expired needs to
 *   say so, not fail silently.
 */

export function GateConsole({
  gate,
  open,
  onDismiss,
  onApprove,
}: {
  gate: PendingGate | null;
  open: boolean;
  onDismiss: () => void;
  onApprove: (
    gate: PendingGate["gate"],
    decision: GateDecision,
    notes?: string,
  ) => Promise<CommandResult>;
}) {
  const { codeRevisions, planSteps, criteria } = useRunShallow(selectGate);
  const now = useSecondTicker();
  const [notes, setNotes] = useState("");
  const [pending, setPending] = useState<GateDecision | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  // A new gate is a new decision: clearing the notes stops one gate's rationale from
  // being submitted with the next one's approval.
  useEffect(() => {
    setNotes("");
    setPending(null);
    setProblem(null);
  }, [gate?.seq]);

  const meta = gate ? gateMeta(gate.gate) : undefined;

  const remaining = useMemo(() => {
    if (!gate?.expiresAt || now === 0) return null;
    const expiry = Date.parse(gate.expiresAt);
    if (Number.isNaN(expiry)) return null;
    return Math.max(0, (expiry - now) / 1000);
  }, [gate?.expiresAt, now]);

  if (!gate || !meta) return null;

  const submit = async (decision: GateDecision) => {
    setPending(decision);
    setProblem(null);
    const result = await onApprove(gate.gate, decision, notes.trim() || undefined);
    if (!result.ok) {
      setProblem(result.error ?? "The decision was not accepted.");
      setPending(null);
      return;
    }
    // Deliberately no `onDismiss()` here. The dialog closes when the run actually leaves
    // AWAITING_INPUT — which clears `pendingGate` in the store — because delivery is not
    // effect (§7.5, §8.6).
  };

  const expired = remaining !== null && remaining <= 0;

  return (
    <Dialog
      open={open}
      onClose={onDismiss}
      role="alertdialog"
      width="max-w-4xl"
      title={`Gate: ${meta.label}`}
      description={
        <span className="flex flex-wrap items-center gap-2">
          <span>{meta.fires}</span>
          {remaining !== null && (
            <Chip tone={expired ? "fail" : remaining < 300 ? "warn" : "idle"}>
              {expired ? "expired" : `${formatClock(remaining)} left`}
            </Chip>
          )}
        </span>
      }
      footer={
        <>
          <span className="mr-auto text-[11px] text-muted">
            {GATE_TIMEOUT_NOTE}
          </span>
          <Button onClick={onDismiss} title="Close this without deciding — the gate stays open">
            Read first
          </Button>
          <Button
            tone="danger"
            disabled={pending !== null || expired}
            onClick={() => void submit("reject")}
          >
            {pending === "reject" ? "Rejecting…" : "Reject"}
          </Button>
          <Button
            tone="primary"
            size="md"
            disabled={pending !== null || expired}
            onClick={() => void submit("approve")}
          >
            {pending === "approve" ? "Approving…" : "Approve"}
          </Button>
        </>
      }
    >
      {expired && (
        <Banner tone="fail" className="mb-2 rounded border">
          This gate has expired. The run has been treated as rejected and terminates
          CANCELLED.
        </Banner>
      )}
      {problem && (
        <Banner tone="fail" className="mb-2 rounded border">
          {problem}
        </Banner>
      )}

      {gate.prompt && <p className="mb-2 text-xs text-fg">{gate.prompt}</p>}

      <GateBody
        gate={gate}
        planSteps={planSteps}
        criteria={criteria}
        codeRevisions={codeRevisions}
      />

      <div className="mt-3">
        <label
          htmlFor="gate-notes"
          className="mb-1 block text-[10px] font-semibold uppercase tracking-widest text-muted"
        >
          Notes
          <span className="ml-2 font-normal normal-case tracking-normal text-idle">
            submitted with the decision and quoted in the report&apos;s narrative
          </span>
        </label>
        <TextArea
          id="gate-notes"
          rows={3}
          value={notes}
          onChange={(next) => setNotes(next.slice(0, 2000))}
          placeholder="Why you are approving or rejecting…"
        />
        <p className="mt-0.5 text-right text-[10px] text-idle">{notes.length}/2000</p>
      </div>

      {meta.specifiedDecisions.length > 0 && (
        <p className="mt-2 text-[11px] text-idle">
          <code className="font-mono">{meta.specifiedDecisions.join(", ")}</code>{" "}
          {meta.specifiedDecisions.length === 1 ? "is" : "are"} specified for this gate in
          AGENTS.md §9 but not accepted by <code className="font-mono">RunApproveRequest</code>{" "}
          yet (docs/FRONTEND.md §15, B10), so {meta.specifiedDecisions.length === 1 ? "it is" : "they are"}{" "}
          not offered here.
        </p>
      )}
    </Dialog>
  );
}

/** What each gate shows, from AGENTS.md §9's "Payload shown" column. */
function GateBody({
  gate,
  planSteps,
  criteria,
  codeRevisions,
}: {
  gate: PendingGate;
  planSteps: ReturnType<typeof selectGate>["planSteps"];
  criteria: ReturnType<typeof selectGate>["criteria"];
  codeRevisions: ReturnType<typeof selectGate>["codeRevisions"];
}) {
  const context = gate.context;

  switch (gate.gate) {
    case "after_plan":
      return (
        <div className="flex flex-col gap-2">
          <Section title="Plan">
            {planSteps.length === 0 ? (
              <Muted>The plan is not in this tab&apos;s buffer.</Muted>
            ) : (
              <ol className="flex flex-col gap-1">
                {planSteps.map((step) => (
                  <li key={step.id} className="text-[11px]">
                    <span className="font-mono text-fg">
                      {step.index}. {step.title}
                    </span>
                    <Badge tone="idle">{step.kind}</Badge>
                    {step.dependsOn.length > 0 && (
                      <span className="ml-2 text-idle">
                        after {step.dependsOn.join(", ")}
                      </span>
                    )}
                    <p className="text-muted">{step.description}</p>
                  </li>
                ))}
              </ol>
            )}
          </Section>
          <Section title="Success criteria">
            {criteria.length === 0 ? (
              <Muted>No criteria were emitted with this plan.</Muted>
            ) : (
              <ul className="flex flex-col gap-0.5">
                {criteria.map((row) => (
                  <li key={row.id} className="flex gap-2 font-mono text-[11px]">
                    <span className="w-32 truncate">{row.metric}</span>
                    <span className="tnum text-muted">
                      {row.comparator} {formatMetric(row.threshold)}
                    </span>
                    <span className={row.required ? "text-fg" : "text-idle"}>
                      {row.required ? "required" : "optional"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <ContextDump context={context} />
        </div>
      );

    case "before_sandbox_exec": {
      const latest = codeRevisions.at(-1);
      const previous = codeRevisions.at(-2);
      const validation = context.validation as
        | { rejections?: string[]; warnings?: string[]; imports_seen?: string[]; writes_metrics_json?: boolean }
        | undefined;
      return (
        <div className="flex flex-col gap-2">
          {validation && (
            <Section title="Validation report">
              <ul className="flex flex-col gap-0.5 text-[11px]">
                {(validation.rejections ?? []).map((line) => (
                  <li key={line} className="text-fail">
                    ✗ {line}
                  </li>
                ))}
                {(validation.warnings ?? []).map((line) => (
                  <li key={line} className="text-warn">
                    ⚠ {line}
                  </li>
                ))}
                <li className={validation.writes_metrics_json ? "text-ok" : "text-fail"}>
                  {validation.writes_metrics_json ? "✓" : "✗"} writes metrics.json
                </li>
                {(validation.imports_seen ?? []).length > 0 && (
                  <li className="font-mono text-muted">
                    imports: {(validation.imports_seen ?? []).join(", ")}
                  </li>
                )}
              </ul>
            </Section>
          )}
          <Section title={previous ? `Revision ${latest?.revision} vs ${previous.revision}` : "Source"}>
            {latest ? (
              <DiffViewer
                before={previous?.content ?? ""}
                after={latest.content ?? ""}
                unifiedDiff={latest.diff}
                maxHeight="max-h-72"
              />
            ) : (
              <Muted>
                No <code className="font-mono">code.revision</code> event has been
                received, so the source is not in this tab&apos;s buffer.
              </Muted>
            )}
          </Section>
          {context.profile !== undefined && (
            <Section title="Profile">
              <pre className="overflow-auto font-mono text-[11px] text-muted">
                {JSON.stringify(context.profile, null, 2)}
              </pre>
            </Section>
          )}
        </div>
      );
    }

    case "before_model_registration":
      return (
        <div className="flex flex-col gap-2">
          <Section title="Metrics and the current champion">
            <ContextDump context={context} empty="No comparison was included in the gate payload." />
          </Section>
        </div>
      );

    case "on_replan":
      return (
        <div className="flex flex-col gap-2">
          <Section title="Failure history and the proposed direction">
            <ContextDump context={context} empty="No replan context was included in the gate payload." />
          </Section>
        </div>
      );
  }
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded border border-line p-2">
      <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-widest text-muted">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <p className="text-[11px] text-idle">{children}</p>;
}

/**
 * The gate's `context` blob, rendered rather than hidden.
 *
 * The per-gate payloads in AGENTS.md §9 are not all emitted with typed fields yet (§15,
 * B2). Showing the raw context is honest about that: an operator deciding whether to run
 * generated code needs everything the backend sent, and a component that renders only the
 * fields it knows about would silently drop the rest.
 */
function ContextDump({
  context,
  empty = "The gate payload carried no additional context.",
}: {
  context: Record<string, unknown>;
  empty?: string;
}) {
  const entries = Object.entries(context).filter(([key]) => key !== "validation");
  if (entries.length === 0) return <Muted>{empty}</Muted>;
  return (
    <pre
      className={clsx(
        "max-h-64 overflow-auto rounded bg-ink p-2 font-mono text-[11px] leading-4",
      )}
    >
      {JSON.stringify(Object.fromEntries(entries), null, 2)}
    </pre>
  );
}
