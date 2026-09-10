"use client";

import { useEffect, useState } from "react";

/**
 * A value that settles `delayMs` after the input stops changing.
 *
 * Used by the console search (150 ms, §8.5.5) and the corpus playground. Debouncing the
 * *value* rather than the handler is deliberate: the input stays fully controlled and
 * responsive while the expensive derivation — filtering 5 000 console lines, or a
 * retrieval round trip — runs on the settled value.
 */
export function useDebouncedValue<T>(value: T, delayMs = 150): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const handle = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(handle);
  }, [value, delayMs]);

  return settled;
}
