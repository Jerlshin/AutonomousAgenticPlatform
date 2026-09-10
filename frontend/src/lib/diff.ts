/**
 * A line diff, dependency-free (§8.7).
 *
 * "Computed with `lib/diff.ts` (a ~120-line LCS over lines) — no dependency for something
 * this small, and the server already sends a `diff` for the adjacent-revision case." The
 * viewer needs a diff for *any* pair of revisions the operator picks, not only adjacent
 * ones, which is why the server's unified diff is a fast path rather than the mechanism.
 *
 * The algorithm is a standard LCS over lines with two cheap guards that matter on real
 * input: identical common prefixes and suffixes are trimmed before the table is built,
 * because a 200-line file with a six-line change is 194 lines of exact match, and a
 * quadratic table over the whole file to find them is wasted work.
 */

export type DiffOp = "equal" | "insert" | "delete";

export interface DiffLine {
  op: DiffOp;
  /** 1-based line number in the "before" text; null for an insertion. */
  beforeLine: number | null;
  /** 1-based line number in the "after" text; null for a deletion. */
  afterLine: number | null;
  text: string;
}

export interface DiffStats {
  added: number;
  removed: number;
  /** Lines present in both, unchanged. */
  unchanged: number;
}

/**
 * Guard against the pathological case.
 *
 * The LCS table is O(n × m) in memory. Two 4 000-line files would be sixteen million
 * cells, which is both slow and pointless — a diff that large is not read line by line.
 * Past this, the two sides are reported as a wholesale replacement, which is honest.
 */
export const MAX_DIFF_LINES = 3000;

export function splitLines(text: string): string[] {
  if (text === "") return [];
  // A trailing newline is a line terminator, not an empty final line; keeping it would
  // report a phantom deletion whenever one side ends with one and the other does not.
  const normalised = text.replace(/\r\n/g, "\n").replace(/\n$/, "");
  return normalised.split("\n");
}

export function diffLines(before: string, after: string): DiffLine[] {
  const left = splitLines(before);
  const right = splitLines(after);

  if (left.length > MAX_DIFF_LINES || right.length > MAX_DIFF_LINES) {
    return [
      ...left.map<DiffLine>((text, index) => ({
        op: "delete",
        beforeLine: index + 1,
        afterLine: null,
        text,
      })),
      ...right.map<DiffLine>((text, index) => ({
        op: "insert",
        beforeLine: null,
        afterLine: index + 1,
        text,
      })),
    ];
  }

  // Trim the identical head.
  let head = 0;
  while (head < left.length && head < right.length && left[head] === right[head]) head++;

  // Trim the identical tail, without overlapping the head.
  let tail = 0;
  while (
    tail < left.length - head &&
    tail < right.length - head &&
    left[left.length - 1 - tail] === right[right.length - 1 - tail]
  ) {
    tail++;
  }

  const middleLeft = left.slice(head, left.length - tail);
  const middleRight = right.slice(head, right.length - tail);

  const out: DiffLine[] = [];
  for (let index = 0; index < head; index++) {
    out.push({
      op: "equal",
      beforeLine: index + 1,
      afterLine: index + 1,
      text: left[index]!,
    });
  }

  out.push(...lcsDiff(middleLeft, middleRight, head));

  for (let index = 0; index < tail; index++) {
    const beforeIndex = left.length - tail + index;
    const afterIndex = right.length - tail + index;
    out.push({
      op: "equal",
      beforeLine: beforeIndex + 1,
      afterLine: afterIndex + 1,
      text: left[beforeIndex]!,
    });
  }

  return out;
}

