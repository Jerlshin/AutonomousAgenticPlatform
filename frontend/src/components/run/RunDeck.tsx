"use client";

import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";
import { CAPABILITIES } from "@/lib/capabilities";
import type { RunEvent } from "@/lib/events.generated";
import { qk, TASKS_PREFIX } from "@/lib/queryKeys";
import type { PendingGate } from "@/lib/types";
import { useRunStream } from "@/hooks/useRunStream";
import { RunStoreProvider } from "@/stores/RunStoreProvider";
import { useUiStore } from "@/stores/uiStore";
import { Banner, Button } from "@/components/ui/primitives";
import { ArtifactDeck } from "./ArtifactDeck";
import { ConsolePane } from "./ConsolePane";
import { DeckFocusProvider, useDeckFocus } from "./DeckFocus";
import { GateConsole } from "./GateConsole";
import { GraphPane } from "./GraphPane";
import { RunHeader } from "./RunHeader";
import { TimelinePane } from "./TimelinePane";

/**
 * The live run control deck (§8.5) — the product.
 *
 * ```
 * ┌─────────────────────────────────────────────────────────────────────┐
 * │ HEADER                                                              │
 * ├──────────────────────────┬──────────────────────────────────────────┤
 * │ ① AGENT GRAPH            │ ② TIMELINE & CRITERIA                    │
 * ├──────────────────────────┴──────────────────────────────────────────┤
 * │ ③ CONSOLE                                                           │
 * ├─────────────────────────────────────────────────────────────────────┤
 * │ ④ ARTIFACTS                                                         │
 * └─────────────────────────────────────────────────────────────────────┘
 * ```
 *
 * `RunDeck` is the **only** component in the app that calls `useRunStream`, and it passes
 * **no stream data as props** — every pane subscribes to its own slice (§6.5). That is
 * what keeps a `token.delta` to one re-render.
 *
 * The grid geometry is load-bearing, not styling. `minmax(0, …)` on the two flexible rows
 * is required or a grid row refuses to shrink below its content height and the console
 * pushes the artifact bar off-screen; `h-[calc(100vh-3rem)]` with no page scroll is what
 * makes the panes scroll internally rather than the document.
 */
export function RunDeck({ runId }: { runId: string }) {
  const client = useQueryClient();
  const [dismissedGate, setDismissedGate] = useState<number | null>(null);
  const invalidateRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const collapsed = useUiStore((state) => state.collapsed);
  const toggleCollapsed = useUiStore((state) => state.toggleCollapsed);
  const topRowFraction = useUiStore((state) => state.topRowFraction);
  const setTopRowFraction = useUiStore((state) => state.setTopRowFraction);
  const graphWidthFraction = useUiStore((state) => state.graphWidthFraction);
  const setGraphWidthFraction = useUiStore((state) => state.setGraphWidthFraction);

  /**
   * The two systems meet here and nowhere else (§6.6).
   *
   * Debounced, and never during replay: a reconnect that replays three hundred events
   * would otherwise issue three hundred refetches of a run that has not changed since the
   * first one.
   */
  const onEvent = useCallback(
    (event: RunEvent) => {
      const invalidates =
        event.type === "run.completed" ||
        event.type === "run.failed" ||
        event.type === "run.cancelled" ||
        event.type === "interrupt.requested" ||
        (event.type === "artifact.created" && CAPABILITIES.runArtifacts);
      if (!invalidates) return;

      if (invalidateRef.current) clearTimeout(invalidateRef.current);
      invalidateRef.current = setTimeout(() => {
        invalidateRef.current = null;
        void client.invalidateQueries({ queryKey: qk.run(runId) });
        void client.invalidateQueries({ queryKey: qk.taskRuns(runId) });
        void client.invalidateQueries({ queryKey: TASKS_PREFIX });
      }, 250);
    },
    [client, runId],
  );

  const onGate = useCallback((gate: PendingGate) => {
    // A new gate un-dismisses: the operator dismissed the *previous* one to read the
    // console, not this one.
    setDismissedGate((seq) => (seq === gate.seq ? seq : null));
  }, []);

  const stream = useRunStream(runId, { onEvent, onGate });

  useEffect(
    () => () => {
      if (invalidateRef.current) clearTimeout(invalidateRef.current);
    },
    [],
  );

  return (
    <RunStoreProvider store={stream.store}>
      <DeckFocusProvider>
        <DeckBody
          stream={stream}
          collapsed={collapsed}
          toggleCollapsed={toggleCollapsed}
          topRowFraction={topRowFraction}
          setTopRowFraction={setTopRowFraction}
          graphWidthFraction={graphWidthFraction}
          setGraphWidthFraction={setGraphWidthFraction}
          dismissedGate={dismissedGate}
          onDismissGate={setDismissedGate}
        />
      </DeckFocusProvider>
    </RunStoreProvider>
  );
}

