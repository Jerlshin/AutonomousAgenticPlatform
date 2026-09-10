/**
 * `REPORT.md` structure checking and criteria cross-check (§8.8).
 *
 * Pure, React-free, and therefore testable on its own — the `lib/` rule from §5. Two
 * jobs, both of which exist because the report is model-generated prose about numbers
 * that are not:
 *
 * 1. **Section checking.** The Reporter must emit eight sections
 *    ([`AGENTS.md §7.8`](../../docs/AGENTS.md)). A missing one MUST be rendered as
 *    missing rather than silently omitted, because section 4 — what went wrong — is
 *    mandatory, and its absence is a reporter defect worth seeing rather than a shorter
 *    document.
 * 2. **Criteria cross-check.** The narrative criteria table is written by a model; the
 *    machine verdict is not. When they disagree, the table is wrong, and §8.8 requires
 *    that surface as a warning banner rather than resolve silently in the prose's favour.
 */

import type { CriterionRow } from "./types";

// ── Section structure ────────────────────────────────────────────────────────

export interface MandatedSection {
  number: number;
  /** The canonical title from `AGENTS.md §7.8`. */
  title: string;
  /**
   * Whether the Reporter is forbidden from omitting it. Only section 4 is called out
   * that way in the prompt ("Never omit this section"), and a run with no failures is
   * required to say so explicitly rather than drop the heading.
   */
  mandatory: boolean;
  /** What its absence means, shown next to the gap. */
  absence: string;
}

export const MANDATED_SECTIONS: readonly MandatedSection[] = [
  {
    number: 1,
    title: "Objective",
    mandatory: false,
    absence: "The restated task and the Planner's assumptions are missing.",
  },
  {
    number: 2,
    title: "Result",
    mandatory: false,
    absence: "The headline and the criteria table are missing.",
  },
  {
    number: 3,
    title: "Approach",
    mandatory: false,
    absence: "The plan as executed is missing.",
  },
  {
    number: 4,
    title: "What went wrong and how it was fixed",
    mandatory: true,
    absence:
      "This section is mandatory. A run with no execution failures must say so explicitly; " +
      "an absent section is a Reporter defect, not a clean run.",
  },
  {
    number: 5,
    title: "Results in detail",
    mandatory: false,
    absence: "Per-metric detail, plots and the MLflow link are missing.",
  },
  {
    number: 6,
    title: "Reproducing this run",
    mandatory: false,
    absence: "The command, dataset hash, seed and image digest are missing.",
  },
  {
    number: 7,
    title: "Limitations and next steps",
    mandatory: false,
    absence: "What was not tested and what the numbers do not show are missing.",
  },
  {
    number: 8,
    title: "Artifacts",
    mandatory: false,
    absence: "The artifact manifest is missing.",
  },
] as const;

export interface ParsedSection extends MandatedSection {
  present: boolean;
  /** The heading exactly as written, which may drift from the canonical title. */
  heading: string | null;
  /** The Markdown between this heading and the next, without the heading itself. */
  body: string;
  /** A stable id for the table-of-contents anchor. */
  anchor: string;
}

export interface ParsedReport {
  /** The `#` title line, if the document has one. */
  title: string | null;
  /** Everything above the first `##`, which carries the status/run/duration line. */
  preamble: string;
  sections: ParsedSection[];
  /** Sections the document contains that are not among the mandated eight. */
  extras: { heading: string; body: string; anchor: string }[];
  missingMandatory: ParsedSection[];
}

export function sectionAnchor(number: number): string {
  return `section-${number}`;
}

/** `## 4. What went wrong` → `{number: 4, rest: "What went wrong"}`; `null` if unnumbered. */
function splitHeading(heading: string): { number: number | null; rest: string } {
  const match = /^\s*(\d{1,2})\s*[.)\]:-]?\s*(.*)$/.exec(heading);
  if (!match) return { number: null, rest: heading.trim() };
  return { number: Number(match[1]), rest: (match[2] ?? "").trim() };
}

function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Split a report into its mandated sections.
 *
 * Matching is by leading section *number* first and title only as a fallback. The
 * Reporter is a small model writing prose: it rewords a heading far more often than it
 * renumbers one, so "## 4. Failures and fixes" must still resolve to section 4 rather
 * than be reported as both a missing section and an unexpected extra.
 */
