"use client";

import clsx from "clsx";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { compileSearch, parseAnsi, stripAnsi } from "@/lib/ansi";
import { formatWallClock } from "@/lib/format";
import type { ConsoleLine } from "@/lib/types";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useFollowTail } from "@/hooks/useFollowTail";
import { useRunShallow } from "@/hooks/useRunSlice";
import { selectConsole } from "@/hooks/useRunSlice";
import { useUiStore } from "@/stores/uiStore";
import { Banner, Button, Empty, Panel } from "@/components/ui/primitives";
import { VirtualList, type VirtualListHandle } from "@/components/ui/virtual-list";
import { ConsoleToolbar } from "./ConsoleToolbar";
import { useDeckFocus } from "./DeckFocus";

/**
 * Pane 3 — the live terminal (§8.5.5).
 *
 * The high-throughput pane, and the one that decides whether the tab survives a long run.
 * Four things keep it alive:
 *
 * * **virtualized always**, not "above N lines", with a fixed 20 px row so measurement
 *   never runs — measurement is the expensive part of a virtualizer;
 * * **filtering and search are memoized on their inputs**, because filtering 5 000 lines
 *   on every keystroke without memoization is a visible freeze (§9.3);
 * * **rows are `memo`'d with a stable key**, or an unmemoized row re-renders the whole
 *   visible window on every commit;
 * * **the pane subscribes to one slice**, so a `token.delta` re-renders this and nothing
 *   else (§9.1's budget of one component per delta).
 *
 * Both kinds of loss are reported rather than hidden: the server's `sandbox.truncated`
 * arrives as an inline divider where it happened, and the client's own ring eviction
 * raises a sticky banner. A log viewer that quietly loses lines is worse than one that
 * says it did.
 */

/** `leading-5`. Fixed, so the virtualizer never measures a row (§9.2). */
const ROW_HEIGHT = 20;
const OVERSCAN = 20;

