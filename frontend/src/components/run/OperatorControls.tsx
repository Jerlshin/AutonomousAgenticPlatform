"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { CAPABILITIES, reasonUnless } from "@/lib/capabilities";
import type { RunStream } from "@/hooks/useRunStream";
import { useRunShallow } from "@/hooks/useRunSlice";
import { Banner, Button } from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/dialog";
import { TextArea } from "@/components/ui/select";

/**
 * Cancel · Resync · Resume, and the overflow menu (§8.5.2).
 *
 * Two behaviours here are worth stating because both are easy to get wrong in a way that
 * is only visible under load:
 *
 * * **Cancel is cooperative, and the UI says so.** The label after clicking is
 *   *"Cancel requested — the run stops at the next node boundary"*, because that is what
 *   the engine actually does. A button that says "Cancelled" while a container is still
 *   training teaches operators to click it twice.
 * * **Optimistic UI is limited to disabling the control.** The command promise resolves
 *   on *delivery*, not effect (§7.5); the authoritative consequence arrives as
 *   `run.cancelled`.
 */
export function OperatorControls({ stream }: { stream: RunStream }) {
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [requested, setRequested] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const { run, terminal } = useRunShallow((state) => ({
    run: state.run,
    terminal: state.terminal,
  }));

  const detail = run?.status_detail ?? run?.status ?? "";
  const resumable = detail === "INTERRUPTED" || detail === "AWAITING_INPUT";

  const resume = useMutation({
    mutationFn: () => api.resumeRun(stream.run?.run_id ?? ""),
    onError: (cause: unknown) => {
      setProblem(cause instanceof ApiError ? cause.message : "The run could not be resumed.");
    },
    onSuccess: () => setProblem(null),
  });

  // Close the overflow menu on an outside click or Escape, which is the behaviour every
  // menu has and the one people notice only when it is missing.
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (cause: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(cause.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKeyDown = (cause: KeyboardEvent) => {
      if (cause.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  const runId = stream.run?.run_id ?? "";

  return (
    <>
      {problem && (
        <span className="text-[11px] text-fail" role="alert">
          {problem}
        </span>
      )}

      {resumable && (
        <Button
          onClick={() => resume.mutate()}
          disabled={resume.isPending}
          title="Re-enqueue this run; it resumes from its last checkpoint"
        >
          {resume.isPending ? "Resuming…" : "Resume"}
        </Button>
      )}

      <Button
        tone="danger"
        disabled={terminal || requested}
        title={
          terminal
            ? "This run has already finished."
            : requested
              ? "Cancel requested — the run stops at the next node boundary."
              : "Ask the run to stop at its next node boundary"
        }
        onClick={() => setConfirming(true)}
      >
        {requested ? "Cancel requested" : "Cancel"}
      </Button>

      <Button
        onClick={stream.resync}
        title="Ask the server for a fresh run.snapshot. Also the recovery for a suspected fold bug."
      >
        Resync
      </Button>

      <div className="relative" ref={menuRef}>
        <Button
          onClick={() => setMenuOpen((open) => !open)}
          title="More actions"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
        >
          ⋮
        </Button>
        {menuOpen && (
          <div
            role="menu"
            className="absolute right-0 top-full z-40 mt-1 flex w-56 flex-col rounded border border-line bg-raised py-1 text-xs shadow-xl"
          >
            <MenuItem
              onClick={() => {
                void navigator.clipboard?.writeText(runId);
                setMenuOpen(false);
              }}
            >
              Copy run id
            </MenuItem>
            <MenuLink href={`/runs/${runId}/code`}>Open code</MenuLink>
            <MenuLink href={`/runs/${runId}/report`}>Open report</MenuLink>
            {stream.mlflow?.ui_url && (
              <MenuItem
                onClick={() => {
                  window.open(stream.mlflow?.ui_url, "_blank", "noopener");
                  setMenuOpen(false);
                }}
              >
                Open MLflow ↗
              </MenuItem>
            )}
            <MenuItem
              disabled={!CAPABILITIES.runBundle}
              title={reasonUnless("runBundle")}
              onClick={() => setMenuOpen(false)}
            >
              Download bundle
            </MenuItem>
            <MenuItem
              onClick={() => {
                stream.reconnectNow();
                setMenuOpen(false);
              }}
            >
              Reconnect now
            </MenuItem>
            <MenuItem
              onClick={() => {
                setDebugOpen(true);
                setMenuOpen(false);
              }}
            >
              Debug drawer
            </MenuItem>
          </div>
        )}
      </div>

      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title="Cancel this run?"
        description="Cancellation is cooperative: the run stops at its next node boundary, not immediately. A container that is already training will finish or hit its timeout."
        footer={
          <>
            <Button onClick={() => setConfirming(false)}>Keep running</Button>
            <Button
              tone="danger"
              onClick={() => {
                void stream.cancel(reason.trim() || "cancelled from the dashboard").then(
                  (result) => {
                    setProblem(result.ok ? null : (result.error ?? "The cancel was not accepted."));
                    if (result.ok) setRequested(true);
                  },
                );
                setConfirming(false);
              }}
            >
              Request cancellation
            </Button>
          </>
        }
      >
        <TextArea
          rows={2}
          value={reason}
          onChange={setReason}
          label="Why this run is being cancelled"
          placeholder="Why (recorded in the run's report)…"
        />
        {stream.status !== "open" && (
          <Banner tone="idle" className="mt-2 rounded border">
            The socket is not open, so this will be sent over REST instead. It will still
            reach the worker.
          </Banner>
        )}
      </Dialog>

      <DebugDrawer stream={stream} open={debugOpen} onClose={() => setDebugOpen(false)} />
    </>
  );
}

function MenuItem({
  children,
  onClick,
  disabled,
  title,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="px-3 py-1 text-left transition-colors hover:bg-surface disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function MenuLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      role="menuitem"
      className="px-3 py-1 transition-colors hover:bg-surface"
    >
      {children}
    </Link>
  );
}

/**
 * The debug drawer (§8.5.2, §9.5).
 *
 * A developer affordance, kept behind the overflow menu on purpose. When a pane disagrees
 * with reality, this is the only tool that says which side is wrong — raw envelopes from
 * the `events` ring, filterable, with a copy-as-JSONL action.
 *
 * It also carries the §9.5 instrumentation. The end-to-end latency target in
 * ARCHITECTURE.md §17 is the one row of that table the frontend alone can measure, and
 * without this drawer it is simply unmeasured. Clock skew makes the absolute number soft;
 * the distribution's shape, and its changes, are what matter.
 */
function DebugDrawer({
  stream,
  open,
  onClose,
}: {
  stream: RunStream;
  open: boolean;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState("");

  if (!open) return null;

  // Read imperatively, not subscribed: the drawer is a snapshot of a ring that changes
  // sixty times a second, and subscribing to it would make the drawer the most expensive
  // component in the app.
  const events = stream.store.getState().events;
  const shown = filter
    ? events.filter(
        (event) =>
          event.type.includes(filter) || String(event.seq) === filter,
      )
    : events;

  return (
    <Dialog open onClose={onClose} title="Debug drawer" width="max-w-4xl">
      <dl className="mb-2 grid grid-cols-2 gap-x-4 gap-y-0.5 text-[11px] lg:grid-cols-4">
        <Metric label="events/s" value={stream.stats.eventsReceived} />
        <Metric label="commits" value={stream.stats.commits} />
        <Metric label="reconnects" value={stream.stats.reconnects} />
        <Metric label="last seq" value={stream.lastSeq} />
        <Metric
          label="latency p50"
          value={stream.stats.latencyP50 == null ? "—" : `${stream.stats.latencyP50} ms`}
        />
        <Metric
          label="latency p95"
          value={stream.stats.latencyP95 == null ? "—" : `${stream.stats.latencyP95} ms`}
        />
        <Metric label="samples" value={stream.stats.samples} />
        <Metric label="held events" value={events.length} />
        <Metric label="dropped events" value={stream.droppedEvents} />
        <Metric label="dropped lines" value={stream.droppedConsoleLines} />
        <Metric label="history" value={stream.historyComplete ? "complete" : "partial"} />
        <Metric label="filter" value={stream.effectiveFilter?.join(" ") ?? "none"} />
      </dl>

      <div className="mb-1 flex items-center gap-2">
        <input
          type="search"
          value={filter}
          onChange={(cause) => setFilter(cause.target.value)}
          placeholder="filter by type or seq…"
          aria-label="Filter events"
          className="w-52 rounded border border-line bg-surface px-2 py-1 text-xs"
        />
        <Button
          onClick={() => {
            void navigator.clipboard?.writeText(
              shown.map((event) => JSON.stringify(event)).join("\n"),
            );
          }}
          title="Copy the shown envelopes as JSONL"
        >
          Copy as JSONL
        </Button>
        <span className="tnum text-[11px] text-muted">
          {shown.length} of {events.length}
        </span>
      </div>

      <pre className="max-h-96 overflow-auto rounded border border-line bg-ink p-2 font-mono text-[11px] leading-4">
        {shown
          .slice(-400)
          .map(
            (event) =>
              `${String(event.seq).padStart(5, " ")} ${event.ts} ${event.type} ${JSON.stringify(
                event.payload,
              )}`,
          )
          .join("\n") || "No events held."}
      </pre>
    </Dialog>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-idle">{label}</dt>
      <dd className="tnum font-mono">{value}</dd>
    </div>
  );
}
