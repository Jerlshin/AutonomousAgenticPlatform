import clsx from "clsx";
import type { Tone } from "@/lib/types";

/**
 * The shared primitives, restyled to the §10 tokens.
 *
 * §2.1 vendors shadcn/ui into `components/ui/` rather than importing a component package,
 * and restyling on the way in is part of that: a component still carrying default shadcn
 * greys is a review comment, not a merge.
 *
 * One rule runs through all of them and is worth stating once: **state is never encoded
 * by colour alone** (§10). Every `Dot`, `Chip` and `StatusGlyph` carries a glyph or a
 * label as well as a tone, because the five status colours include a red/green pair and a
 * red/green-only scorecard is unreadable for a meaningful fraction of engineers.
 */

export type { Tone };

const DOT_TONES: Record<Tone, string> = {
  running: "bg-running",
  ok: "bg-ok",
  warn: "bg-warn",
  fail: "bg-fail",
  idle: "bg-idle",
};

const TEXT_TONES: Record<Tone, string> = {
  running: "text-running",
  ok: "text-ok",
  warn: "text-warn",
  fail: "text-fail",
  idle: "text-idle",
};

const BORDER_TONES: Record<Tone, string> = {
  running: "border-running/40",
  ok: "border-ok/40",
  warn: "border-warn/40",
  fail: "border-fail/40",
  idle: "border-line",
};

export function toneText(tone: Tone): string {
  return TEXT_TONES[tone];
}

/** The glyph that carries a status without relying on its colour. */
export const GLYPH: Record<Tone, string> = {
  running: "▶",
  ok: "✓",
  warn: "⚠",
  fail: "✗",
  idle: "◦",
};

export function Panel({
  title,
  count,
  right,
  className,
  bodyClassName,
  children,
  as: Element = "section",
  labelledBy,
}: {
  title: string;
  /** §8.5.1: "pane headers MUST carry a live count". */
  count?: React.ReactNode;
  right?: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  children: React.ReactNode;
  as?: "section" | "div";
  labelledBy?: string;
}) {
  return (
    <Element
      className={clsx("flex min-h-0 min-w-0 flex-col bg-surface", className)}
      aria-labelledby={labelledBy}
    >
      <header className="flex h-8 shrink-0 items-center justify-between gap-2 border-b border-line px-3">
        <h2
          id={labelledBy}
          className="flex items-baseline gap-2 truncate text-[11px] font-semibold uppercase tracking-widest text-muted"
        >
          {title}
          {count !== undefined && (
            <span className="tnum font-normal normal-case tracking-normal text-idle">
              {count}
            </span>
          )}
        </h2>
        {right && <div className="flex shrink-0 items-center gap-1.5">{right}</div>}
      </header>
      <div className={clsx("min-h-0 flex-1", bodyClassName ?? "overflow-auto")}>
        {children}
      </div>
    </Element>
  );
}

export function Dot({
  tone,
  pulse,
  label,
}: {
  tone: Tone;
  pulse?: boolean;
  /** Announced to a screen reader, which cannot see the colour at all. */
  label?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-block h-2 w-2 shrink-0 rounded-full",
        DOT_TONES[tone],
        pulse && "pulse",
      )}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}

export function Chip({
  children,
  tone = "idle",
  title,
  dot = true,
  className,
}: {
  children: React.ReactNode;
  tone?: Tone;
  title?: string;
  dot?: boolean;
  className?: string;
}) {
  return (
    <span
      title={title}
      className={clsx(
        "tnum inline-flex items-center gap-1.5 whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px]",
        BORDER_TONES[tone],
        tone === "idle" ? "text-muted" : TEXT_TONES[tone],
        className,
      )}
    >
      {dot && <Dot tone={tone} />}
      {children}
    </span>
  );
}

// Written out rather than interpolated. Tailwind v4 scans source text for class names, so
// a template literal like `bg-${tone}/15` produces a class that is never generated and a
// badge that is silently transparent.
const BADGE_TONES: Record<Tone, string> = {
  running: "bg-running/15 text-running",
  ok: "bg-ok/15 text-ok",
  warn: "bg-warn/15 text-warn",
  fail: "bg-fail/15 text-fail",
  idle: "bg-raised text-muted",
};

