"use client";

import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { CAPABILITIES, reasonUnless } from "@/lib/capabilities";
import { formatBytes, shortHash, shortId } from "@/lib/format";
import type { ArtifactRow, MetricPoint } from "@/lib/types";
import { useRunShallow, selectArtifacts } from "@/hooks/useRunSlice";
import { Banner, Button, Copyable, Empty, Panel } from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/dialog";
import { MetricSparkline } from "./MetricSparkline";
import { MetricsTable } from "./MetricsTable";

/**
 * Pane 4 — the artifact and deliverable deck (§8.5.6).
 *
 * ```
 * ARTIFACTS · 6                          MLflow: run-b41e7c2a ↗  [bundle ⇩]
 * ┌────────────┬────────────┬────────────┬────────────┐
 * │ main.py    │metrics.json│ confusion  │ model/     │
 * │ code 2.9KB │ metrics    │ _matrix.png│ .joblib    │
 * └────────────┴────────────┴────────────┴────────────┘
 * accuracy 0.9737 ▁▂▄▇  ·  f1_macro 0.9712  ·  train 1.83s
 * ```
 *
 * Tiles are grouped by `Deliverable.artifact_type` in the order a person looks for them —
 * code, model, plot, report, metrics, log, bundle — rather than alphabetically or by
 * arrival.
 *
 * Everything that needs an endpoint the backend does not have yet renders **disabled with
 * a reason** (§2.4). A greyed *Download bundle* whose tooltip names the missing route is
 * honest; a button that 404s is not, and a silently absent one hides the scope.
 */

/** §8.5.6: the order a person looks for them, from AGENTS.md §3.2's vocabulary. */
const TYPE_ORDER: readonly ArtifactRow["type"][] = [
  "code",
  "model",
  "plot",
  "report",
  "metrics",
  "log",
  "bundle",
];

const MLFLOW_BASE = (
  process.env.NEXT_PUBLIC_MLFLOW_BASE ?? "http://localhost:5001"
).replace(/\/$/, "");

export function ArtifactDeck({ collapsed }: { collapsed?: boolean }) {
  const { artifacts, metrics, mlflow, bundleUrl } = useRunShallow(selectArtifacts);
  const [preview, setPreview] = useState<ArtifactRow | null>(null);

  const grouped = useMemo(() => {
    const sorted = [...artifacts].sort(
      (a, b) => TYPE_ORDER.indexOf(a.type) - TYPE_ORDER.indexOf(b.type),
    );
    return sorted;
  }, [artifacts]);

  const metricKeys = useMemo(() => {
    const keys: string[] = [];
    for (const point of metrics) if (!keys.includes(point.key)) keys.push(point.key);
    return keys;
  }, [metrics]);

  const reconstructed = grouped.some((artifact) => artifact.reconstructed);

  if (collapsed) return null;

  return (
    <Panel
      title="Artifacts"
      count={grouped.length}
      className="shrink-0 border-t border-line"
      bodyClassName="overflow-x-auto"
      right={
        <>
          {mlflow && <MlflowLinks mlflow={mlflow} />}
          <Button
            disabled={!CAPABILITIES.runBundle}
            title={reasonUnless("runBundle") ?? "Download the run bundle"}
            onClick={() => {
              if (bundleUrl) window.open(bundleUrl, "_blank", "noopener");
            }}
          >
            ⇩ bundle
          </Button>
        </>
      }
    >
      {reconstructed && (
        <Banner tone="idle">
          Some tiles were reconstructed from the run record — this tab did not see their{" "}
          <code className="font-mono">artifact.created</code> events, so their download
          links and ids are not available.
        </Banner>
      )}

      {grouped.length === 0 && metricKeys.length === 0 ? (
        <Empty>No artifacts yet — the sandbox has not produced any.</Empty>
      ) : (
        <div className="flex flex-col gap-2 p-2">
          {grouped.length > 0 && (
            <ul className="flex flex-wrap gap-2">
              {grouped.map((artifact) => (
                <ArtifactTile
                  key={artifact.name}
                  artifact={artifact}
                  onPreview={() => setPreview(artifact)}
                />
              ))}
            </ul>
          )}

          {metricKeys.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line/60 pt-1.5">
              {metricKeys.map((key) => (
                <MetricSparkline key={key} metricKey={key} points={metrics} />
              ))}
            </div>
          )}
        </div>
      )}

      <ArtifactPreview
        artifact={preview}
        metrics={metrics}
        onClose={() => setPreview(null)}
      />
    </Panel>
  );
}

