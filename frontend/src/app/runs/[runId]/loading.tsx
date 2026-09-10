/**
 * The deck's skeleton (§8.1): the four-pane grid with empty panes, never a spinner.
 *
 * It matches `RunDeck`'s geometry exactly — `auto minmax(0,46fr) minmax(0,54fr) auto`,
 * `h-[calc(100vh-3rem)]` — so nothing shifts when the real deck replaces it.
 */
export default function RunLoading() {
  return (
    <div
      className="grid h-[calc(100vh-3rem)] overflow-hidden"
      style={{ gridTemplateRows: "auto minmax(0, 46fr) minmax(0, 54fr) auto" }}
      aria-busy="true"
    >
      <div className="h-14 shrink-0 border-b border-line bg-surface" />
      <div className="flex min-h-0 border-b border-line">
        <PaneFrame title="Agent graph" className="basis-[38%] border-r border-line" />
        <PaneFrame title="Timeline" className="basis-[62%]" />
      </div>
      <PaneFrame title="Console" className="min-h-0 border-b border-line" />
      <div className="h-24 shrink-0 bg-surface" />
    </div>
  );
}

function PaneFrame({ title, className }: { title: string; className?: string }) {
  return (
    <section className={`flex min-h-0 flex-col bg-surface ${className ?? ""}`}>
      <header className="flex h-8 shrink-0 items-center border-b border-line px-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-widest text-muted">
          {title}
        </h2>
      </header>
      <div className="min-h-0 flex-1" />
    </section>
  );
}
