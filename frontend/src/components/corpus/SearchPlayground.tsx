"use client";

import clsx from "clsx";
import { useMutation } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  asSearchCollection,
  SEARCH_COLLECTIONS,
  TASK_KINDS,
  type SearchCollection,
} from "@/lib/domain";
import { formatMetric } from "@/lib/format";
import type { CorpusSearchHit, CorpusSearchResponse } from "@/lib/rest";
import {
  Badge,
  Banner,
  Button,
  Chip,
  Empty,
  ErrorState,
  Panel,
} from "@/components/ui/primitives";
import { Field, Select, TextInput } from "@/components/ui/select";
import type { Tone } from "@/lib/types";

/**
 * The retrieval playground (§8.9) — what the Researcher sees, on demand.
 *
 * The session history strip is the point of the view rather than a convenience: comparing
 * two phrasings of the same question side by side is the entire reason to have a
 * playground, so every executed query is kept and any two can be pinned against each
 * other.
 *
 * Two things this view deliberately does **not** show, because `CorpusSearchResponse`
 * does not carry them and §8.9 forbids inventing them:
 *
 * * the hybrid-retrieval explain fields (`dense_rank`, `sparse_rank`, `rrf_score`,
 *   `took_ms`) — §15, B11;
 * * per-hit metadata, which is what the `run_memory` fix inspector would need for the
 *   error fingerprint, the fix summary and the link back to the originating run.
 *   `CorpusSearchHit` has `point_id`, `score`, `source_uri`, `title`, `section`, `text`
 *   and `trust_level` and nothing else, so that inspector is noted as unavailable rather
 *   than faked from the chunk text.
 */

const TOP_K_DEFAULT = 6;
const TOP_K_MIN = 1;
const TOP_K_MAX = 50;

const TRUST_TONES: Record<string, Tone> = {
  curated: "ok",
  verified: "running",
  untrusted: "warn",
};

interface HistoryEntry {
  id: number;
  query: string;
  collection: SearchCollection;
  topK: number;
  taskKind: string;
  response: CorpusSearchResponse;
}

export function SearchPlayground() {
  const formId = useId();
  const [query, setQuery] = useState("");
  const [collection, setCollection] = useState<SearchCollection>(SEARCH_COLLECTIONS[0]);
  const [topK, setTopK] = useState(String(TOP_K_DEFAULT));
  const [taskKind, setTaskKind] = useState("");

  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [pinned, setPinned] = useState<number | null>(null);
  // A monotonic id, so two identical queries stay distinguishable in the strip.
  const idRef = useRef(0);

  const search = useMutation({
    mutationFn: () =>
      api.searchCorpus({
        query: query.trim(),
        collection,
        top_k: clampTopK(topK),
        task_kind: taskKind === "" ? null : taskKind,
      }),
    onSuccess: (response) => {
      setHistory((entries) =>
        [
          {
            id: (idRef.current += 1),
            query: query.trim(),
            collection,
            topK: clampTopK(topK),
            taskKind,
            response,
          },
          ...entries,
        ].slice(0, 12),
      );
    },
  });

  const latest = history[0] ?? null;
  const comparison = pinned === null ? null : history.find((e) => e.id === pinned) ?? null;

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (query.trim() === "" || search.isPending) return;
    search.mutate();
  }

  return (
    <div className="flex flex-col gap-3">
      <Panel title="Retrieval playground" className="border border-line">
        <form onSubmit={submit} className="flex flex-col gap-3 p-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64 flex-1">
              <Field label="Query" htmlFor={`${formId}-query`}>
                <TextInput
                  id={`${formId}-query`}
                  value={query}
                  onChange={setQuery}
                  type="search"
                  placeholder="What the Researcher would ask…"
                />
              </Field>
            </div>

            <Field label="Collection" htmlFor={`${formId}-collection`}>
              <Select
                id={`${formId}-collection`}
                label="Collection"
                value={collection}
                onChange={(value) => setCollection(asSearchCollection(value))}
                options={SEARCH_COLLECTIONS.map((name) => ({
                  value: name,
                  label: name,
                }))}
              />
            </Field>

            <Field
              label="Top-K"
              htmlFor={`${formId}-topk`}
              hint={`${TOP_K_MIN}–${TOP_K_MAX}`}
            >
              <TextInput
                id={`${formId}-topk`}
                value={topK}
                onChange={setTopK}
                type="number"
                min={TOP_K_MIN}
                max={TOP_K_MAX}
                className="w-20"
              />
            </Field>

            <Field label="Task kind" htmlFor={`${formId}-kind`}>
              <Select
                id={`${formId}-kind`}
                label="Task kind filter"
                value={taskKind}
                onChange={setTaskKind}
                options={[
                  { value: "", label: "Any" },
                  ...TASK_KINDS.map((kind) => ({ value: kind, label: kind })),
                ]}
              />
            </Field>

            <Button
              type="submit"
              tone="primary"
              disabled={query.trim() === "" || search.isPending}
            >
              {search.isPending ? "Searching…" : "Search"}
            </Button>
          </div>

          {collection === "run_memory" && (
            <Banner tone="idle" className="rounded border">
              <span className="text-muted">
                <span className="font-mono">run_memory</span> holds one point per debug
                cycle of a successful run. The fix inspector — error fingerprint, fix
                summary, and a link back to the originating run — needs per-hit metadata
                that <span className="font-mono">CorpusSearchHit</span> does not return
                (docs/FRONTEND.md §15, B11), so each hit below shows only its stored text.
              </span>
            </Banner>
          )}
        </form>
      </Panel>

      {search.isError && (
        <ErrorState
          title="The search failed"
          message={
            search.error instanceof Error
              ? search.error.message
              : "POST /api/v1/corpus/search did not answer."
          }
          onRetry={() => search.mutate()}
        />
      )}

      {history.length > 0 && (
        <HistoryStrip
          history={history}
          pinned={pinned}
          onPin={(id) => setPinned((current) => (current === id ? null : id))}
        />
      )}

      {latest === null ? (
        <Panel title="Results" className="border border-line">
          <Empty>
            Run a query to see what the Researcher would retrieve for it.
          </Empty>
        </Panel>
      ) : (
        <div className={clsx("grid gap-3", comparison && "lg:grid-cols-2")}>
          <ResultColumn entry={latest} heading="Latest" />
          {comparison && comparison.id !== latest.id && (
            <ResultColumn entry={comparison} heading="Pinned" />
          )}
        </div>
      )}
    </div>
  );
}

