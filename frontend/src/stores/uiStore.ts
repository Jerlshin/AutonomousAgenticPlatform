"use client";

/**
 * Operator preferences: the third state system (§6.1).
 *
 * What belongs here is what should survive a reload and follow the *person*, not the run
 * — pane split sizes, console filters, the follow-tail toggle, a half-written task
 * prompt. What does not belong here is anything the stream owns: a value that arrives
 * over the socket has one source of truth, and a persisted copy of it is a second one
 * that goes stale in `localStorage`.
 *
 * Persistence is hand-rolled rather than `zustand/middleware/persist` for one reason that
 * matters in an App Router app: the store is read during the first client render, and a
 * middleware that hydrates asynchronously produces a visible flash of default layout on
 * every navigation. Reading `localStorage` once at module scope, guarded for the server,
 * costs one synchronous read and removes the flash.
 */

import { create } from "zustand";

const STORAGE_KEY = "pluton.ui.v1";

export type ConsoleFilter = "all" | "stdout" | "stderr" | "tokens";

export interface UiState {
  /** Fraction of the deck's height given to the graph/timeline row (§8.5.1). */
  topRowFraction: number;
  /** Fraction of the top row's width given to the graph pane. */
  graphWidthFraction: number;
  collapsed: Record<"graph" | "timeline" | "console" | "artifacts", boolean>;

  consoleFilter: ConsoleFilter;
  followTail: boolean;
  showTimestamps: boolean;
  renderAnsi: boolean;

  /** Per-task draft of the submission form, so a refresh does not lose a long prompt. */
  taskDrafts: Record<string, string>;
}

export interface UiActions {
  setTopRowFraction(fraction: number): void;
  setGraphWidthFraction(fraction: number): void;
  toggleCollapsed(pane: keyof UiState["collapsed"]): void;
  setConsoleFilter(filter: ConsoleFilter): void;
  setFollowTail(follow: boolean): void;
  toggleTimestamps(): void;
  toggleAnsi(): void;
  saveDraft(key: string, value: string): void;
  clearDraft(key: string): void;
}

const DEFAULTS: UiState = {
  topRowFraction: 0.46,
  graphWidthFraction: 0.38,
  collapsed: { graph: false, timeline: false, console: false, artifacts: false },
  consoleFilter: "all",
  followTail: true,
  showTimestamps: false,
  renderAnsi: false,
  taskDrafts: {},
};

function load(): UiState {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<UiState>;
    // Merged field by field rather than spread wholesale: a stored blob written by an
    // older version is missing keys this one reads, and `undefined` for `followTail`
    // would silently turn the console's default behaviour off.
    return {
      ...DEFAULTS,
      ...parsed,
      collapsed: { ...DEFAULTS.collapsed, ...(parsed.collapsed ?? {}) },
      taskDrafts: { ...(parsed.taskDrafts ?? {}) },
    };
  } catch {
    return DEFAULTS;
  }
}

function save(state: UiState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // A private-mode browser or a full quota. Preferences are a convenience; losing them
    // must never break the view they decorate.
  }
}

export const useUiStore = create<UiState & UiActions>((set, get) => {
  const persist = (patch: Partial<UiState>) => {
    set(patch);
    save({ ...get(), ...patch });
  };

  return {
    ...load(),

    setTopRowFraction: (fraction) =>
      persist({ topRowFraction: clamp(fraction, 0.2, 0.8) }),
    setGraphWidthFraction: (fraction) =>
      persist({ graphWidthFraction: clamp(fraction, 0.2, 0.7) }),
    toggleCollapsed: (pane) =>
      persist({ collapsed: { ...get().collapsed, [pane]: !get().collapsed[pane] } }),
    setConsoleFilter: (consoleFilter) => persist({ consoleFilter }),
    setFollowTail: (followTail) => persist({ followTail }),
    toggleTimestamps: () => persist({ showTimestamps: !get().showTimestamps }),
    toggleAnsi: () => persist({ renderAnsi: !get().renderAnsi }),
    saveDraft: (key, value) => persist({ taskDrafts: { ...get().taskDrafts, [key]: value } }),
    clearDraft: (key) => {
      const { [key]: _dropped, ...rest } = get().taskDrafts;
      persist({ taskDrafts: rest });
    },
  };
});

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
