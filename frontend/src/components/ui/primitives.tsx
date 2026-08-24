import clsx from "clsx";

/**
 * The handful of primitives the run view needs.
 *
 * §18.1 vendors shadcn/ui into `components/ui/` rather than importing a package. These
 * are the pieces this phase actually uses; the rest arrive with the pages that need them,
 * which keeps the vendored surface to code that is on screen.
 */

export function Panel({
  title,
  right,
  className,
  bodyClassName,
  children,
}: {
  title: string;
  right?: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  children: React.ReactNode;
}) {
  return (
    <section className={clsx("flex min-h-0 flex-col bg-surface", className)}>
      <header className="flex h-8 shrink-0 items-center justify-between border-b border-line px-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-widest text-muted">
          {title}
        </h2>
        {right}
      </header>
      <div className={clsx("min-h-0 flex-1 overflow-auto", bodyClassName)}>
        {children}
      </div>
    </section>
  );
}

const DOT_TONES = {
  running: "bg-running",
  ok: "bg-ok",
  warn: "bg-warn",
  fail: "bg-fail",
  idle: "bg-idle",
} as const;

export type Tone = keyof typeof DOT_TONES;

export function Dot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span
      className={clsx(
        "inline-block h-2 w-2 shrink-0 rounded-full",
        DOT_TONES[tone],
        pulse && "animate-pulse",
      )}
    />
  );
}

export function Chip({
  children,
  tone = "idle",
}: {
  children: React.ReactNode;
  tone?: Tone;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded border border-line px-1.5 py-0.5 text-[11px] text-muted">
      <Dot tone={tone} />
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  tone = "default",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  tone?: "default" | "danger";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        "rounded border px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        tone === "danger"
          ? "border-fail/40 text-fail hover:bg-fail/10"
          : "border-line text-fg hover:bg-raised",
      )}
    >
      {children}
    </button>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="px-3 py-6 text-center text-xs text-muted">{children}</p>
  );
}

/** `8412` → `8.4s`. Durations in the timeline are read at a glance, not measured. */
export function formatDuration(ms: number | undefined): string {
  if (ms === undefined) return "";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatBytes(bytes: number | undefined): string {
  if (bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTokens(count: number | undefined): string {
  if (count === undefined) return "";
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}