export function parseReport(markdown: string): ParsedReport {
  const lines = markdown.split(/\r?\n/);

  let title: string | null = null;
  const preambleLines: string[] = [];
  const found: { number: number | null; heading: string; body: string[] }[] = [];
  let current: { number: number | null; heading: string; body: string[] } | null = null;
  let inFence = false;

  for (const line of lines) {
    // A `##` inside a fenced block is code, not a heading. Without this a report that
    // pastes a Markdown snippet splits into phantom sections.
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;

    const heading = inFence ? null : /^##\s+(.*)$/.exec(line);
    if (heading) {
      const text = (heading[1] ?? "").trim();
      const { number } = splitHeading(text);
      current = { number, heading: text, body: [] };
      found.push(current);
      continue;
    }

    if (!inFence && title === null && /^#\s+(.*)$/.test(line)) {
      title = (/^#\s+(.*)$/.exec(line)?.[1] ?? "").trim();
      continue;
    }

    if (current) current.body.push(line);
    else preambleLines.push(line);
  }

  const claimed = new Set<number>();
  const sections: ParsedSection[] = MANDATED_SECTIONS.map((mandated) => {
    let hit = found.find(
      (candidate, index) => candidate.number === mandated.number && !claimed.has(index),
    );
    if (!hit) {
      const wanted = normalise(mandated.title);
      hit = found.find((candidate, index) => {
        if (claimed.has(index) || candidate.number !== null) return false;
        const actual = normalise(splitHeading(candidate.heading).rest);
        return actual === wanted || actual.startsWith(wanted) || wanted.startsWith(actual);
      });
    }
    if (hit) claimed.add(found.indexOf(hit));

    return {
      ...mandated,
      present: hit !== undefined,
      heading: hit?.heading ?? null,
      body: (hit?.body ?? []).join("\n").trim(),
      anchor: sectionAnchor(mandated.number),
    };
  });

  const extras = found
    .filter((_, index) => !claimed.has(index))
    .map((section, index) => ({
      heading: section.heading,
      body: section.body.join("\n").trim(),
      anchor: `extra-${index}`,
    }));

  return {
    title,
    preamble: preambleLines.join("\n").trim(),
    sections,
    extras,
    missingMandatory: sections.filter((s) => s.mandatory && !s.present),
  };
}

// ── The criteria table ───────────────────────────────────────────────────────

export interface ReportedCriterion {
  criterion: string;
  target: string;
  achieved: string;
  status: string;
  /** `achieved` parsed as a number, or `null` when the report said "not measured". */
  achievedValue: number | null;
  /** What the status glyph claims: `true` pass, `false` miss, `null` unreadable. */
  claimsPass: boolean | null;
}

function cells(row: string): string[] {
  return row
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

const SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

/**
 * Pull the criteria table out of the report.
 *
 * Scans for the first GFM table whose header names a criterion column, rather than
 * assuming it sits in section 2 — a report that moves the table has still reported the
 * numbers, and refusing to find it there would raise a false mismatch.
 */
export function parseCriteriaTable(markdown: string): ReportedCriterion[] {
  const lines = markdown.split(/\r?\n/);

  for (let index = 0; index < lines.length - 1; index += 1) {
    const header = lines[index] ?? "";
    if (!header.includes("|")) continue;
    if (!SEPARATOR.test(lines[index + 1] ?? "")) continue;

    const columns = cells(header).map(normalise);
    const criterionColumn = columns.findIndex(
      (name) => name === "criterion" || name === "metric" || name === "criteria",
    );
    if (criterionColumn === -1) continue;

    const targetColumn = columns.findIndex((n) => n.includes("target") || n.includes("threshold"));
    const achievedColumn = columns.findIndex(
      (n) => n.includes("achieved") || n.includes("observed") || n.includes("actual"),
    );
    const statusColumn = columns.findIndex((n) => n.includes("status") || n.includes("result"));

    const rows: ReportedCriterion[] = [];
    for (let cursor = index + 2; cursor < lines.length; cursor += 1) {
      const line = lines[cursor] ?? "";
      if (!line.includes("|") || line.trim() === "") break;
      const parts = cells(line);
      const criterion = parts[criterionColumn] ?? "";
      if (criterion === "") continue;
      const achieved = achievedColumn === -1 ? "" : (parts[achievedColumn] ?? "");
      const status = statusColumn === -1 ? "" : (parts[statusColumn] ?? "");
      rows.push({
        criterion,
        target: targetColumn === -1 ? "" : (parts[targetColumn] ?? ""),
        achieved,
        status,
        achievedValue: parseNumeric(achieved),
        claimsPass: readStatus(status),
      });
    }
    if (rows.length > 0) return rows;
  }

  return [];
}

/** The first number in a cell, ignoring backticks, bold markers and trailing prose. */
export function parseNumeric(cell: string): number | null {
  const match = /-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?/.exec(cell.replace(/[`*_]/g, ""));
  if (!match) return null;
  const value = Number(match[0]);
  return Number.isFinite(value) ? value : null;
}

function readStatus(cell: string): boolean | null {
  const text = cell.toLowerCase();
  if (/✅|✔|\bpass(ed)?\b|\bmet\b|\bok\b/.test(text)) return true;
  if (/❌|✗|⚠|\bfail(ed)?\b|\bmiss(ed)?\b|\bnot met\b|\bbelow\b/.test(text)) return false;
  return null;
}

// ── Cross-check ──────────────────────────────────────────────────────────────

export type MismatchKind = "value" | "status" | "unknown-criterion" | "omitted";

export interface CriteriaMismatch {
  kind: MismatchKind;
  criterion: string;
  detail: string;
}

/** How many decimals the report chose to show, so it is judged at its own precision. */
function shownDecimals(cell: string): number {
  const match = /-?\d+\.(\d+)/.exec(cell.replace(/[`*_]/g, ""));
  return match ? (match[1] ?? "").length : 0;
}

function round(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

/**
 * Compare the report's narrative table against the machine verdict.
 *
 * Values are compared **at the precision the report chose to display**. The Reporter is
 * told not to round, but a table showing `0.97` against an observed `0.9737` is a
 * formatting choice, not a false claim; a table showing `0.98` against the same value is
 * a false claim. Comparing at full float precision would flag every legitimately
 * abbreviated table and train operators to ignore the banner — which would then be
 * missing when the numbers really do disagree.
 *
 * Only a verdict that has actually been observed can contradict anything, so an
 * un-evaluated run yields no mismatches rather than eight spurious ones.
 */
export function crossCheckCriteria(
  reported: readonly ReportedCriterion[],
  actual: readonly CriterionRow[],
): CriteriaMismatch[] {
  if (reported.length === 0 || actual.length === 0) return [];
  if (!actual.some((row) => row.verdictSeen)) return [];

  const mismatches: CriteriaMismatch[] = [];
  const matched = new Set<string>();

  for (const row of reported) {
    const key = normalise(row.criterion);
    const truth = actual.find(
      (candidate) =>
        normalise(candidate.metric) === key || normalise(candidate.id) === key,
    );

    if (!truth) {
      mismatches.push({
        kind: "unknown-criterion",
        criterion: row.criterion,
        detail: "The report lists a criterion the evaluator never scored.",
      });
      continue;
    }
    matched.add(truth.id);

    if (row.achievedValue !== null && truth.observed !== null) {
      const decimals = shownDecimals(row.achieved);
      if (round(row.achievedValue, decimals) !== round(truth.observed, decimals)) {
        mismatches.push({
          kind: "value",
          criterion: truth.metric,
          detail: `The report says ${row.achieved.trim()}; the evaluator observed ${truth.observed}.`,
        });
      }
    } else if (row.achievedValue !== null && truth.observed === null) {
      mismatches.push({
        kind: "value",
        criterion: truth.metric,
        detail: `The report says ${row.achieved.trim()}, but this metric was never measured.`,
      });
    }

    if (row.claimsPass !== null && truth.passed !== null && row.claimsPass !== truth.passed) {
      mismatches.push({
        kind: "status",
        criterion: truth.metric,
        detail: `The report marks this ${row.claimsPass ? "passed" : "failed"}; the evaluator scored it ${truth.passed ? "passed" : "failed"}.`,
      });
    }
  }

  for (const truth of actual) {
    if (!matched.has(truth.id) && truth.required) {
      mismatches.push({
        kind: "omitted",
        criterion: truth.metric,
        detail: "A required criterion is missing from the report's table.",
      });
    }
  }

  return mismatches;
}