/** The LCS table over the changed middle, walked back into a line-by-line script. */
function lcsDiff(left: string[], right: string[], offset: number): DiffLine[] {
  const rows = left.length;
  const columns = right.length;

  // `table[i][j]` is the LCS length of left[i:] and right[j:].
  const table: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(columns + 1).fill(0),
  );
  for (let i = rows - 1; i >= 0; i--) {
    for (let j = columns - 1; j >= 0; j--) {
      table[i]![j] =
        left[i] === right[j]
          ? table[i + 1]![j + 1]! + 1
          : Math.max(table[i + 1]![j]!, table[i]![j + 1]!);
    }
  }

  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < rows && j < columns) {
    if (left[i] === right[j]) {
      out.push({
        op: "equal",
        beforeLine: offset + i + 1,
        afterLine: offset + j + 1,
        text: left[i]!,
      });
      i++;
      j++;
    } else if (table[i + 1]![j]! >= table[i]![j + 1]!) {
      out.push({
        op: "delete",
        beforeLine: offset + i + 1,
        afterLine: null,
        text: left[i]!,
      });
      i++;
    } else {
      out.push({
        op: "insert",
        beforeLine: null,
        afterLine: offset + j + 1,
        text: right[j]!,
      });
      j++;
    }
  }
  while (i < rows) {
    out.push({ op: "delete", beforeLine: offset + i + 1, afterLine: null, text: left[i]! });
    i++;
  }
  while (j < columns) {
    out.push({
      op: "insert",
      beforeLine: null,
      afterLine: offset + j + 1,
      text: right[j]!,
    });
    j++;
  }
  return out;
}

export function diffStats(lines: readonly DiffLine[]): DiffStats {
  let added = 0;
  let removed = 0;
  let unchanged = 0;
  for (const line of lines) {
    if (line.op === "insert") added++;
    else if (line.op === "delete") removed++;
    else unchanged++;
  }
  return { added, removed, unchanged };
}

export interface SideBySideRow {
  before: DiffLine | null;
  after: DiffLine | null;
}

/**
 * Pair the script into side-by-side rows.
 *
 * Deletions and the insertions that replace them are paired onto one row rather than
 * stacked, so a changed line reads as a change and not as a removal followed by an
 * unrelated addition several rows later.
 */
export function toSideBySide(lines: readonly DiffLine[]): SideBySideRow[] {
  const rows: SideBySideRow[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index]!;
    if (line.op === "equal") {
      rows.push({ before: line, after: line });
      index++;
      continue;
    }
    const deletions: DiffLine[] = [];
    const insertions: DiffLine[] = [];
    while (index < lines.length && lines[index]!.op === "delete") {
      deletions.push(lines[index]!);
      index++;
    }
    while (index < lines.length && lines[index]!.op === "insert") {
      insertions.push(lines[index]!);
      index++;
    }
    const height = Math.max(deletions.length, insertions.length);
    for (let offset = 0; offset < height; offset++) {
      rows.push({
        before: deletions[offset] ?? null,
        after: insertions[offset] ?? null,
      });
    }
  }
  return rows;
}

/**
 * Parse the unified diff the server already sends for adjacent revisions.
 *
 * Used as a fast path when the two selected revisions happen to be adjacent and the
 * payload carried a `diff`: it is the diff the backend actually produced, so rendering it
 * rather than recomputing avoids the two disagreeing over whitespace or context size.
 * Returns `null` for anything it does not recognise, and the caller falls back to `diffLines`.
 */
export function parseUnifiedDiff(diff: string): DiffLine[] | null {
  if (!diff.trim()) return null;
  const out: DiffLine[] = [];
  let beforeLine = 0;
  let afterLine = 0;
  let sawHunk = false;

  for (const raw of diff.split("\n")) {
    if (raw.startsWith("@@")) {
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      if (!match) return null;
      beforeLine = Number(match[1]);
      afterLine = Number(match[2]);
      sawHunk = true;
      continue;
    }
    if (!sawHunk) continue; // the ---/+++ header
    if (raw.startsWith("+")) {
      out.push({ op: "insert", beforeLine: null, afterLine, text: raw.slice(1) });
      afterLine++;
    } else if (raw.startsWith("-")) {
      out.push({ op: "delete", beforeLine, afterLine: null, text: raw.slice(1) });
      beforeLine++;
    } else if (raw.startsWith(" ") || raw === "") {
      out.push({ op: "equal", beforeLine, afterLine, text: raw.slice(1) });
      beforeLine++;
      afterLine++;
    } else if (raw.startsWith("\\")) {
      // "\ No newline at end of file" — metadata, not a line.
      continue;
    } else {
      return null;
    }
  }
  return sawHunk ? out : null;
}
