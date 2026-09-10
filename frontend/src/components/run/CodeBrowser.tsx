"use client";

import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { CAPABILITIES } from "@/lib/capabilities";
import { formatRelative, shortHash } from "@/lib/format";
import type { CodeRevisionRow } from "@/lib/types";
import { useRunStream } from "@/hooks/useRunStream";
import { useRunShallow, selectCode } from "@/hooks/useRunSlice";
import { RunStoreProvider } from "@/stores/RunStoreProvider";
import {
  Banner,
  Chip,
  Copyable,
  Empty,
  Panel,
  Skeleton,
} from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { DiffViewer } from "./DiffViewer";

/**
 * `/runs/[runId]/code` — the revision diff viewer (§8.7).
 *
 * This view is the clearest evidence the platform self-corrects, which is why the
 * rationale and the diagnosis are not decoration: a diff without them is just a patch.
 * Each revision shows *why* it was written (`rationale`), *what it was written against*
 * (`addresses_error`), and the Debugger's account of the failure from the same iteration.
 *
 * The stream is opened rather than polled, because for a live run the revisions exist
 * only as `code.revision` events. For a run that finished before this tab opened, the
 * replay delivers the same events from the retained window — and when that window has
 * been trimmed, the fallback below says so instead of showing an empty rail.
 */
export function CodeBrowser({ runId }: { runId: string }) {
  const stream = useRunStream(runId);
  return (
    <RunStoreProvider store={stream.store}>
      <CodeBrowserBody replayed={stream.replayed} />
    </RunStoreProvider>
  );
}

