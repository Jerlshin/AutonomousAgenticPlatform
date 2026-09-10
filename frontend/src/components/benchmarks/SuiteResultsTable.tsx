"use client";

import clsx from "clsx";
import Link from "next/link";
import { Fragment, useState } from "react";
import { readChecks, type CaseComparison, type Transition } from "@/lib/benchmarks";
import { formatAbsolute, formatClock, formatMetric, formatRelative } from "@/lib/format";
import type { BenchmarkResultRead } from "@/lib/rest";
import { Badge, Chip, Empty, Panel } from "@/components/ui/primitives";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/**
 * The per-case results table (§8.10).
 *
 * Two rules shape it. First, **rows link to the run that produced the number** — "the
 * value of a benchmark table is being one click from the run that produced it" — so
 * `run_id` becomes a link wherever it is set. Second, the `checks` array is *expanded*
 * rather than summarised: `passed: false` tells an operator nothing, and the reason a
 * case failed is the one thing they came to the table for.
 *
 * The transition column is what makes this a regression report rather than a snapshot. A
 * pass→fail row is the one that matters, so it sorts to the top and is the only
 * transition rendered in the failure tone.
 */

const TRANSITION_LABEL: Record<Transition, string> = {
  regressed: "pass → fail",
  gained: "fail → pass",
  unchanged: "unchanged",
  new: "first run",
};

export function SuiteResultsTable({
  cases,
  trapIds,
}: {
  cases: CaseComparison[];
  /** Case ids the suite definition marks as traps, for the badge. */
  trapIds: ReadonlySet<string>;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  // Regressions first, then first-runs, then everything else alphabetically. A table
  // sorted purely by case id buries the row the operator opened the page for.
  const ordered = [...cases].sort((a, b) => {
    const rank = (entry: CaseComparison) =>
      entry.transition === "regressed" ? 0 : entry.transition === "new" ? 1 : 2;
    return rank(a) - rank(b) || a.caseId.localeCompare(b.caseId);
  });

  const regressions = cases.filter((entry) => entry.transition === "regressed").length;

  return (
    <Panel
      title="Cases"
      count={`${cases.length}`}
      className="border border-line"
      right={
        regressions > 0 ? (
          <Chip tone="fail" dot={false}>
            {regressions} regression{regressions === 1 ? "" : "s"}
          </Chip>
        ) : (
          <span className="text-[11px] text-idle">No regressions.</span>
        )
      }
    >
      {ordered.length === 0 ? (
        <Empty>This suite has no recorded results yet.</Empty>
      ) : (
        <div className="overflow-auto">
          <Table caption="Every case in this suite, newest result first">
            <THead>
              <TH />
              <TH>Case</TH>
              <TH>Result</TH>
              <TH>Outcome</TH>
              <TH>Change</TH>
              <TH numeric>Duration</TH>
              <TH>Metrics</TH>
              <TH numeric>Recorded</TH>
              <TH />
            </THead>
            <TBody>
              {ordered.map((entry) => {
                const row = entry.latest;
                const failedChecks = readChecks(row).filter((check) => !check.passed);
                const open = expanded === entry.caseId;
                return (
                  <Fragment key={entry.caseId}>
                    <TR>
                      <TD>
                        <button
                          type="button"
                          onClick={() => setExpanded(open ? null : entry.caseId)}
                          aria-expanded={open}
                          aria-label={`${open ? "Hide" : "Show"} the expectations for ${entry.caseId}`}
                          className="rounded px-1 text-[11px] text-muted transition-colors hover:bg-raised hover:text-fg"
                        >
                          {open ? "▾" : "▸"}
                        </button>
                      </TD>
                      <TD mono>
                        <span className="flex items-center gap-1.5">
                          {entry.caseId}
                          {trapIds.has(entry.caseId) && <Badge tone="warn">trap</Badge>}
                        </span>
                      </TD>
                      <TD>
                        <Chip tone={row.passed ? "ok" : "fail"} dot={false}>
                          {row.passed ? "✓ pass" : "✗ fail"}
                        </Chip>
                      </TD>
                      <TD>
                        <span className="text-muted">{row.outcome ?? "—"}</span>
                      </TD>
                      <TD>
                        <span
                          className={clsx(
                            "text-[11px]",
                            entry.transition === "regressed" && "text-fail",
                            entry.transition === "gained" && "text-ok",
                            entry.transition === "unchanged" && "text-idle",
                            entry.transition === "new" && "text-muted",
                          )}
                          title={
                            entry.previous
                              ? `Previous result recorded ${formatAbsolute(entry.previous.created_at)}`
                              : "This case has only one recorded result."
                          }
                        >
                          {TRANSITION_LABEL[entry.transition]}
                        </span>
                      </TD>
                      <TD numeric>
                        {row.duration_seconds == null
                          ? "—"
                          : formatClock(row.duration_seconds)}
                      </TD>
                      <TD>
                        <MetricsSummary metrics={row.metrics ?? {}} />
                      </TD>
                      <TD numeric title={formatAbsolute(row.created_at)}>
                        {formatRelative(row.created_at)}
                      </TD>
                      <TD className="text-right">
                        {row.run_id ? (
                          <Link
                            href={`/runs/${row.run_id}`}
                            className="rounded border border-line px-1.5 py-0.5 text-[11px] transition-colors hover:bg-raised"
                          >
                            Open run
                          </Link>
                        ) : (
                          <span
                            className="text-[11px] text-idle"
                            title="This result recorded no run id."
                          >
                            —
                          </span>
                        )}
                      </TD>
                    </TR>

                    {open && (
                      <TR>
                        <TD />
                        <TD className="pb-3" colSpan={8}>
                          <ChecksDetail
                            row={row}
                            failedChecks={failedChecks.length}
                          />
                        </TD>
                      </TR>
                    )}
                  </Fragment>
                );
              })}
            </TBody>
          </Table>
        </div>
      )}
    </Panel>
  );
}

/** The two or three metrics that fit in a cell; the rest are in the expansion. */
function MetricsSummary({ metrics }: { metrics: Record<string, unknown> }) {
  const entries = Object.entries(metrics).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number",
  );
  if (entries.length === 0) return <span className="text-idle">—</span>;
  return (
    <span className="flex flex-wrap gap-x-2 text-[11px] text-muted">
      {entries.slice(0, 3).map(([key, value]) => (
        <span key={key} className="tnum whitespace-nowrap">
          <span className="text-idle">{key}</span> {formatMetric(value)}
        </span>
      ))}
      {entries.length > 3 && (
        <span className="text-idle">+{entries.length - 3}</span>
      )}
    </span>
  );
}