const TYPE_LABEL: Record<ArtifactRow["type"], string> = {
  code: "code",
  model: "model",
  plot: "plot",
  report: "report",
  metrics: "metrics",
  log: "log",
  bundle: "bundle",
};

function ArtifactTile({
  artifact,
  onPreview,
}: {
  artifact: ArtifactRow;
  onPreview: () => void;
}) {
  const previewable =
    artifact.type === "plot" || artifact.type === "metrics" || artifact.type === "report";
  const src = artifact.artifactId ? `/api/artifact/${artifact.artifactId}` : null;

  return (
    <li
      className={clsx(
        "flex w-40 shrink-0 flex-col gap-0.5 rounded border border-line bg-raised p-2",
        artifact.reconstructed && "border-dashed",
      )}
    >
      <button
        type="button"
        onClick={previewable ? onPreview : undefined}
        disabled={!previewable}
        title={previewable ? `Preview ${artifact.name}` : artifact.name}
        className="truncate text-left font-mono text-[11px] text-fg enabled:hover:text-running disabled:cursor-default"
      >
        {artifact.name}
      </button>

      {artifact.type === "plot" && src && (
        // Lazy and thumbnailed by CSS, never by loading a full-resolution plot into a
        // 96 px tile (§9.3). A plain `<img>`, not `next/image`: the bytes come from this
        // app's own authenticated proxy with unknown dimensions, and the optimizer needs
        // either a known size or a custom loader to do anything useful with them.
        /* eslint-disable-next-line @next/next/no-img-element */
        <img
          src={src}
          alt={artifact.name}
          loading="lazy"
          className="h-16 w-full cursor-pointer rounded bg-ink object-contain"
          onClick={onPreview}
        />
      )}

      <span className="flex items-baseline justify-between gap-1 text-[10px] text-muted">
        <span>{TYPE_LABEL[artifact.type]}</span>
        <span className="tnum">{formatBytes(artifact.sizeBytes)}</span>
      </span>

      <span className="flex items-center justify-between gap-1">
        {artifact.sha256 ? (
          <Copyable
            value={artifact.sha256}
            title={`Copy the full sha256 of ${artifact.name}`}
          >
            {shortHash(artifact.sha256)}
          </Copyable>
        ) : (
          <span className="text-[10px] text-idle">no hash</span>
        )}
        <a
          href={
            artifact.artifactId ? `/api/artifact/${artifact.artifactId}?download=1` : "#"
          }
          aria-disabled={!CAPABILITIES.artifactDownload || !artifact.artifactId}
          title={
            reasonUnless("artifactDownload") ??
            (artifact.artifactId ? "Download" : "This artifact has no id")
          }
          onClick={(cause) => {
            if (!CAPABILITIES.artifactDownload || !artifact.artifactId) {
              cause.preventDefault();
            }
          }}
          className={clsx(
            "text-[10px]",
            CAPABILITIES.artifactDownload && artifact.artifactId
              ? "text-muted hover:text-fg"
              : "cursor-not-allowed text-idle opacity-40",
          )}
        >
          ⇩
        </a>
      </span>
    </li>
  );
}

/**
 * MLflow deep links (§8.5.6, MLOPS.md §4.1).
 *
 * The parent and the winning child are linked **separately**. The nesting is the whole
 * point of the hierarchy — parent is the run, children are its attempts — and linking
 * only the parent hides the per-attempt comparison that makes it worth having.
 */
