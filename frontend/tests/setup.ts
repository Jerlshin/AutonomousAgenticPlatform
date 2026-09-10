import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library does not auto-clean under Vitest's globals, and a leaked tree between
// cases is how a passing suite starts asserting against the previous test's DOM.
afterEach(() => {
  cleanup();
});

// jsdom implements neither. The console pane's virtualizer measures a scroll container
// and the graph pane transitions on state change; without these, both throw before the
// assertion that matters.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
// `requestAnimationFrame` is replaced unconditionally, not only when jsdom lacks it.
// The frame batcher (§6.4) is on the critical path of every protocol test, and jsdom's
// own rAF is driven by a real 16 ms clock that `vi.useFakeTimers()` does not control —
// which makes "flush one frame" a sleep rather than an assertion.
globalThis.requestAnimationFrame = ((callback: FrameRequestCallback) =>
  setTimeout(() => callback(Date.now()), 0) as unknown as number) as typeof requestAnimationFrame;
globalThis.cancelAnimationFrame = ((handle: number) =>
  clearTimeout(handle)) as typeof cancelAnimationFrame;
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {};
}
