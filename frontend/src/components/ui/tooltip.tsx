"use client";

import clsx from "clsx";
import { useState } from "react";

/**
 * A hover/focus tooltip.
 *
 * Shown on focus as well as hover, which is the whole reason this exists rather than a
 * bare `title` attribute in the places that need rich content: a keyboard user must be
 * able to reach the graph node's model and visit count (§8.5.3), and `title` never fires
 * on focus.
 *
 * Where plain text is enough — a disabled button's reason, an absolute timestamp beside a
 * relative one — `title` is still the right tool and this component is overkill.
 */
export function Tooltip({
  content,
  children,
  side = "top",
  className,
}: {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: "top" | "bottom" | "right";
  className?: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <span
      className={clsx("relative inline-flex", className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {children}
      {open && (
        <span
          role="tooltip"
          className={clsx(
            "pointer-events-none absolute z-50 w-max max-w-xs rounded border border-line bg-raised px-2 py-1 text-[11px] leading-4 text-fg shadow-xl",
            side === "top" && "bottom-full left-1/2 mb-1 -translate-x-1/2",
            side === "bottom" && "left-1/2 top-full mt-1 -translate-x-1/2",
            side === "right" && "left-full top-1/2 ml-1 -translate-y-1/2",
          )}
        >
          {content}
        </span>
      )}
    </span>
  );
}
