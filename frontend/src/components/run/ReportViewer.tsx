import Link from "next/link";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CAPABILITIES, REASONS } from "@/lib/capabilities";
import { formatBytes, formatClock, shortHash } from "@/lib/format";
import {
  crossCheckCriteria,
  parseCriteriaTable,
  parseReport,
  type CriteriaMismatch,
  type ParsedSection,
} from "@/lib/report";
import type { RunRead } from "@/lib/rest";
import { Badge, Banner, Empty, Panel } from "@/components/ui/primitives";
import { ReportActions } from "./ReportActions";
import {
  criteriaFromResult,
  readTerminalResult,
  type TerminalResult,
} from "@/lib/terminalResult";

/**
 * `/runs/[runId]/report` — the rendered `REPORT.md` (§8.8).
 *
 * A Server Component, which buys three things at once: the Markdown parser never enters
 * the client bundle (§9.4), the fetch uses the server-only token (§2.2), and the document
 * is HTML by the time it reaches the browser. `react-markdown` is used **without**
 * `rehype-raw`, so embedded HTML is escaped rather than rendered — §8.8 requires that,
 * because every word of this document was written by a model.
 *
 * The source has three tiers (§8.8), tried in order and each gated so nothing is called
 * speculatively (§2.4). Today both upper tiers are gated off, so what renders is the
 * deterministic fallback: the eight sections' worth of data the platform *does* hold, in
 * `RunRead.result`. That is not a placeholder — for a terminal run it is the same
 * evaluation, deliverable manifest and MLflow reference the narrative report would be
 * written from, and labelling it as reconstructed is what §2.4 requires of a lossy path.
 */

export interface ReportSource {
  markdown: string | null;
  /** Where the document came from, for the provenance line. */
  origin: "endpoint" | "artifact" | "none";
  /** Why the markdown is absent, when it is. */
  reason: string | null;
}

export function ReportViewer({
  runId,
  run,
  source,
}: {
  runId: string;
  run: RunRead;
  source: ReportSource;
}) {
  const result = readTerminalResult(run.result);
  const criteria = criteriaFromResult(result);
  const mlflowUrl = result?.mlflow?.ui_url || null;

  if (source.markdown === null) {
    return (
      <ReportFallback
        runId={runId}
        run={run}
        result={result}
        reason={source.reason}
        mlflowUrl={mlflowUrl}
      />
    );
  }

  const parsed = parseReport(source.markdown);
  const mismatches = crossCheckCriteria(parseCriteriaTable(source.markdown), criteria);

  return (
    <div className="flex min-h-0 flex-1 gap-4">
      <TableOfContents sections={parsed.sections} extras={parsed.extras} />

      <article className="min-w-0 flex-1 overflow-auto">
        <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h1 className="text-lg font-semibold text-fg">{parsed.title ?? run.title}</h1>
          <div className="no-print">
            <ReportActions markdown={source.markdown} runId={runId} mlflowUrl={mlflowUrl} />
          </div>
        </header>

        {mismatches.length > 0 && <MismatchBanner mismatches={mismatches} />}

        {parsed.missingMandatory.length > 0 && (
          <Banner tone="fail" className="mb-2 rounded border">
            {parsed.missingMandatory.map((section) => section.title).join(", ")} — a
            mandatory section is missing from this report. This is a Reporter defect
            (AGENTS.md §7.8), not a shorter run.
          </Banner>
        )}

        {parsed.preamble && (
          <div className="report-prose mb-4 text-xs text-muted">
            <Markdown remarkPlugins={[remarkGfm]}>{parsed.preamble}</Markdown>
          </div>
        )}

        {parsed.sections.map((section) => (
          <ReportSectionBlock key={section.number} section={section} />
        ))}

        {parsed.extras.map((extra) => (
          <section key={extra.anchor} id={extra.anchor} className="mb-6 scroll-mt-4">
            <h2 className="mb-1 border-b border-line pb-1 text-sm font-semibold text-fg">
              {extra.heading}
            </h2>
            <div className="report-prose">
              <Markdown remarkPlugins={[remarkGfm]}>{extra.body}</Markdown>
            </div>
          </section>
        ))}

        <footer className="no-print mt-6 border-t border-line pt-2 text-[11px] text-idle">
          Rendered from{" "}
          {source.origin === "endpoint"
            ? "GET /runs/{id}/report"
            : "the report deliverable, through the artifact proxy"}
          .
        </footer>
      </article>
    </div>
  );
}

// ── Structure ────────────────────────────────────────────────────────────────

