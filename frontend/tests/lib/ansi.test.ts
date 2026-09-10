import { describe, expect, it } from "vitest";
import { compileSearch, parseAnsi, stripAnsi } from "@/lib/ansi";

/** Written as an escape, never as a literal byte: a raw ESC in source is invisible. */
const ESC = "\u001b";

describe("stripAnsi", () => {
  it("removes SGR colours", () => {
    expect(stripAnsi(`${ESC}[31mfailed${ESC}[0m`)).toBe("failed");
  });

  it("removes the cursor movement tqdm emits, not only colours", () => {
    expect(stripAnsi(`${ESC}[2K${ESC}[1G 50%|#####     | 5/10`)).toBe(
      " 50%|#####     | 5/10",
    );
  });

  it("removes OSC window-title sequences", () => {
    expect(stripAnsi(`${ESC}]0;trainingdone`)).toBe("done");
  });

  it("leaves an escape-free line untouched and identical", () => {
    const line = "Fitting 5 folds for each of 12 candidates";
    expect(stripAnsi(line)).toBe(line);
  });
});

describe("parseAnsi", () => {
  it("returns one span for plain text", () => {
    expect(parseAnsi("hello")).toEqual([{ text: "hello" }]);
  });

  it("maps ANSI red onto the platform's fail tone", () => {
    const spans = parseAnsi(`ok ${ESC}[31mbad${ESC}[0m tail`);
    expect(spans.map((span) => span.text)).toEqual(["ok ", "bad", " tail"]);
    expect(spans[1]?.className).toBe("text-fail");
    expect(spans[2]?.className).toBeUndefined();
  });

  it("does not carry an unclosed colour past the end of the line", () => {
    const spans = parseAnsi(`${ESC}[32mstill green`);
    expect(spans).toHaveLength(1);
    expect(spans[0]?.className).toBe("text-ok");
    // The next line calls parseAnsi afresh, so state cannot leak into it.
    expect(parseAnsi("plain")[0]?.className).toBeUndefined();
  });
});

describe("compileSearch", () => {
  it("is case-insensitive substring by default", () => {
    const { test } = compileSearch("ValueError", false);
    expect(test("raise valueerror(x)")).toBe(true);
    expect(test("all good")).toBe(false);
  });

  it("matches nothing and reports invalid for a half-typed regex", () => {
    const { test, invalid } = compileSearch("foo(", true);
    // Typing `foo(` must not throw — that would make the regex toggle unusable.
    expect(invalid).toBe(true);
    expect(test("foo(bar)")).toBe(false);
  });

  it("compiles a complete regex", () => {
    const { test, invalid } = compileSearch("^Epoch \\d+", true);
    expect(invalid).toBe(false);
    expect(test("Epoch 12/50")).toBe(true);
  });
});