/**
 * The expanded expectations for one case.
 *
 * Failed checks come first and keep their `detail` verbatim — that string is written by
 * the scorer to say precisely which expectation missed and by how much, and paraphrasing
 * it into a chip would throw away the only diagnostic in the row.
 */
function ChecksDetail({
  row,
  failedChecks,
}: {
  row: BenchmarkResultRead;
  failedChecks: number;
}) {
  const checks = readChecks(row).sort(
    (a, b) => Number(a.passed) - Number(b.passed),
  );
  const metrics = Object.entries(row.metrics ?? {});

  return (
    <div className="flex flex-col gap-2 rounded border border-line bg-raised/40 p-2">
      <p className="text-[11px] text-muted">
        {checks.length === 0
          ? "This result recorded no expectation checks."
          : `${checks.length} expectation${checks.length === 1 ? "" : "s"} checked, ${failedChecks} failed.`}
      </p>

      {checks.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {checks.map((check, index) => (
            <li
              key={`${check.name}-${index}`}
              className="flex items-baseline gap-2 text-[11px]"
            >
              <span className={check.passed ? "text-ok" : "text-fail"}>
                {check.passed ? "✓" : "✗"}
              </span>
              <span className="font-mono text-fg">{check.name}</span>
              {check.detail && <span className="min-w-0 text-muted">{check.detail}</span>}
            </li>
          ))}
        </ul>
      )}

      {metrics.length > 0 && (
        <dl className="flex flex-wrap gap-x-4 gap-y-0.5 border-t border-line pt-1.5 text-[11px]">
          {metrics.map(([key, value]) => (
            <span key={key} className="flex gap-1.5">
              <dt className="text-idle">{key}</dt>
              <dd className="tnum text-fg">
                {typeof value === "number" ? formatMetric(value) : String(value)}
              </dd>
            </span>
          ))}
        </dl>
      )}
    </div>
  );
}
