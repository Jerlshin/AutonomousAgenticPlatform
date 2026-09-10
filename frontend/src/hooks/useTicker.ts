"use client";

import { useSyncExternalStore } from "react";

/**
 * One 1 Hz clock for the whole deck (§9.3).
 *
 * "Live counters MUST share one 1 Hz interval published through the store or a context,
 * never one `setInterval` per row." A twenty-entry timeline with a per-row timer is
 * twenty timers firing at twenty slightly different phases, which is both twenty wake-ups
 * a second and a column of numbers that update out of step with each other.
 *
 * Implemented with `useSyncExternalStore` over a single module-level interval that is
 * started on the first subscriber and cleared on the last, so a page with no live
 * counters holds no timer at all.
 */

type Listener = () => void;

const listeners = new Set<Listener>();
let handle: ReturnType<typeof setInterval> | null = null;
let now = Date.now();

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  if (handle === null) {
    // Aligned to the next whole second so the first tick is not a fraction of one, which
    // would make an elapsed counter jump by two on its first update.
    handle = setInterval(() => {
      now = Date.now();
      for (const notify of listeners) notify();
    }, 1000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && handle !== null) {
      clearInterval(handle);
      handle = null;
    }
  };
}

function getSnapshot(): number {
  return now;
}

/**
 * The server snapshot is a constant.
 *
 * A server render has no clock to tick, and returning `Date.now()` there would produce a
 * hydration mismatch on every elapsed counter in the deck.
 */
function getServerSnapshot(): number {
  return 0;
}

/** The current time, re-rendering the caller once a second. */
export function useSecondTicker(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