function CodeBrowserBody({ replayed }: { replayed: boolean }) {
  const { codeRevisions, diagnoses, historyComplete } = useRunShallow(selectCode);
  const [leftId, setLeftId] = useState<number | null>(null);
  const [rightId, setRightId] = useState<number | null>(null);

  // Default to the last two revisions, and follow the stream as new ones arrive — until
  // the operator picks a pair themselves, at which point their choice sticks.
  const [pinned, setPinned] = useState(false);
  useEffect(() => {
    if (pinned || codeRevisions.length === 0) return;
    const latest = codeRevisions[codeRevisions.length - 1]!;
    const previous = codeRevisions[codeRevisions.length - 2];
    setRightId(latest.revision);
    setLeftId(previous?.revision ?? null);
  }, [codeRevisions, pinned]);

  const left = codeRevisions.find((revision) => revision.revision === leftId) ?? null;
  const right = codeRevisions.find((revision) => revision.revision === rightId) ?? null;

  const diagnosis = useMemo(() => {
    if (!right) return null;
    // The diagnosis that preceded this revision — the one it was written from.
    return [...diagnoses].reverse().find((row) => (row.revision ?? 0) < right.revision) ?? null;
  }, [diagnoses, right]);

  const options = codeRevisions.map((revision) => ({
    value: String(revision.revision),
    label: `rev ${revision.revision}`,
  }));

  if (!replayed && codeRevisions.length === 0) {
    return (
      <Panel title="Code revisions" className="min-h-0 flex-1 border border-line">
        <Skeleton rows={8} />
      </Panel>
    );
  }

  if (codeRevisions.length === 0) {
    return (
      <Panel title="Code revisions" className="min-h-0 flex-1 border border-line">
        <Empty>
          No <code className="font-mono">code.revision</code> event was captured for this
          run — the emitter is not built yet (docs/FRONTEND.md §15, B2), or the retention
          window dropped them.{" "}
          {CAPABILITIES.runArtifacts
            ? "The final source is available from the artifact deck."
            : "The final source will be available from the artifact deck once GET /runs/{id}/artifacts lands."}
        </Empty>
      </Panel>
    );
  }

  return (
    <Panel
      title="Code revisions"
      count={codeRevisions.length}
      className="min-h-0 flex-1 border border-line"
      bodyClassName="min-h-0 flex flex-col"
      right={
        <>
          <Select
            label="Compare from"
            value={leftId === null ? "" : String(leftId)}
            onChange={(value) => {
              setPinned(true);
              setLeftId(value === "" ? null : Number(value));
            }}
            options={[{ value: "", label: "(empty)" }, ...options]}
          />
          <span className="text-[11px] text-idle">→</span>
          <Select
            label="Compare to"
            value={rightId === null ? "" : String(rightId)}
            onChange={(value) => {
              setPinned(true);
              setRightId(value === "" ? null : Number(value));
            }}
            options={options}
          />
        </>
      }
    >
      {!historyComplete && (
        <Banner tone="warn">
          Part of this run&apos;s history is no longer held, so intermediate revisions may
          be missing from the rail below.
        </Banner>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-2 p-2 lg:flex-row">
        <RevisionRail
          revisions={codeRevisions}
          selected={rightId}
          onSelect={(revision) => {
            setPinned(true);
            setRightId(revision);
            const index = codeRevisions.findIndex((row) => row.revision === revision);
            setLeftId(index > 0 ? (codeRevisions[index - 1]?.revision ?? null) : null);
          }}
        />

        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
          {right && (
            <section className="shrink-0 rounded border border-line p-2 text-[11px]">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-mono text-fg">
                  rev {right.revision} · {right.path}
                </span>
                <Copyable value={right.sha256} title="Copy the full sha256">
                  {shortHash(right.sha256)}
                </Copyable>
                <span className="tnum text-muted">{right.linesChanged} lines changed</span>
                <span className="text-idle">{formatRelative(right.ts)}</span>
                {right.addressesError && (
                  <Chip tone="fail" dot={false} title="The error fingerprint this revision was written against">
                    fixes {right.addressesError}
                  </Chip>
                )}
              </div>
              {right.rationale && (
                <p className="mt-1 text-muted">
                  <span className="text-idle">rationale — </span>
                  {right.rationale}
                </p>
              )}
              {diagnosis && (
                <p className="mt-1 text-warn">
                  <span className="text-idle">debugger — </span>
                  {diagnosis.summary}
                </p>
              )}
            </section>
          )}

          <div className="min-h-0 flex-1">
            {right ? (
              <DiffViewer
                before={left?.content ?? ""}
                after={right.content ?? ""}
                unifiedDiff={right.diff}
                maxHeight="max-h-full"
              />
            ) : (
              <Empty>Pick a revision to compare.</Empty>
            )}
          </div>

          {right && right.content === undefined && (
            <Banner tone="idle" className="shrink-0 rounded border">
              The full source of this revision was dropped to stay within the client
              buffer; its metadata and the server&apos;s own diff are still held.
            </Banner>
          )}
        </div>
      </div>
    </Panel>
  );
}

/** The revision rail: `rev 1 · rev 2 · rev 3 ★` (§8.7). */
function RevisionRail({
  revisions,
  selected,
  onSelect,
}: {
  revisions: readonly CodeRevisionRow[];
  selected: number | null;
  onSelect: (revision: number) => void;
}) {
  return (
    <ol className="flex shrink-0 gap-1 overflow-x-auto lg:w-40 lg:flex-col lg:overflow-y-auto">
      {revisions.map((revision, index) => {
        const latest = index === revisions.length - 1;
        return (
          <li key={revision.revision}>
            <button
              type="button"
              onClick={() => onSelect(revision.revision)}
              title={revision.rationale || `Revision ${revision.revision}`}
              className={clsx(
                "flex w-full flex-col rounded border px-2 py-1 text-left text-[11px] transition-colors",
                selected === revision.revision
                  ? "border-running/50 bg-running/10 text-fg"
                  : "border-line text-muted hover:bg-raised",
              )}
            >
              <span className="font-mono">
                rev {revision.revision}
                {latest && <span className="ml-1 text-warn">★</span>}
              </span>
              <span className="tnum truncate text-idle">
                {revision.linesChanged} lines
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
