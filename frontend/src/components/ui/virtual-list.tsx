"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import clsx from "clsx";
import { useEffect, useImperativeHandle, useRef, type Ref } from "react";

/**
 * The TanStack Virtual wrapper (§9.2).
 *
 * Two rules from §9.2 are enforced here rather than left to each caller, because both
 * fail silently when they are missed:
 *
 * * **fixed row height, no measurement.** Measuring per row is the expensive part of a
 *   virtualizer, and the console's rows are `leading-5` — 20 px — by construction. A
 *   caller that needs variable heights should say so explicitly with `estimateSize` and
 *   `measure`, and accept the cost.
 * * **the container needs an explicit height from the grid.** `height: 100%` inside an
 *   `auto` grid row collapses to zero and renders nothing, with no error and no visible
 *   symptom other than an empty pane. The wrapper takes `h-full` and documents that its
 *   parent must be a `minmax(0, …)` row.
 *
 * Auto-scroll is conditional on being within `BOTTOM_EPSILON_PX` of the bottom, because
 * auto-scrolling away from what someone is reading is the classic log-viewer bug.
 */

/** Within this many pixels of the bottom counts as "at the bottom" (§9.2). */
export const BOTTOM_EPSILON_PX = 8;

export interface VirtualListHandle {
  scrollToIndex(index: number, align?: "start" | "center" | "end" | "auto"): void;
  scrollToBottom(): void;
  isAtBottom(): boolean;
}

export function VirtualList<T>({
  items,
  rowHeight,
  renderRow,
  className,
  overscan = 20,
  followTail = false,
  onAtBottomChange,
  ariaLabel,
  handleRef,
  measure = false,
}: {
  items: readonly T[];
  rowHeight: number;
  renderRow: (item: T, index: number) => React.ReactNode;
  className?: string;
  overscan?: number;
  /** When true, new items scroll into view — but only if already at the bottom. */
  followTail?: boolean;
  onAtBottomChange?: (atBottom: boolean) => void;
  ariaLabel?: string;
  handleRef?: Ref<VirtualListHandle>;
  /** Opt into per-row measurement. Costs a layout read per visible row; use sparingly. */
  measure?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan,
  });

  const isAtBottom = () => {
    const element = scrollRef.current;
    if (!element) return true;
    return (
      element.scrollHeight - element.scrollTop - element.clientHeight <= BOTTOM_EPSILON_PX
    );
  };

  useImperativeHandle(handleRef, () => ({
    scrollToIndex: (index, align = "center") => virtualizer.scrollToIndex(index, { align }),
    scrollToBottom: () => {
      const element = scrollRef.current;
      if (element) element.scrollTop = element.scrollHeight;
    },
    isAtBottom,
  }));

  // Follow the tail only while the viewer has not scrolled away from it.
  useEffect(() => {
    if (!followTail || items.length === 0) return;
    if (!atBottomRef.current) return;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [followTail, items.length]);

  return (
    <div
      ref={scrollRef}
      role={ariaLabel ? "log" : undefined}
      aria-label={ariaLabel}
      onScroll={() => {
        const bottom = isAtBottom();
        if (bottom !== atBottomRef.current) {
          atBottomRef.current = bottom;
          onAtBottomChange?.(bottom);
        }
      }}
      // `h-full` requires the parent to be a `minmax(0, …)` grid row or a `min-h-0` flex
      // child. In an `auto` row this collapses to zero height and renders nothing.
      className={clsx("h-full overflow-auto", className)}
    >
      <div
        style={{ height: `${virtualizer.getTotalSize()}px`, position: "relative" }}
      >
        {virtualizer.getVirtualItems().map((row) => {
          const item = items[row.index];
          if (item === undefined) return null;
          return (
            <div
              key={row.key}
              data-index={row.index}
              ref={measure ? virtualizer.measureElement : undefined}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: measure ? undefined : `${row.size}px`,
                transform: `translateY(${row.start}px)`,
              }}
            >
              {renderRow(item, row.index)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