export function Badge({
  children,
  tone = "idle",
  title,
}: {
  children: React.ReactNode;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={clsx(
        "inline-flex items-center rounded px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider",
        BADGE_TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  tone = "default",
  title,
  type = "button",
  size = "sm",
  className,
  ...rest
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  /** The tooltip. On a disabled control this MUST say *why* it is disabled (§2.4). */
  title?: string;
  tone?: "default" | "primary" | "danger";
  type?: "button" | "submit";
  size?: "sm" | "md";
  className?: string;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onClick" | "type" | "title">) {
  return (
    <button
      {...rest}
      type={type === "submit" ? "submit" : "button"}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded border transition-colors duration-150",
        "disabled:cursor-not-allowed disabled:opacity-40",
        size === "sm" ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm",
        tone === "danger" && "border-fail/40 text-fail hover:bg-fail/10",
        tone === "primary" &&
          "border-running/50 bg-running/15 text-running hover:bg-running/25",
        tone === "default" && "border-line text-fg hover:bg-raised",
        className,
      )}
    >
      {children}
    </button>
  );
}

/**
 * An empty state that carries the next action, not just an absence (§8.1).
 *
 * "No tasks yet" tells someone the query worked. "No tasks yet — create one" tells them
 * what to do about it, which is the only reason they are reading an empty pane.
 */
export function Empty({
  children,
  action,
}: {
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-3 py-8 text-center">
      <p className="text-xs text-muted">{children}</p>
      {action}
    </div>
  );
}

/** A pane-sized failure with a retry. Never a bare stack trace. */
export function ErrorState({
  title,
  message,
  onRetry,
}: {
  title: string;
  message?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-3 py-8 text-center">
      <p className="text-xs font-semibold text-fail">
        {GLYPH.fail} {title}
      </p>
      {message && <p className="max-w-md text-xs text-muted">{message}</p>}
      {onRetry && (
        <Button onClick={onRetry} size="sm">
          Try again
        </Button>
      )}
    </div>
  );
}

/**
 * A loading skeleton that renders the *shape* of what is coming, never a spinner (§8.1).
 *
 * A spinner says "something is happening"; a skeleton says "a table with four rows is
 * happening", and the difference is whether the layout moves when the data lands.
 */
export function Skeleton({
  rows = 3,
  className,
}: {
  rows?: number;
  className?: string;
}) {
  return (
    <div className={clsx("flex flex-col gap-2 p-3", className)} aria-hidden>
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="h-4 rounded bg-raised"
          style={{ width: `${100 - (index % 3) * 12}%` }}
        />
      ))}
    </div>
  );
}

/**
 * Copy-on-click, with a confirmation.
 *
 * §11 requires every id, hash and path to be copyable, and §8.5.6 requires artifact
 * hashes specifically — artifact identity is what makes a claim reproducible, and an
 * operator who has to select twelve characters of a hash by hand will not check it.
 */
export function Copyable({
  value,
  children,
  className,
  title,
}: {
  value: string;
  children?: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title ?? `Copy ${value}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value);
      }}
      className={clsx(
        "cursor-copy font-mono text-[11px] text-muted transition-colors hover:text-fg",
        className,
      )}
    >
      {children ?? value}
    </button>
  );
}

/** A labelled figure. The dashboard's stat tiles and the deck's budget readouts. */
export function Stat({
  label,
  value,
  hint,
  tone = "idle",
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: Tone;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 border border-line bg-surface px-3 py-2">
      <span className="text-[10px] font-semibold uppercase tracking-widest text-muted">
        {label}
      </span>
      <span className={clsx("tnum truncate text-xl font-semibold", TEXT_TONES[tone])}>
        {value}
      </span>
      {hint && <span className="truncate text-[10px] text-idle">{hint}</span>}
    </div>
  );
}

/**
 * A banner for something the operator must know but need not act on immediately.
 *
 * The truncation notices in §3.4 are the canonical use: history was dropped, the pane is
 * still usable, and pretending otherwise is the failure mode.
 */
export function Banner({
  tone = "warn",
  children,
  className,
}: {
  tone?: Tone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={clsx(
        "flex items-center gap-2 border-b px-3 py-1 text-[11px]",
        BORDER_TONES[tone],
        TEXT_TONES[tone],
        tone === "warn" && "bg-warn/5",
        tone === "fail" && "bg-fail/5",
        className,
      )}
    >
      <span aria-hidden>{GLYPH[tone]}</span>
      <span className="min-w-0 flex-1">{children}</span>
    </div>
  );
}
