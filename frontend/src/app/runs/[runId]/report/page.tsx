import Link from "next/link";
import { notFound } from "next/navigation";
import { CAPABILITIES, REASONS } from "@/lib/capabilities";
import { ErrorState } from "@/components/ui/primitives";
import { ReportViewer, type ReportSource } from "@/components/run/ReportViewer";
import { getRunServerSide, serverText } from "@/lib/serverApi";
import { readTerminalResult } from "@/lib/terminalResult";

/**
 * `/runs/[runId]/report` — the rendered `REPORT.md` (§8.8).
 *
 * A true Server Component: it reads with the server-only token (§2.2) and renders the
 * Markdown at request time, so no parser and no report text reaches the client beyond the
 * finished HTML.
 *
 * Source resolution follows §8.8's three tiers, and each is behind its capability flag so
 * nothing is called speculatively (§2.4). Both upper tiers are gated off today, which is
 * why this reliably lands on the deterministic fallback — that is the specified behaviour
 * until B3/B4 land, not a stub.
 */
export default async function RunReportPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;

  const run = await getRunServerSide(runId);
  if (!run.ok) {
    if (run.status === 404) notFound();
    return (
      <div className="flex min-h-0 flex-1 flex-col p-4">
        <ErrorState
          title="Could not load this run"
          message={`${run.error} The report is rendered from the run record, so it cannot be shown without it.`}
        />
      </div>
    );
  }

  const source = await resolveReport(runId, run.data.result);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 p-4">
      <nav aria-label="Breadcrumb" className="no-print text-xs text-muted">
        <Link href="/tasks" className="transition-colors hover:text-fg">
          Tasks
        </Link>
        <span className="mx-1.5 text-idle">/</span>
        <Link href={`/runs/${runId}`} className="transition-colors hover:text-fg">
          <span className="font-mono">{runId.slice(0, 8)}</span>
        </Link>
        <span className="mx-1.5 text-idle">/</span>
        <span>Report</span>
      </nav>

      <ReportViewer runId={runId} run={run.data} source={source} />
    </div>
  );
}

/**
 * Tier 1 `GET /runs/{id}/report`, tier 2 the `report`-typed deliverable, tier 3 nothing.
 *
 * The tiers are ordered by fidelity, and each returns the *reason* it could not produce a
 * document rather than an empty string, because that reason is what the fallback page
 * renders in place of the report.
 */
async function resolveReport(
  runId: string,
  result: unknown,
): Promise<ReportSource> {
  if (CAPABILITIES.runReport) {
    const response = await serverText(`/runs/${runId}/report`);
    if (response.ok && response.data.trim() !== "") {
      return { markdown: response.data, origin: "endpoint", reason: null };
    }
  }

  if (CAPABILITIES.runArtifacts) {
    const report = readTerminalResult(result)?.deliverables.find(
      (file) => file.artifact_type === "report",
    );
    if (report?.artifact_id) {
      const response = await serverText(`/artifacts/${report.artifact_id}`);
      if (response.ok && response.data.trim() !== "") {
        return { markdown: response.data, origin: "artifact", reason: null };
      }
    }
  }

  return {
    markdown: null,
    origin: "none",
    reason: `The narrative report cannot be fetched yet: ${REASONS.runReport} What follows is reconstructed from the run record.`,
  };
}
