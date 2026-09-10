"use client";

import { useCallback, useRef, useState } from "react";
import { useUiStore } from "@/stores/uiStore";

/**
 * Scroll anchoring for the console (§8.5.5).
 *
 * The rule: follow-tail is on by default, MUST auto-disable when the viewer scrolls up,
 * and MUST re-enable when they scroll back to the bottom. Between those two, a
 * **Jump to latest (N new)** pill counts what arrived while they were reading.
 *
 * The preference itself lives in `uiStore` so it survives a reload — watching a training
 * log and reading back through one are different sessions, and the toggle should remember
 * which one you were in. What lives here is the transient half: whether the viewport is
 * currently at the bottom, and how many lines have landed since it stopped being.
 */
export function useFollowTail(lineCount: number) {
  const followTail = useUiStore((state) => state.followTail);
  const setFollowTail = useUiStore((state) => state.setFollowTail);

  const [detached, setDetached] = useState(false);
  const [pending, setPending] = useState(0);
  const anchorRef = useRef(lineCount);

  if (!detached && anchorRef.current !== lineCount) anchorRef.current = lineCount;
  if (detached && lineCount - anchorRef.current !== pending) {
    // Derived during render rather than in an effect: an effect would paint one frame
    // with a stale count, which on a fast stream is a pill that always reads low.
    setPending(Math.max(0, lineCount - anchorRef.current));
  }

  const onAtBottomChange = useCallback(
    (atBottom: boolean) => {
      setDetached(!atBottom);
      if (atBottom) {
        setPending(0);
        setFollowTail(true);
      } else {
        setFollowTail(false);
      }
    },
    [setFollowTail],
  );

  const reattach = useCallback(() => {
    setDetached(false);
    setPending(0);
    setFollowTail(true);
  }, [setFollowTail]);

  return {
    /** Whether the list should scroll new rows into view. */
    following: followTail && !detached,
    /** True while the viewer has scrolled away from the tail. */
    detached,
    /** How many lines arrived while detached — the number in the pill. */
    pending,
    onAtBottomChange,
    reattach,
    setFollowTail,
    followTail,
  };
}
