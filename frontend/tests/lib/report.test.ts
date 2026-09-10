/**
 * `REPORT.md` structure checking and the criteria cross-check (§8.8, §13).
 *
 * The cases that matter here are the dishonest ones: a report missing its mandatory
 * section, and a report whose narrative table disagrees with the machine verdict. Both
 * are model-generated defects the viewer exists to surface, and both are invisible if the
 * parser is lenient in the wrong place.
 */

import { describe, expect, it } from "vitest";
import {
  crossCheckCriteria,
  MANDATED_SECTIONS,
  parseCriteriaTable,
  parseNumeric,
  parseReport,
} from "@/lib/report";
import type { CriterionRow } from "@/lib/types";

function criterion(overrides: Partial<CriterionRow> = {}): CriterionRow {
  return {
    id: "c1",
    metric: "accuracy",
    comparator: ">=",
    threshold: 0.9,
    tolerance: 0,
    required: true,
    weight: 1,
    rationale: "",
    observed: 0.9737,
    passed: true,
    note: "",
    verdictSeen: true,
    ...overrides,
  };
}

const COMPLETE = `# Breast cancer classifier

**Status:** SUCCEEDED · **Run:** \`abc\` · **Duration:** 04:12

## 1. Objective
Classify the dataset.

## 2. Result
It worked.

| Criterion | Target | Achieved | Status |
|---|---|---|---|
| accuracy | >= 0.90 | 0.9737 | ✅ Pass |

## 3. Approach
A gradient boosting model.

## 4. What went wrong and how it was fixed
No execution failures occurred.

## 5. Results in detail
Everything.

## 6. Reproducing this run
A command.

## 7. Limitations and next steps
Honest limitations.

## 8. Artifacts
| File | Type | Size | SHA-256 |
|---|---|---|---|
| main.py | code | 1 kB | abc |
`;

describe("parseReport", () => {
  it("finds all eight mandated sections in a complete report", () => {
    const parsed = parseReport(COMPLETE);
    expect(parsed.sections).toHaveLength(MANDATED_SECTIONS.length);
    expect(parsed.sections.every((section) => section.present)).toBe(true);
    expect(parsed.missingMandatory).toHaveLength(0);
  });

  it("reads the document title and preamble", () => {
    const parsed = parseReport(COMPLETE);
    expect(parsed.title).toBe("Breast cancer classifier");
    expect(parsed.preamble).toContain("**Status:** SUCCEEDED");
  });

  it("reports a missing mandatory section rather than closing the gap", () => {
    const withoutFour = COMPLETE.replace(
      /## 4\. What went wrong and how it was fixed\nNo execution failures occurred\.\n\n/,
      "",
    );
    const parsed = parseReport(withoutFour);
    const four = parsed.sections.find((section) => section.number === 4)!;
    expect(four.present).toBe(false);
    expect(parsed.missingMandatory.map((section) => section.number)).toEqual([4]);
    // The other seven are unaffected — an omission must not shift the numbering.
    expect(parsed.sections.filter((section) => section.present)).toHaveLength(7);
  });

  it("matches a reworded heading by its number", () => {
    const reworded = COMPLETE.replace(
      "## 4. What went wrong and how it was fixed",
      "## 4. Failures and their fixes",
    );
    const parsed = parseReport(reworded);
    const four = parsed.sections.find((section) => section.number === 4)!;
    expect(four.present).toBe(true);
    expect(four.heading).toBe("4. Failures and their fixes");
    // And it must not also be reported as an unexpected extra section.
    expect(parsed.extras).toHaveLength(0);
  });

  it("matches an unnumbered heading by its title", () => {
    const parsed = parseReport("## Objective\nSomething.\n");
    expect(parsed.sections.find((section) => section.number === 1)!.present).toBe(true);
  });

  it("ignores a '##' inside a fenced code block", () => {
    const parsed = parseReport(
      "## 1. Objective\nSee below.\n\n```python\n## 4. not a heading\nx = 1\n```\n",
    );
    expect(parsed.sections.find((section) => section.number === 4)!.present).toBe(false);
    expect(parsed.sections.find((section) => section.number === 1)!.body).toContain(
      "## 4. not a heading",
    );
  });

  it("collects headings outside the mandated eight as extras", () => {
    const parsed = parseReport(`${COMPLETE}\n## Appendix\nExtra material.\n`);
    expect(parsed.extras.map((extra) => extra.heading)).toEqual(["Appendix"]);
  });
});