function clampTopK(raw: string): number {
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return TOP_K_DEFAULT;
  return Math.min(TOP_K_MAX, Math.max(TOP_K_MIN, parsed));
}

function HistoryStrip({
  history,
  pinned,
  onPin,
}: {
  history: HistoryEntry[];
  pinned: number | null;
  onPin: (id: number) => void;
}) {
  return (
    <Panel
      title="Session history"
      count={history.length}
      className="border border-line"
      right={
        <span className="text-[11px] text-idle">
          Pin one to compare it against the latest.
        </span>
      }
    >
      <ul className="flex flex-wrap gap-1.5 p-2">
        {history.map((entry) => (
          <li key={entry.id}>
            <button
              type="button"
              onClick={() => onPin(entry.id)}
              aria-pressed={pinned === entry.id}
              className={clsx(
                "flex max-w-xs items-center gap-1.5 rounded border px-2 py-1 text-[11px] transition-colors",
                pinned === entry.id
                  ? "border-running/50 bg-running/15 text-running"
                  : "border-line text-muted hover:bg-raised hover:text-fg",
              )}
              title={`${entry.query} · ${entry.collection} · top ${entry.topK}`}
            >
              <span className="min-w-0 truncate">{entry.query}</span>
              <span className="shrink-0 text-idle">
                {entry.response.hits.length}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function ResultColumn({ entry, heading }: { entry: HistoryEntry; heading: string }) {
  return (
    <Panel
      title={heading}
      count={`${entry.response.hits.length} hit${entry.response.hits.length === 1 ? "" : "s"}`}
      className="min-w-0 border border-line"
      right={
        <span className="min-w-0 truncate text-[11px] text-idle">
          {entry.collection} · top {entry.topK}
          {entry.taskKind && ` · ${entry.taskKind}`}
        </span>
      }
    >
      <p className="border-b border-line px-3 py-1.5 text-[11px] text-muted">
        <span className="text-idle">query — </span>
        {entry.query}
      </p>
      {entry.response.hits.length === 0 ? (
        <Empty>
          Nothing matched in <span className="font-mono">{entry.collection}</span>.
        </Empty>
      ) : (
        <ol className="flex flex-col">
          {entry.response.hits.map((hit, index) => (
            <HitCard
              key={hit.point_id}
              hit={hit}
              rank={index + 1}
              query={entry.query}
            />
          ))}
        </ol>
      )}
    </Panel>
  );
}

function HitCard({
  hit,
  rank,
  query,
}: {
  hit: CorpusSearchHit;
  rank: number;
  query: string;
}) {
  const trust = hit.trust_level ?? "curated";
  return (
    <li className="border-b border-line/60 px-3 py-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="tnum text-[11px] text-idle">#{rank}</span>
        <span className="min-w-0 flex-1 truncate text-xs font-semibold text-fg">
          {hit.title || hit.source_uri || hit.point_id}
        </span>
        <Chip tone="idle" dot={false} title="Similarity score">
          {formatMetric(hit.score)}
        </Chip>
        <Badge tone={TRUST_TONES[trust] ?? "idle"} title={`trust_level: ${trust}`}>
          {trust}
        </Badge>
      </div>

      {(hit.section || hit.source_uri) && (
        <p className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-idle">
          {hit.section && <span>§ {hit.section}</span>}
          {hit.source_uri && <span className="font-mono">{hit.source_uri}</span>}
        </p>
      )}

      <p className="mt-1 whitespace-pre-wrap text-[11px] leading-5 text-muted">
        <Highlighted text={hit.text} query={query} />
      </p>
    </li>
  );
}

/**
 * Query terms marked in the chunk text (§8.9).
 *
 * Split on the terms rather than injecting HTML: the chunk is corpus content and must
 * never be rendered as markup. Terms shorter than three characters are skipped, because
 * highlighting every "a" and "of" marks the whole paragraph and communicates nothing.
 */
function Highlighted({ text, query }: { text: string; query: string }) {
  const terms = Array.from(
    new Set(
      query
        .toLowerCase()
        .split(/[^a-z0-9_]+/i)
        .filter((term) => term.length >= 3),
    ),
  );

  if (terms.length === 0) return <>{text}</>;

  const pattern = new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "gi");
  const parts = text.split(pattern);

  return (
    <>
      {parts.map((part, index) =>
        terms.includes(part.toLowerCase()) ? (
          <mark key={index} className="rounded-sm bg-running/25 text-fg">
            {part}
          </mark>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