function MlflowLinks({
  mlflow,
}: {
  mlflow: NonNullable<ReturnType<typeof selectArtifacts>["mlflow"]>;
}) {
  const child =
    mlflow.ui_url ||
    `${MLFLOW_BASE}/#/experiments/${mlflow.experiment_id}/runs/${mlflow.run_id}`;
  const parent = mlflow.parent_run_id
    ? `${MLFLOW_BASE}/#/experiments/${mlflow.experiment_id}/runs/${mlflow.parent_run_id}`
    : null;

  return (
    <span className="flex items-center gap-1.5 text-[11px]">
      {parent && (
        <a
          href={parent}
          target="_blank"
          rel="noopener noreferrer"
          className="font-mono text-muted hover:text-running"
          title="The parent MLflow run — every attempt of this task"
        >
          run-{shortId(mlflow.parent_run_id)} ↗
        </a>
      )}
      <a
        href={child}
        target="_blank"
        rel="noopener noreferrer"
        className="font-mono text-muted hover:text-running"
        title="The MLflow run this attempt logged to"
      >
        {parent ? "attempt" : "mlflow"} {shortId(mlflow.run_id)} ↗
      </a>
      {mlflow.registered_model && (
        <a
          href={`${MLFLOW_BASE}/#/models/${encodeURIComponent(mlflow.registered_model)}${
            mlflow.model_version ? `/versions/${mlflow.model_version}` : ""
          }`}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded bg-ok/15 px-1 font-mono text-ok"
          title="This run registered a model version"
        >
          {mlflow.registered_model}
          {mlflow.model_version ? ` v${mlflow.model_version}` : ""} ↗
        </a>
      )}
    </span>
  );
}

/**
 * The preview dialog.
 *
 * `metrics.json` is parsed into the MLOPS.md §3.3 table rather than shown raw; plots are
 * streamed through the authenticated proxy; a report opens as text. Everything else has
 * no preview, and the tile says so by being un-clickable rather than by opening an empty
 * dialog.
 */
function ArtifactPreview({
  artifact,
  metrics,
  onClose,
}: {
  artifact: ArtifactRow | null;
  metrics: readonly MetricPoint[];
  onClose: () => void;
}) {
  const [body, setBody] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);

  const src = artifact?.artifactId ? `/api/artifact/${artifact.artifactId}` : null;
  const wantsJson = artifact?.type === "metrics";

  // Fetched when the dialog opens, not when the deck mounts: it holds up to a dozen
  // tiles and none of their bodies is worth a request until somebody asks for one.
  useEffect(() => {
    if (!wantsJson || !src) return;
    let alive = true;
    setBody(null);
    setError(null);
    void fetch(src)
      .then(async (response) => {
        if (!response.ok) {
          const detail = (await response.json().catch(() => null)) as {
            error?: string;
          } | null;
          throw new Error(detail?.error ?? `The proxy answered ${response.status}.`);
        }
        return response.json();
      })
      .then((parsed) => {
        if (alive) setBody(parsed);
      })
      .catch((cause: Error) => {
        if (alive) setError(cause.message);
      });
    return () => {
      alive = false;
    };
  }, [wantsJson, src]);

  if (!artifact) return null;

  return (
    <Dialog
      open
      onClose={onClose}
      title={artifact.name}
      description={
        <span className="flex flex-wrap items-baseline gap-x-3 font-mono text-[11px]">
          <span>{TYPE_LABEL[artifact.type]}</span>
          <span className="tnum">{formatBytes(artifact.sizeBytes)}</span>
          {artifact.sha256 && <Copyable value={artifact.sha256}>{artifact.sha256}</Copyable>}
        </span>
      }
      width="max-w-3xl"
    >
      {!src ? (
        <p className="text-xs text-muted">
          This artifact was reconstructed from the run record and has no id, so its bytes
          cannot be fetched. {reasonUnless("runArtifacts")}
        </p>
      ) : error ? (
        <p className="text-xs text-warn">⚠ {error}</p>
      ) : artifact.type === "plot" ? (
        /* eslint-disable-next-line @next/next/no-img-element */
        <img src={src} alt={artifact.name} className="mx-auto max-h-[60vh]" />
      ) : wantsJson ? (
        body === null ? (
          <p className="text-xs text-muted">Loading…</p>
        ) : (
          <MetricsTable body={body} />
        )
      ) : (
        <p className="text-xs text-muted">
          No inline preview for this type.{" "}
          {CAPABILITIES.artifactDownload ? (
            <a
              href={`${src}?download=1`}
              className="underline"
              target="_blank"
              rel="noopener noreferrer"
            >
              Download it
            </a>
          ) : (
            reasonUnless("artifactDownload")
          )}
        </p>
      )}

      {metrics.length > 0 && artifact.type === "metrics" && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-2">
          {[...new Set(metrics.map((point) => point.key))].map((key) => (
            <MetricSparkline key={key} metricKey={key} points={metrics} />
          ))}
        </div>
      )}
    </Dialog>
  );
}