function TableOfContents({
  sections,
  extras,
}: {
  sections: ParsedSection[];
  extras: { heading: string; anchor: string }[];
}) {
  return (
    <nav
      aria-label="Report contents"
      className="no-print sticky top-0 hidden h-fit w-56 shrink-0 self-start text-xs lg:block"
    >
      <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-widest text-muted">
        Contents
      </p>
      <ol className="flex flex-col gap-1">
        {sections.map((section) => (
          <li key={section.number}>
            {section.present ? (
              <a
                href={`#${section.anchor}`}
                className="flex items-baseline gap-1.5 text-muted transition-colors hover:text-fg"
              >
                <span className="tnum text-idle">{section.number}.</span>
                <span className="min-w-0 truncate">{section.title}</span>
              </a>
            ) : (
              // A missing section keeps its place in the list rather than closing the
              // gap: a table of contents that renumbers around an omission hides it,
              // which is the failure §8.8 is guarding against.
              <span
                className="flex items-baseline gap-1.5 text-idle line-through decoration-idle/60"
                title="This section is missing from the report."
              >
                <span className="tnum">{section.number}.</span>
                <span className="min-w-0 truncate">{section.title}</span>
              </span>
            )}
          </li>
        ))}
        {extras.map((extra) => (
          <li key={extra.anchor}>
            <a
              href={`#${extra.anchor}`}
              className="flex items-baseline gap-1.5 text-muted transition-colors hover:text-fg"
            >
              <span className="text-idle">+</span>
              <span className="min-w-0 truncate">{extra.heading}</span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

function ReportSectionBlock({ section }: { section: ParsedSection }) {
  return (
    <section id={section.anchor} className="mb-6 scroll-mt-4">
      <h2 className="mb-1 flex items-baseline gap-2 border-b border-line pb-1 text-sm font-semibold text-fg">
        <span className="tnum text-idle">{section.number}.</span>
        <span>{section.heading ?? section.title}</span>
        {!section.present && (
          <Badge tone={section.mandatory ? "fail" : "warn"}>missing</Badge>
        )}
      </h2>
      {section.present ? (
        <div className="report-prose">
          <Markdown remarkPlugins={[remarkGfm]}>{section.body}</Markdown>
        </div>
      ) : (
        <p
          className={`text-xs ${section.mandatory ? "text-fail" : "text-muted"}`}
        >
          {section.absence}
        </p>
      )}
    </section>
  );
}

function MismatchBanner({ mismatches }: { mismatches: CriteriaMismatch[] }) {
  return (
    <div className="mb-3 rounded border border-warn/40 bg-warn/5 px-3 py-2">
      <p className="text-xs font-semibold text-warn">
        ⚠ The report&apos;s criteria table disagrees with the evaluator.
      </p>
      <p className="mt-0.5 text-[11px] text-muted">
        These numbers are generated from run state and must match. Where they differ, the
        machine verdict below is authoritative and the narrative table is wrong.
      </p>
      <ul className="mt-1.5 flex flex-col gap-0.5">
        {mismatches.map((mismatch, index) => (
          <li key={`${mismatch.criterion}-${index}`} className="text-[11px] text-warn">
            <span className="font-mono">{mismatch.criterion}</span>{" "}
            <span className="text-muted">— {mismatch.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── The deterministic fallback ───────────────────────────────────────────────

/**
 * What the platform can show when the narrative document is unreachable.
 *
 * §8.8's last two rules: render the deterministic fallback rather than an empty page,
 * and for a `FAILED` run with no report render the failure dossier instead. Both are the
 * same page here — a failed run simply has an error and no evaluation, and pretending
 * those are different views would duplicate the manifest and the MLflow link.
 */
function ReportFallback({
  runId,
  run,
  result,
  reason,
  mlflowUrl,
}: {
  runId: string;
  run: RunRead;
  result: TerminalResult | null;
  reason: string | null;
  mlflowUrl: string | null;
}) {
  const criteria = criteriaFromResult(result);
  const deliverables = result?.deliverables ?? [];
  const failed = run.status === "FAILED" || run.outcome === "FAILED";
  const terminal = result !== null || failed;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto">
      <Banner tone={failed ? "fail" : "warn"} className="rounded border">
        {reason ?? "The narrative report is not available for this run."}
      </Banner>

      {!terminal && (
        <Empty>
          This run has not finished. The report is written by the Reporter on every
          terminal path, so it will appear here once the run completes.
        </Empty>
      )}

      {failed && (
        <Panel title="Failure dossier" className="border border-line">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 p-3 text-xs">
            <dt className="text-muted">Error</dt>
            <dd className="text-fail">{run.error ?? "No error message was recorded."}</dd>
            <dt className="text-muted">Last node</dt>
            <dd className="font-mono text-fg">{run.current_node ?? "—"}</dd>
            <dt className="text-muted">Phase</dt>
            <dd className="font-mono text-fg">{run.phase ?? "—"}</dd>
            <dt className="text-muted">Debug iterations</dt>
            <dd className="tnum text-fg">{run.debug_iterations ?? 0}</dd>
            <dt className="text-muted">Replans</dt>
            <dd className="tnum text-fg">{run.replan_count ?? 0}</dd>
          </dl>
        </Panel>
      )}

      {criteria.length > 0 && (
        <Panel
          title="Criteria"
          count={criteria.length}
          className="border border-line"
        >
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-line text-left text-[10px] uppercase tracking-widest text-muted">
                <th className="px-3 py-1 font-semibold">Criterion</th>
                <th className="px-3 py-1 font-semibold">Target</th>
                <th className="px-3 py-1 text-right font-semibold">Achieved</th>
                <th className="px-3 py-1 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {criteria.map((row) => (
                <tr key={row.id} className="border-b border-line/60">
                  <td className="px-3 py-1 font-mono text-fg">{row.metric}</td>
                  <td className="tnum px-3 py-1 text-muted">
                    {row.comparator} {row.threshold}
                  </td>
                  <td className="tnum px-3 py-1 text-right text-fg">
                    {row.observed ?? <span className="text-idle">not measured</span>}
                  </td>
                  <td className="px-3 py-1">
                    {row.passed ? (
                      <span className="text-ok">✓ Pass</span>
                    ) : (
                      <span className="text-fail">
                        ✗ {row.observed === null ? "Absent" : "Miss"}
                      </span>
                    )}
                    {!row.required && <span className="ml-1 text-idle">(optional)</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {deliverables.length > 0 && (
        <Panel title="Artifacts" count={deliverables.length} className="border border-line">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-line text-left text-[10px] uppercase tracking-widest text-muted">
                <th className="px-3 py-1 font-semibold">File</th>
                <th className="px-3 py-1 font-semibold">Type</th>
                <th className="px-3 py-1 text-right font-semibold">Size</th>
                <th className="px-3 py-1 font-semibold">SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {deliverables.map((file) => (
                <tr key={`${file.name}-${file.sha256}`} className="border-b border-line/60">
                  <td className="px-3 py-1 font-mono text-fg">{file.name}</td>
                  <td className="px-3 py-1 text-muted">{file.artifact_type}</td>
                  <td className="tnum px-3 py-1 text-right text-muted">
                    {formatBytes(file.size_bytes)}
                  </td>
                  <td
                    className="px-3 py-1 font-mono text-idle"
                    title={file.sha256}
                  >
                    {shortHash(file.sha256)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      <Panel title="Run" className="border border-line">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 p-3 text-xs">
          <dt className="text-muted">Run</dt>
          <dd className="font-mono text-fg">{runId}</dd>
          <dt className="text-muted">Duration</dt>
          <dd className="tnum text-fg">
            {formatClock(
              (Date.parse(run.updated_at) - Date.parse(run.created_at)) / 1000,
            )}
          </dd>
          <dt className="text-muted">Tokens</dt>
          <dd className="tnum text-fg">
            {(run.tokens_in ?? 0) + (run.tokens_out ?? 0)}
          </dd>
          <dt className="text-muted">MLflow</dt>
          <dd>
            {mlflowUrl ? (
              <a
                href={mlflowUrl}
                target="_blank"
                rel="noreferrer"
                className="text-running underline-offset-2 hover:underline"
              >
                Open the MLflow run
              </a>
            ) : (
              <span className="text-idle">Not recorded for this run.</span>
            )}
          </dd>
        </dl>
      </Panel>

      <p className="no-print text-[11px] text-idle">
        Reconstructed from the run record. The narrative report needs{" "}
        <span className="font-mono">{REASONS.runReport}</span>{" "}
        {!CAPABILITIES.runArtifacts && (
          <>
            The deliverable fallback needs{" "}
            <span className="font-mono">{REASONS.runArtifacts}</span>
          </>
        )}
      </p>

      <p className="no-print text-[11px] text-idle">
        <Link href={`/runs/${runId}`} className="text-running hover:underline">
          Back to the run deck
        </Link>
      </p>
    </div>
  );
}