export function ConsolePane({ collapsed }: { collapsed?: boolean }) {
  const { consoleLines, droppedConsoleLines, terminal } = useRunShallow(selectConsole);
  const focus = useDeckFocus();

  const filter = useUiStore((state) => state.consoleFilter);
  const setFilter = useUiStore((state) => state.setConsoleFilter);
  const showTimestamps = useUiStore((state) => state.showTimestamps);
  const toggleTimestamps = useUiStore((state) => state.toggleTimestamps);
  const renderAnsi = useUiStore((state) => state.renderAnsi);
  const toggleAnsi = useUiStore((state) => state.toggleAnsi);

  const [rawSearch, setRawSearch] = useState("");
  const [useRegex, setUseRegex] = useState(false);
  const [matchIndex, setMatchIndex] = useState(0);
  const search = useDebouncedValue(rawSearch, 150);

  const listRef = useRef<VirtualListHandle | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  const tail = useFollowTail(consoleLines.length);

  // ---- derivation, memoized on its inputs -------------------------------

  const counts = useMemo(() => {
    let stdout = 0;
    let stderr = 0;
    let tokens = 0;
    for (const line of consoleLines) {
      if (line.stream === "stdout") stdout += 1;
      else if (line.stream === "stderr") stderr += 1;
      else if (line.stream === "token") tokens += 1;
    }
    return { all: consoleLines.length, stdout, stderr, tokens };
  }, [consoleLines]);

  const scoped = useMemo(() => {
    if (!focus.node && !focus.executionId) return consoleLines;
    return consoleLines.filter(
      (line) =>
        (!focus.executionId || line.executionId === focus.executionId) &&
        (!focus.node || line.node === focus.node || line.executionId != null),
    );
  }, [consoleLines, focus.node, focus.executionId]);

  const filtered = useMemo(() => {
    if (filter === "all") return scoped;
    const want =
      filter === "tokens" ? "token" : filter === "stderr" ? "stderr" : "stdout";
    // Markers are the client's own notices — a sandbox launch, a truncation. They stay
    // visible under every source filter, because hiding "the container exited 1" while
    // showing its stderr would remove the line that explains the rest.
    return scoped.filter((line) => line.stream === want || line.stream === "marker");
  }, [scoped, filter]);

  const { test, invalid } = useMemo(
    () => compileSearch(search, useRegex),
    [search, useRegex],
  );

  const matches = useMemo(() => {
    if (!search) return [] as number[];
    const found: number[] = [];
    for (let index = 0; index < filtered.length; index++) {
      if (test(filtered[index]!.text)) found.push(index);
    }
    return found;
  }, [filtered, search, test]);

  // Clamp rather than reset: a match list that shrinks while the viewer is on match 40
  // should land them on the last one, not throw them back to the top.
  const boundedIndex = matches.length === 0 ? 0 : Math.min(matchIndex, matches.length - 1);

  const step = useCallback(
    (delta: number) => {
      if (matches.length === 0) return;
      const next = (boundedIndex + delta + matches.length) % matches.length;
      setMatchIndex(next);
      const target = matches[next];
      if (target !== undefined) listRef.current?.scrollToIndex(target, "center");
    },
    [matches, boundedIndex],
  );

  // ---- keyboard (§11) ---------------------------------------------------

  useEffect(() => {
    const onKeyDown = (cause: KeyboardEvent) => {
      // Shortcuts MUST NOT fire while a text input has focus (§11), or typing "f" into
      // the search box toggles follow-tail.
      const target = cause.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (cause.key === "/") {
        cause.preventDefault();
        searchRef.current?.focus();
      } else if (cause.key === "f") {
        cause.preventDefault();
        tail.setFollowTail(!tail.followTail);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [tail]);

  // ---- actions ----------------------------------------------------------

  const copyVisible = useCallback(() => {
    void navigator.clipboard?.writeText(
      filtered.map((line) => stripAnsi(line.text)).join("\n"),
    );
  }, [filtered]);

  const download = useCallback(() => {
    // A client-side Blob, so this needs no backend endpoint (§8.5.5).
    const body = consoleLines
      .map((line) => `${line.ts} [${line.stream}] ${stripAnsi(line.text)}`)
      .join("\n");
    const url = URL.createObjectURL(new Blob([body], { type: "text/plain" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "run-console.log";
    anchor.click();
    URL.revokeObjectURL(url);
  }, [consoleLines]);

  if (collapsed) return null;

  const activeMatch = matches[boundedIndex];

  return (
    <Panel
      title="Console"
      count={
        droppedConsoleLines > 0
          ? `${filtered.length.toLocaleString()} of ${consoleLines.length.toLocaleString()} · ${droppedConsoleLines.toLocaleString()} dropped`
          : `${filtered.length.toLocaleString()} lines`
      }
      className="min-h-0 border-t border-line"
      bodyClassName="min-h-0 flex flex-col relative"
      right={
        <ConsoleToolbar
          filter={filter}
          onFilter={setFilter}
          counts={counts}
          search={rawSearch}
          onSearch={(term) => {
            setRawSearch(term);
            setMatchIndex(0);
          }}
          useRegex={useRegex}
          onUseRegex={setUseRegex}
          regexInvalid={invalid}
          matchCount={matches.length}
          matchIndex={boundedIndex}
          onPrevMatch={() => step(-1)}
          onNextMatch={() => step(1)}
          searchRef={searchRef}
          showTimestamps={showTimestamps}
          onToggleTimestamps={toggleTimestamps}
          renderAnsi={renderAnsi}
          onToggleAnsi={toggleAnsi}
          following={tail.following}
          onToggleFollow={() => tail.setFollowTail(!tail.followTail)}
          nodeFilter={focus.node}
          executionFilter={focus.executionId}
          onClearScope={focus.clear}
          onCopyVisible={copyVisible}
          onDownload={download}
        />
      }
    >
      {droppedConsoleLines > 0 && (
        <Banner tone="warn">
          {droppedConsoleLines.toLocaleString()} earlier lines are no longer held — the
          client-side buffer evicted them.
        </Banner>
      )}

      {filtered.length === 0 ? (
        <Empty>
          {consoleLines.length === 0
            ? terminal
              ? "This run produced no console output."
              : "No output yet — the sandbox has not started."
            : "No line matches the current filter."}
        </Empty>
      ) : (
        <div className="min-h-0 flex-1">
          <VirtualList
            items={filtered}
            rowHeight={ROW_HEIGHT}
            overscan={OVERSCAN}
            followTail={tail.following}
            onAtBottomChange={tail.onAtBottomChange}
            handleRef={listRef}
            // Deliberately not `role="log"`: §11 forbids making the console a live
            // region, because announcing thousands of lines is hostile.
            className="font-mono text-[12px]"
            renderRow={(line, index) => (
              <ConsoleRow
                line={line}
                renderAnsi={renderAnsi}
                showTimestamp={showTimestamps}
                highlight={search}
                isMatch={activeMatch === index}
              />
            )}
          />
        </div>
      )}

      {tail.detached && tail.pending > 0 && (
        <div className="pointer-events-none absolute inset-x-0 bottom-2 flex justify-center">
          <span className="pointer-events-auto">
            <Button
              tone="primary"
              onClick={() => {
                tail.reattach();
                listRef.current?.scrollToBottom();
              }}
            >
              Jump to latest ({tail.pending.toLocaleString()} new)
            </Button>
          </span>
        </div>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------------------------

const STREAM_CLASS: Record<ConsoleLine["stream"], string> = {
  stdout: "text-fg",
  stderr: "text-fail",
  token: "italic text-muted",
  marker: "text-warn",
};

const MARKER_TONE: Record<string, string> = {
  info: "text-running",
  warn: "text-warn",
  fail: "text-fail",
};

/**
 * One line.
 *
 * `memo` is load-bearing (§9.2): without it every commit re-renders the whole visible
 * window, which at 200 events/s is 40 rows × 60 frames of wasted reconciliation.
 * `data-seq` is on the row so the debug drawer and a bug report can point at a frame.
 */
const ConsoleRow = memo(function ConsoleRow({
  line,
  renderAnsi,
  showTimestamp,
  highlight,
  isMatch,
}: {
  line: ConsoleLine;
  renderAnsi: boolean;
  showTimestamp: boolean;
  highlight: string;
  isMatch: boolean;
}) {
  const tone =
    line.stream === "marker" && line.tone
      ? (MARKER_TONE[line.tone] ?? STREAM_CLASS.marker)
      : STREAM_CLASS[line.stream];

  return (
    <div
      data-seq={line.seq}
      className={clsx(
        "flex h-5 items-center gap-2 whitespace-pre px-3 leading-5",
        tone,
        isMatch && "bg-running/15",
      )}
    >
      {showTimestamp && (
        <span className="tnum shrink-0 text-[11px] text-idle">
          {formatWallClock(line.ts)}
        </span>
      )}
      <span className="min-w-0 flex-1 truncate">
        {renderAnsi ? (
          parseAnsi(line.text).map((span, index) => (
            <span
              key={index}
              className={clsx(span.className, span.bold && "font-bold", span.dim && "opacity-60")}
            >
              {span.text}
            </span>
          ))
        ) : (
          <Highlighted text={stripAnsi(line.text)} term={highlight} />
        )}
      </span>
    </div>
  );
});

/** Matches highlighted in place (§8.5.5), case-insensitively, without a regex per row. */
function Highlighted({ text, term }: { text: string; term: string }) {
  if (!term) return <>{text}</>;
  const haystack = text.toLowerCase();
  const needle = term.toLowerCase();
  const at = haystack.indexOf(needle);
  if (at === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark className="bg-warn/30 text-fg">{text.slice(at, at + term.length)}</mark>
      {text.slice(at + term.length)}
    </>
  );
}
