"use client";

import { createContext, useContext } from "react";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import type { RunStoreApi, RunStreamState } from "./runStore";

/**
 * Puts one run's store in scope for the panes beneath it.
 *
 * §6.1 rules Context out for run *data* — a `token.delta` several times a second would
 * re-render every consumer beneath the provider. What travels through this context is the
 * store *handle*, which never changes identity for the life of the deck. Subscriptions
 * still go through `useStore`, so each pane re-renders only when its own slice changes.
 *
 * The context exists because §6.3 says "one store per mounted run stream" and the
 * dashboard means it literally: four active rows tail four runs at once, and a module
 * singleton would give them one `activeNode` between them.
 */
const RunStoreContext = createContext<RunStoreApi | null>(null);

export function RunStoreProvider({
  store,
  children,
}: {
  store: RunStoreApi;
  children: React.ReactNode;
}) {
  return <RunStoreContext.Provider value={store}>{children}</RunStoreContext.Provider>;
}

/** The store handle. For actions and one-off reads in effects, never for subscriptions. */
export function useRunStoreApi(): RunStoreApi {
  const store = useContext(RunStoreContext);
  if (!store) {
    throw new Error(
      "useRunStoreApi must be used inside a <RunStoreProvider>. The provider is mounted " +
        "by RunDeck, which owns the stream (docs/FRONTEND.md §8.5.1).",
    );
  }
  return store;
}

/** Subscribe to one derived value from the run in scope. */
export function useRunStore<T>(selector: (state: RunStreamState) => T): T {
  return useStore(useRunStoreApi(), selector);
}

/** Subscribe to several fields at once, shallow-compared (§6.5). */
export function useRunStoreShallow<T extends Record<string, unknown>>(
  selector: (state: RunStreamState) => T,
): T {
  return useStore(useRunStoreApi(), useShallow(selector));
}
