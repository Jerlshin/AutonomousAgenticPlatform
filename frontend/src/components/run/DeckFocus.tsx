"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";

/**
 * Cross-pane selection for the run deck.
 *
 * §8.5.3 and §8.5.4 both require it: clicking a graph node scrolls the timeline to that
 * node's most recent entry and filters the console to it; clicking a `sandbox_exec`
 * timeline entry filters the console to its `execution_id`. That is one piece of state
 * three panes read.
 *
 * A React Context is the right mechanism here and *not* a contradiction of §6.1's ruling
 * against Context: what that clause forbids is putting *run data* behind a provider,
 * because a value changing several times a second re-renders every consumer. This value
 * changes when somebody clicks something.
 */

export interface DeckFocus {
  /** Filters the console and highlights the graph node. */
  node: string | null;
  /** Filters the console to one container's output. */
  executionId: string | null;
  /** The timeline entry to scroll to and highlight. */
  entryId: string | null;

  focusNode(node: string | null): void;
  focusExecution(executionId: string | null, node?: string): void;
  focusEntry(entryId: string, node: string): void;
  clear(): void;

  /**
   * Increments when something asks for the gate dialog to be raised again.
   *
   * §8.6 requires the gate dialog to be dismissible without deciding — an operator needs
   * to read the console before approving — and requires the graph's `hitl_gate` marker to
   * reopen it. A counter rather than a boolean so a second click re-raises a dialog the
   * operator dismissed again.
   */
  gateRaises: number;
  raiseGate(): void;
}

const DeckFocusContext = createContext<DeckFocus | null>(null);

export function DeckFocusProvider({ children }: { children: React.ReactNode }) {
  const [node, setNode] = useState<string | null>(null);
  const [executionId, setExecutionId] = useState<string | null>(null);
  const [entryId, setEntryId] = useState<string | null>(null);
  const [gateRaises, setGateRaises] = useState(0);

  const focusNode = useCallback((next: string | null) => {
    setNode(next);
    // Selecting a node clears any narrower execution scope: the two together would show
    // one container's output filtered to a node that did not produce it, which is empty
    // and looks like a bug.
    setExecutionId(null);
    setEntryId(null);
  }, []);

  const focusExecution = useCallback((next: string | null, owner?: string) => {
    setExecutionId(next);
    setNode(owner ?? null);
    setEntryId(null);
  }, []);

  const focusEntry = useCallback((nextEntry: string, owner: string) => {
    setEntryId(nextEntry);
    setNode(owner);
    setExecutionId(null);
  }, []);

  const clear = useCallback(() => {
    setNode(null);
    setExecutionId(null);
    setEntryId(null);
  }, []);

  const raiseGate = useCallback(() => setGateRaises((count) => count + 1), []);

  const value = useMemo<DeckFocus>(
    () => ({
      node,
      executionId,
      entryId,
      focusNode,
      focusExecution,
      focusEntry,
      clear,
      gateRaises,
      raiseGate,
    }),
    [
      node,
      executionId,
      entryId,
      focusNode,
      focusExecution,
      focusEntry,
      clear,
      gateRaises,
      raiseGate,
    ],
  );

  return <DeckFocusContext.Provider value={value}>{children}</DeckFocusContext.Provider>;
}

export function useDeckFocus(): DeckFocus {
  const value = useContext(DeckFocusContext);
  if (!value) {
    throw new Error("useDeckFocus must be used inside a <DeckFocusProvider> (RunDeck).");
  }
  return value;
}