/**
 * The deck's body, inside both providers.
 *
 * Split from `RunDeck` because the gate console and the keyboard map need `useDeckFocus`,
 * and a hook cannot be called in the component that renders its own provider.
 */
function DeckBody({
  stream,
  collapsed,
  toggleCollapsed,
  topRowFraction,
  setTopRowFraction,
  graphWidthFraction,
  setGraphWidthFraction,
  dismissedGate,
  onDismissGate,
}: {
  stream: ReturnType<typeof useRunStream>;
  collapsed: ReturnType<typeof useUiStore.getState>["collapsed"];
  toggleCollapsed: (pane: "graph" | "timeline" | "console" | "artifacts") => void;
  topRowFraction: number;
  setTopRowFraction: (fraction: number) => void;
  graphWidthFraction: number;
  setGraphWidthFraction: (fraction: number) => void;
  dismissedGate: number | null;
  onDismissGate: (seq: number | null) => void;
}) {
  const gridRef = useRef<HTMLDivElement | null>(null);
  const topRowRef = useRef<HTMLDivElement | null>(null);
  const { gateRaises } = useDeckFocus();

  // The graph's `hitl_gate` marker asks for the dialog back. §8.6 requires a dismissed
  // gate to be reopenable, because dismissing it is how an operator goes to read the
  // console before deciding.
  useEffect(() => {
    if (gateRaises > 0) onDismissGate(null);
  }, [gateRaises, onDismissGate]);

  useDeckShortcuts(toggleCollapsed);

  const pendingGate = stream.pendingGate;
  const gateOpen = pendingGate !== null && dismissedGate !== pendingGate.seq;

  const topPercent = Math.round(topRowFraction * 100);
  const graphPercent = Math.round(graphWidthFraction * 100);

  return (
    <>
      {/* §8.5.1: the deck fills the viewport exactly and never scrolls the page. The
          header above it is `h-12`. Below `lg` the panes stack, each with a fixed height
          and its own scroll — a four-pane grid on a laptop half-screen is four unusable
          panes. */}
      <div
        ref={gridRef}
        className={clsx(
          "flex min-h-0 flex-1 flex-col overflow-auto",
          "lg:grid lg:h-[calc(100vh-3rem)] lg:overflow-hidden",
        )}
        style={{
          gridTemplateRows: `auto minmax(0, ${topPercent}fr) minmax(0, ${
            100 - topPercent
          }fr) auto`,
        }}
      >
        <RunHeader stream={stream} />

        {pendingGate && !gateOpen && (
          <Banner tone="warn" className="shrink-0">
            <span className="flex items-center gap-2">
              A gate is waiting on you:{" "}
              <code className="font-mono">{pendingGate.gate}</code>. Expiry is treated as
              rejection.
              <Button tone="primary" onClick={() => onDismissGate(null)}>
                Decide now
              </Button>
            </span>
          </Banner>
        )}

        <div
          ref={topRowRef}
          className={clsx(
            "flex min-h-0 shrink-0 flex-col border-b border-line",
            "h-[520px] lg:h-auto lg:flex-row",
          )}
        >
          <PaneShell
            collapsed={collapsed.graph}
            onToggle={() => toggleCollapsed("graph")}
            label="Agent graph"
            style={{ flexBasis: `${graphPercent}%` }}
            className="min-h-[220px] lg:min-h-0"
          >
            <GraphPane collapsed={collapsed.graph} />
          </PaneShell>

          <Splitter
            orientation="vertical"
            label="Resize the graph and timeline panes"
            onDrag={(fraction) => setGraphWidthFraction(fraction)}
            containerRef={topRowRef}
          />

          <PaneShell
            collapsed={collapsed.timeline}
            onToggle={() => toggleCollapsed("timeline")}
            label="Timeline"
            style={{ flexBasis: `${100 - graphPercent}%` }}
            className="min-h-[260px] lg:min-h-0"
          >
            <TimelinePane collapsed={collapsed.timeline} />
          </PaneShell>
        </div>

        <Splitter
          orientation="horizontal"
          label="Resize the upper panes and the console"
          onDrag={(fraction) => setTopRowFraction(fraction)}
          containerRef={gridRef}
        />

        <PaneShell
          collapsed={collapsed.console}
          onToggle={() => toggleCollapsed("console")}
          label="Console"
          className="min-h-[240px] lg:min-h-0"
        >
          <ConsolePane collapsed={collapsed.console} />
        </PaneShell>

        <PaneShell
          collapsed={collapsed.artifacts}
          onToggle={() => toggleCollapsed("artifacts")}
          label="Artifacts"
          className="shrink-0"
        >
          <ArtifactDeck collapsed={collapsed.artifacts} />
        </PaneShell>
      </div>

      <GateConsole
        gate={pendingGate}
        open={gateOpen}
        onDismiss={() => {
          if (pendingGate) onDismissGate(pendingGate.seq);
        }}
        onApprove={stream.approve}
      />
    </>
  );
}

