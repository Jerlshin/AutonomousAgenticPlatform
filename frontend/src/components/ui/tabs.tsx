"use client";

import clsx from "clsx";

/**
 * A segmented control.
 *
 * Rendered as a `radiogroup` rather than a row of buttons: the console's
 * **all · stdout · stderr · tokens** filter is a single choice from a fixed set, and that
 * is what arrow-key navigation and a screen reader's "2 of 4" announcement come from
 * (§11). A row of buttons would need every one of those behaviours re-implemented.
 */

export interface TabOption<T extends string> {
  value: T;
  label: string;
  /** Live counts belong on the tab that filters to them. */
  count?: number;
  title?: string;
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  className,
}: {
  options: readonly TabOption<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}) {
  const move = (delta: number) => {
    const index = options.findIndex((option) => option.value === value);
    const next = options[(index + delta + options.length) % options.length];
    if (next) onChange(next.value);
  };

  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={clsx("inline-flex overflow-hidden rounded border border-line", className)}
      onKeyDown={(cause) => {
        if (cause.key === "ArrowRight" || cause.key === "ArrowDown") {
          cause.preventDefault();
          move(1);
        } else if (cause.key === "ArrowLeft" || cause.key === "ArrowUp") {
          cause.preventDefault();
          move(-1);
        }
      }}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={option.title}
            // Only the selected option is in the tab order; the arrow keys move within
            // the group. That is the standard radiogroup pattern, and it keeps a
            // four-option filter from costing four tab stops.
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(option.value)}
            className={clsx(
              "px-2 py-0.5 text-[11px] transition-colors duration-150",
              active ? "bg-raised text-fg" : "text-muted hover:text-fg",
            )}
          >
            {option.label}
            {option.count !== undefined && (
              <span className="tnum ml-1 text-idle">{option.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
