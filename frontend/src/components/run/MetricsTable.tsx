"use client";

import { useMemo, useState } from "react";
import { z } from "zod";
import { formatMetric } from "@/lib/format";
import { Copyable } from "@/components/ui/primitives";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/**
 * `metrics.json`, parsed and rendered as a table (§8.5.6, MLOPS.md §3.3).
 *
 * A raw JSON blob in a pane this small helps nobody: the point of `metrics.json` is that
 * a person can check *which dataset*, *which split*, *which seed* produced a number, and
 * that is four nested objects deep in the raw form. A **view raw** toggle covers the rest.
 *
 * The document is validated with Zod before it is rendered, and that is not ceremony: it
 * is written by model-generated code running in a sandbox, so its shape is an assumption
 * rather than a guarantee. A schema failure renders the raw body with a note instead of
 * throwing a component boundary, because a malformed `metrics.json` is itself a finding.
 */

const dataset = z
  .object({
    id: z.string().optional(),
    sha256: z.string().optional(),
    n_samples: z.number().optional(),
    split: z.union([z.string(), z.record(z.unknown())]).optional(),
    seed: z.number().optional(),
    target_column: z.string().optional(),
  })
  .passthrough();

const schema = z
  .object({
    task_kind: z.string().optional(),
    framework: z.string().optional(),
    primary_metric: z.string().optional(),
    dataset: dataset.optional(),
    params: z.record(z.union([z.string(), z.number(), z.boolean(), z.null()])).optional(),
    metrics: z.record(z.number()).optional(),
    metric_series: z.record(z.array(z.number())).optional(),
    runtime: z.record(z.union([z.string(), z.number()])).optional(),
    baseline: z.record(z.union([z.string(), z.number()])).optional(),
  })
  .passthrough();

export function MetricsTable({ body }: { body: unknown }) {
  const [raw, setRaw] = useState(false);
  const parsed = useMemo(() => schema.safeParse(body), [body]);

  if (!parsed.success) {
    return (
      <div className="flex flex-col gap-1 p-2">
        <p className="text-[11px] text-warn">
          ⚠ This <code className="font-mono">metrics.json</code> does not match the
          MLOPS.md §3.2 schema. Showing it raw — the shape is itself a finding.
        </p>
        <RawJson body={body} />
      </div>
    );
  }

  const document = parsed.data;
  const metrics = Object.entries(document.metrics ?? {});
  const params = Object.entries(document.params ?? {});
  const runtime = Object.entries(document.runtime ?? {});
  const baseline = Object.entries(document.baseline ?? {});

  return (
    <div className="flex flex-col gap-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px]">
          {document.task_kind && (
            <span className="font-mono text-muted">{document.task_kind}</span>
          )}
          {document.framework && (
            <span className="font-mono text-idle">{document.framework}</span>
          )}
          {document.primary_metric && (
            <span className="text-muted">
              primary{" "}
              <span className="font-mono text-fg">{document.primary_metric}</span>
            </span>
          )}
        </span>
        <button
          type="button"
          onClick={() => setRaw((open) => !open)}
          aria-pressed={raw}
          className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:text-fg"
        >
          {raw ? "hide raw" : "view raw"}
        </button>
      </div>

      {document.dataset && (
        <section>
          <h4 className="mb-0.5 text-[10px] font-semibold uppercase tracking-widest text-muted">
            Dataset
          </h4>
          <dl className="grid grid-cols-2 gap-x-4 text-[11px] lg:grid-cols-4">
            {document.dataset.id && <Pair label="id">{document.dataset.id}</Pair>}
            {document.dataset.sha256 && (
              <Pair label="sha256">
                {/* Dataset identity is what makes a number reproducible; it has to be
                    copyable, not merely visible. */}
                <Copyable value={document.dataset.sha256}>
                  {document.dataset.sha256.slice(0, 12)}
                </Copyable>
              </Pair>
            )}
            {document.dataset.n_samples !== undefined && (
              <Pair label="n">{document.dataset.n_samples.toLocaleString()}</Pair>
            )}
            {document.dataset.seed !== undefined && (
              <Pair label="seed">{document.dataset.seed}</Pair>
            )}
            {document.dataset.split !== undefined && (
              <Pair label="split">
                {typeof document.dataset.split === "string"
                  ? document.dataset.split
                  : JSON.stringify(document.dataset.split)}
              </Pair>
            )}
            {document.dataset.target_column && (
              <Pair label="target">{document.dataset.target_column}</Pair>
            )}
          </dl>
        </section>
      )}

      {metrics.length > 0 && (
        <Section title="Metrics">
          <Table>
            <THead>
              <TH>Metric</TH>
              <TH numeric>Value</TH>
              <TH numeric>Baseline</TH>
            </THead>
            <TBody>
              {metrics.map(([key, value]) => {
                const reference = document.baseline?.[key];
                return (
                  <TR key={key}>
                    <TD mono>{key}</TD>
                    <TD numeric mono>
                      {formatMetric(value)}
                    </TD>
                    <TD numeric mono className="text-muted">
                      {typeof reference === "number" ? formatMetric(reference) : "—"}
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        </Section>
      )}

      {params.length > 0 && (
        <Section title="Params">
          <dl className="grid grid-cols-2 gap-x-4 text-[11px] lg:grid-cols-3">
            {params.map(([key, value]) => (
              <Pair key={key} label={key}>
                {value === null ? "null" : String(value)}
              </Pair>
            ))}
          </dl>
        </Section>
      )}

      {runtime.length > 0 && (
        <Section title="Runtime">
          <dl className="grid grid-cols-2 gap-x-4 text-[11px] lg:grid-cols-4">
            {runtime.map(([key, value]) => (
              <Pair key={key} label={key}>
                {String(value)}
              </Pair>
            ))}
          </dl>
        </Section>
      )}

      {baseline.length > 0 && metrics.length === 0 && (
        <Section title="Baseline">
          <dl className="grid grid-cols-2 gap-x-4 text-[11px] lg:grid-cols-3">
            {baseline.map(([key, value]) => (
              <Pair key={key} label={key}>
                {String(value)}
              </Pair>
            ))}
          </dl>
        </Section>
      )}

      {raw && <RawJson body={body} />}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h4 className="mb-0.5 text-[10px] font-semibold uppercase tracking-widest text-muted">
        {title}
      </h4>
      {children}
    </section>
  );
}

function Pair({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5">
      <dt className="shrink-0 font-mono text-idle">{label}</dt>
      <dd className="tnum min-w-0 truncate font-mono">{children}</dd>
    </div>
  );
}

function RawJson({ body }: { body: unknown }) {
  return (
    <pre className="max-h-48 overflow-auto rounded border border-line bg-ink p-2 font-mono text-[11px] leading-4">
      {JSON.stringify(body, null, 2)}
    </pre>
  );
}
