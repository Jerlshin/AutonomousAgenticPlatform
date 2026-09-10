"use client";

import clsx from "clsx";
import { useEffect, useRef } from "react";

/**
 * A modal dialog that traps focus and restores it on close (§11).
 *
 * `role` is a parameter rather than a constant because the HITL gate console MUST be an
 * `alertdialog` (§8.6) — the run is waiting on a person, and that is the one thing in
 * this app that legitimately interrupts. Everything else is a plain `dialog`.
 *
 * Focus restoration matters more here than it looks: an operator who dismisses the gate
 * dialog to read the console (§8.6 requires that to be possible) has to land back on the
 * control they came from, or they lose their place in a four-pane deck.
 */

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  role = "dialog",
  width = "max-w-2xl",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  role?: "dialog" | "alertdialog";
  width?: string;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    focusables(panel)[0]?.focus();

    const onKeyDown = (cause: KeyboardEvent) => {
      if (cause.key === "Escape") {
        cause.stopPropagation();
        onClose();
        return;
      }
      if (cause.key !== "Tab") return;
      const targets = focusables(panelRef.current);
      if (targets.length === 0) return;
      const first = targets[0]!;
      const last = targets[targets.length - 1]!;
      // The trap is two wrap-arounds, not a scan on every keystroke: Tab off the last
      // control returns to the first, Shift+Tab off the first goes to the last.
      if (cause.shiftKey && document.activeElement === first) {
        cause.preventDefault();
        last.focus();
      } else if (!cause.shiftKey && document.activeElement === last) {
        cause.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      restoreRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/70 p-4 pt-[10vh]"
      onMouseDown={(cause) => {
        // Only a click that both starts and ends on the backdrop dismisses. A drag that
        // began inside the dialog and released outside it is a text selection.
        if (cause.target === cause.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role={role}
        aria-modal="true"
        aria-label={title}
        className={clsx(
          "flex max-h-[80vh] w-full flex-col rounded border border-line bg-raised shadow-2xl",
          width,
        )}
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">{title}</h2>
            {description && (
              <div className="mt-1 text-xs text-muted">{description}</div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded px-1 text-muted transition-colors hover:text-fg"
          >
            ✕
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-auto px-4 py-3">{children}</div>
        {footer && (
          <footer className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-line px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

function focusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return [
    ...root.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ];
}
