import { describe, expect, it } from "vitest";
import {
  diffLines,
  diffStats,
  MAX_DIFF_LINES,
  parseUnifiedDiff,
  splitLines,
  toSideBySide,
} from "@/lib/diff";

describe("splitLines", () => {
  it("treats a trailing newline as a terminator, not an empty final line", () => {
    // Otherwise one side ending with a newline reports a phantom deletion.
    expect(splitLines("a\nb\n")).toEqual(["a", "b"]);
    expect(splitLines("a\nb")).toEqual(["a", "b"]);
  });

  it("normalises CRLF", () => {
    expect(splitLines("a\r\nb")).toEqual(["a", "b"]);
  });

  it("returns nothing for an empty document", () => {
    expect(splitLines("")).toEqual([]);
  });
});

describe("diffLines", () => {
  it("reports an unchanged file as entirely equal", () => {
    const lines = diffLines("a\nb\nc", "a\nb\nc");
    expect(diffStats(lines)).toEqual({ added: 0, removed: 0, unchanged: 3 });
  });

  it("finds a one-line change inside a long identical file", () => {
    const before = Array.from({ length: 200 }, (_, i) => `line ${i}`).join("\n");
    const after = before.replace("line 100", "line 100 changed");
    const lines = diffLines(before, after);
    const stats = diffStats(lines);
    expect(stats.added).toBe(1);
    expect(stats.removed).toBe(1);
    expect(stats.unchanged).toBe(199);
  });

  it("numbers both sides so a row can be traced back to a file", () => {
    const lines = diffLines("a\nb", "a\nc");
    const removed = lines.find((line) => line.op === "delete");
    const added = lines.find((line) => line.op === "insert");
    expect(removed).toMatchObject({ beforeLine: 2, afterLine: null, text: "b" });
    expect(added).toMatchObject({ beforeLine: null, afterLine: 2, text: "c" });
  });

  it("handles an insertion at the top and a deletion at the bottom", () => {
    const lines = diffLines("b\nc\nd", "a\nb\nc");
    expect(diffStats(lines)).toEqual({ added: 1, removed: 1, unchanged: 2 });
  });

  it("treats a pathologically large pair as a wholesale replacement", () => {
    const before = Array.from({ length: MAX_DIFF_LINES + 1 }, (_, i) => `a${i}`).join("\n");
    const after = "b";
    const lines = diffLines(before, after);
    // A quadratic table over sixteen million cells is not worth building for a diff
    // nobody reads line by line.
    expect(diffStats(lines).unchanged).toBe(0);
    expect(diffStats(lines).added).toBe(1);
  });
});

describe("toSideBySide", () => {
  it("pairs a deletion with the insertion that replaced it", () => {
    const rows = toSideBySide(diffLines("a\nold\nb", "a\nnew\nb"));
    const changed = rows.find((row) => row.before?.op === "delete");
    // Stacked rather than paired, a changed line reads as a removal followed by an
    // unrelated addition several rows later.
    expect(changed?.before?.text).toBe("old");
    expect(changed?.after?.text).toBe("new");
  });

  it("pads the shorter side when the counts differ", () => {
    const rows = toSideBySide(diffLines("old", "new1\nnew2"));
    expect(rows).toHaveLength(2);
    expect(rows[1]?.before).toBeNull();
    expect(rows[1]?.after?.text).toBe("new2");
  });
});

describe("parseUnifiedDiff", () => {
  it("reads a hunk header and numbers both sides from it", () => {
    const lines = parseUnifiedDiff(
      ["--- a/main.py", "+++ b/main.py", "@@ -10,3 +10,3 @@", " keep", "-old", "+new"].join(
        "\n",
      ),
    );
    expect(lines).not.toBeNull();
    expect(lines![0]).toMatchObject({ op: "equal", beforeLine: 10, afterLine: 10 });
    expect(lines![1]).toMatchObject({ op: "delete", beforeLine: 11, text: "old" });
    expect(lines![2]).toMatchObject({ op: "insert", afterLine: 11, text: "new" });
  });

  it("ignores the no-newline marker", () => {
    const lines = parseUnifiedDiff(
      ["@@ -1 +1 @@", "-a", "\\ No newline at end of file", "+b"].join("\n"),
    );
    expect(lines).toHaveLength(2);
  });

  it("returns null for something that is not a unified diff", () => {
    // The caller then falls back to computing the diff itself, which is always correct.
    expect(parseUnifiedDiff("just some text")).toBeNull();
    expect(parseUnifiedDiff("")).toBeNull();
  });
});