/**
 * A collapsible pane wrapper.
 *
 * §8.5.1 requires each pane to be independently collapsible: an operator watching a long
 * `train` execution wants the console at full height, and the graph is not telling them
 * anything new for those twenty minutes.
 */
function PaneShell({
  collapsed,
  onToggle,
  label,
  children,
  className,
  style,
}: {
  collapsed: boolean;
  onToggle: () => void;
  label: string;
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  if (collapsed) {
    return (
      <button
        type="button"
        onClick={onToggle}
        title={`Expand the ${label.toLowerCase()} pane`}
        className="flex h-7 shrink-0 items-center gap-2 border-b border-line bg-surface px-3 text-[11px] uppercase tracking-widest text-muted transition-colors hover:text-fg"
      >
        <span aria-hidden>▸</span>
        {label}
        <span className="normal-case tracking-normal text-idle">collapsed</span>
      </button>
    );
  }

  return (
    <div className={clsx("relative flex min-h-0 min-w-0 flex-col", className)} style={style}>
      <button
        type="button"
        onClick={onToggle}
        aria-label={`Collapse the ${label.toLowerCase()} pane`}
        title={`Collapse the ${label.toLowerCase()} pane`}
        className="absolute right-1 top-0.5 z-20 rounded px-1 text-[11px] text-idle transition-colors hover:text-fg"
      >
        ▾
      </button>
      {children}
    </div>
  );
}

/**
 * A draggable split, persisted in `uiStore` (§8.5.1).
 *
 * Keyboard-operable as well as draggable, because §11 requires every control to be: a
 * split an operator cannot adjust without a mouse is a split they cannot adjust on a
 * laptop trackpad either.
 */
function Splitter({
  orientation,
  label,
  onDrag,
  containerRef,
}: {
  orientation: "horizontal" | "vertical";
  label: string;
  onDrag: (fraction: number) => void;
  containerRef: React.RefObject<HTMLDivElement | null>;
}) {
  const dragging = useRef(false);

  const fractionFrom = useCallback(
    (clientX: number, clientY: number) => {
      const box = containerRef.current?.getBoundingClientRect();
      if (!box) return null;
      return orientation === "vertical"
        ? (clientX - box.left) / box.width
        : (clientY - box.top) / box.height;
    },
    [containerRef, orientation],
  );

  useEffect(() => {
    const onMove = (cause: PointerEvent) => {
      if (!dragging.current) return;
      const fraction = fractionFrom(cause.clientX, cause.clientY);
      if (fraction !== null) onDrag(fraction);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [fractionFrom, onDrag]);

  return (
    <div
      role="separator"
      aria-orientation={orientation}
      aria-label={label}
      tabIndex={0}
      onPointerDown={(cause) => {
        dragging.current = true;
        // Without this, dragging a splitter selects every label it passes over.
        document.body.style.userSelect = "none";
        cause.preventDefault();
      }}
      onKeyDown={(cause) => {
        const box = containerRef.current?.getBoundingClientRect();
        if (!box) return;
        const size = orientation === "vertical" ? box.width : box.height;
        const forward = cause.key === "ArrowRight" || cause.key === "ArrowDown";
        const back = cause.key === "ArrowLeft" || cause.key === "ArrowUp";
        if (!forward && !back) return;
        cause.preventDefault();
        const current =
          orientation === "vertical"
            ? (cause.currentTarget.getBoundingClientRect().left - box.left) / box.width
            : (cause.currentTarget.getBoundingClientRect().top - box.top) / box.height;
        onDrag(current + ((forward ? 1 : -1) * 24) / size);
      }}
      className={clsx(
        "group hidden shrink-0 bg-line/40 transition-colors hover:bg-running/40 lg:block",
        orientation === "vertical" ? "w-1 cursor-col-resize" : "h-1 cursor-row-resize",
      )}
    />
  );
}

/**
 * The deck's keyboard map (§11).
 *
 * `g` graph · `t` timeline · `c` console · `a` artifacts toggle their panes; `/` and `f`
 * belong to the console and are handled there. Nothing fires while a text input has
 * focus, or typing "console" into the search box would collapse three panes.
 */
function useDeckShortcuts(
  toggleCollapsed: (pane: "graph" | "timeline" | "console" | "artifacts") => void,
) {
  useEffect(() => {
    const onKeyDown = (cause: KeyboardEvent) => {
      const target = cause.target as HTMLElement | null;
      if (
        cause.metaKey ||
        cause.ctrlKey ||
        cause.altKey ||
        (target &&
          (target.tagName === "INPUT" ||
            target.tagName === "TEXTAREA" ||
            target.isContentEditable))
      ) {
        return;
      }
      const pane = (
        { g: "graph", t: "timeline", c: "console", a: "artifacts" } as const
      )[cause.key];
      if (!pane) return;
      cause.preventDefault();
      toggleCollapsed(pane);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggleCollapsed]);
}