describe("parseCriteriaTable", () => {
  it("extracts the criteria rows", () => {
    const rows = parseCriteriaTable(COMPLETE);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      criterion: "accuracy",
      achievedValue: 0.9737,
      claimsPass: true,
    });
  });

  it("reads a miss glyph as a failure claim", () => {
    const table = [
      "| Criterion | Target | Achieved | Status |",
      "|---|---|---|---|",
      "| roc_auc | >= 0.99 | 0.9401 | ⚠️ Miss (stretch goal) |",
    ].join("\n");
    expect(parseCriteriaTable(table)[0]!.claimsPass).toBe(false);
  });

  it("returns nothing when the document has no criteria table", () => {
    expect(parseCriteriaTable("## 2. Result\nIt worked.\n")).toEqual([]);
  });

  it("skips a table that is not the criteria table", () => {
    const rows = parseCriteriaTable(
      "| File | Type |\n|---|---|\n| main.py | code |\n",
    );
    expect(rows).toEqual([]);
  });

  it("parses a value out of a decorated cell", () => {
    expect(parseNumeric("`0.9737`")).toBe(0.9737);
    expect(parseNumeric("**0.81** (macro)")).toBe(0.81);
    expect(parseNumeric("not measured")).toBeNull();
  });
});

describe("crossCheckCriteria", () => {
  it("passes a table that agrees with the verdict", () => {
    const reported = parseCriteriaTable(COMPLETE);
    expect(crossCheckCriteria(reported, [criterion()])).toEqual([]);
  });

  it("accepts a value abbreviated to fewer decimals", () => {
    // 0.97 against an observed 0.9737 is a formatting choice, not a false claim, and
    // flagging it would train operators to ignore the banner.
    const reported = parseCriteriaTable(
      "| Criterion | Target | Achieved | Status |\n|---|---|---|---|\n| accuracy | >= 0.90 | 0.97 | ✅ Pass |",
    );
    expect(crossCheckCriteria(reported, [criterion()])).toEqual([]);
  });

  it("flags a value that differs at the precision the report chose", () => {
    const reported = parseCriteriaTable(
      "| Criterion | Target | Achieved | Status |\n|---|---|---|---|\n| accuracy | >= 0.90 | 0.98 | ✅ Pass |",
    );
    const mismatches = crossCheckCriteria(reported, [criterion()]);
    expect(mismatches).toHaveLength(1);
    expect(mismatches[0]!.kind).toBe("value");
  });

  it("flags a table that claims a pass the evaluator scored as a failure", () => {
    const reported = parseCriteriaTable(
      "| Criterion | Target | Achieved | Status |\n|---|---|---|---|\n| accuracy | >= 0.90 | 0.8100 | ✅ Pass |",
    );
    const mismatches = crossCheckCriteria(reported, [
      criterion({ observed: 0.81, passed: false }),
    ]);
    expect(mismatches.map((m) => m.kind)).toContain("status");
  });

  it("flags a number reported for a metric that was never measured", () => {
    const reported = parseCriteriaTable(
      "| Criterion | Target | Achieved | Status |\n|---|---|---|---|\n| accuracy | >= 0.90 | 0.9500 | ✅ Pass |",
    );
    const mismatches = crossCheckCriteria(reported, [
      criterion({ observed: null, passed: false }),
    ]);
    expect(mismatches[0]!.detail).toContain("never measured");
  });

  it("flags a required criterion the report omitted", () => {
    const reported = parseCriteriaTable(COMPLETE);
    const mismatches = crossCheckCriteria(reported, [
      criterion(),
      criterion({ id: "c2", metric: "f1_macro", observed: 0.8, required: true }),
    ]);
    expect(mismatches).toHaveLength(1);
    expect(mismatches[0]).toMatchObject({ kind: "omitted", criterion: "f1_macro" });
  });

  it("does not flag an optional criterion the report omitted", () => {
    const reported = parseCriteriaTable(COMPLETE);
    const mismatches = crossCheckCriteria(reported, [
      criterion(),
      criterion({ id: "c2", metric: "roc_auc", observed: 0.99, required: false }),
    ]);
    expect(mismatches).toEqual([]);
  });

  it("flags a criterion the evaluator never scored", () => {
    const reported = parseCriteriaTable(
      "| Criterion | Target | Achieved | Status |\n|---|---|---|---|\n| invented | >= 0.5 | 0.99 | ✅ Pass |",
    );
    expect(crossCheckCriteria(reported, [criterion()])[0]!.kind).toBe(
      "unknown-criterion",
    );
  });

  it("stays silent before a verdict exists", () => {
    // An un-evaluated run must not produce a wall of spurious mismatches.
    const reported = parseCriteriaTable(COMPLETE);
    const pending = [criterion({ observed: null, passed: null, verdictSeen: false })];
    expect(crossCheckCriteria(reported, pending)).toEqual([]);
  });
});
