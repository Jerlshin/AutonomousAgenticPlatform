"use client";

import clsx from "clsx";
import { Segmented } from "@/components/ui/tabs";
import { Button } from "@/components/ui/primitives";
import { TextInput } from "@/components/ui/select";
import type { ConsoleFilter } from "@/stores/uiStore";

/**
 * The console's controls (§8.5.5).
 *
 * Everything here is *view* state and lives in the pane, never in the run store: §6.5 is
 * explicit that console filtering and search happen in the pane, because storing the
 * filter would re-fold the whole buffer on every keystroke.
 */

export interface ConsoleToolbarProps {
  filter: ConsoleFilter;
  onFilter: (filter: ConsoleFilter) => void;
  counts: Record<ConsoleFilter, number>;

  search: string;
  onSearch: (term: string) => void;
  useRegex: boolean;
  onUseRegex: (on: boolean) => void;
  regexInvalid: boolean;
  matchCount: number;
  matchIndex: number;
  onPrevMatch: () => void;
  onNextMatch: () => void;
  searchRef: React.Ref<HTMLInputElement>;

  showTimestamps: boolean;
  onToggleTimestamps: () => void;
  renderAnsi: boolean;
  onToggleAnsi: () => void;

  following: boolean;
  onToggleFollow: () => void;

  nodeFilter: string | null;
  executionFilter: string | null;
  onClearScope: () => void;

  onCopyVisible: () => void;
  onDownload: () => void;
}

export function ConsoleToolbar(props: ConsoleToolbarProps) {
  const scoped = props.nodeFilter ?? props.executionFilter;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {scoped && (
        <button
          type="button"
          onClick={props.onClearScope}
          title="Clear the scope set by the graph or timeline pane"
          className="inline-flex items-center gap-1 rounded border border-running/40 bg-running/10 px-1.5 py-0.5 text-[11px] text-running"
        >
          {props.nodeFilter ? `node ${scoped}` : `exec ${scoped.slice(0, 8)}`}
          <span aria-hidden>×</span>
        </button>
      )}

      <Segmented
        label="Console source"
        value={props.filter}
        onChange={props.onFilter}
        options={[
          { value: "all", label: "all", count: props.counts.all },
          { value: "stdout", label: "stdout", count: props.counts.stdout },
          { value: "stderr", label: "stderr", count: props.counts.stderr },
          { value: "tokens", label: "tokens", count: props.counts.tokens },
        ]}
      />

      <span className="flex items-center gap-1">
        <TextInput
          ref={props.searchRef}
          value={props.search}
          onChange={props.onSearch}
          type="search"
          label="Search the console"
          placeholder="/ to search…"
          className={clsx("w-40", props.regexInvalid && "border-warn")}
        />
        <button
          type="button"
          aria-pressed={props.useRegex}
          onClick={() => props.onUseRegex(!props.useRegex)}
          title={
            props.regexInvalid
              ? "This regular expression is not valid yet."
              : "Treat the search term as a regular expression"
          }
          className={clsx(
            "rounded border px-1 py-0.5 font-mono text-[11px] transition-colors",
            props.useRegex
              ? "border-running/50 bg-running/15 text-running"
              : "border-line text-muted hover:text-fg",
            props.regexInvalid && props.useRegex && "border-warn text-warn",
          )}
        >
          .*
        </button>
        {props.search && (
          <span className="tnum flex items-center gap-0.5 text-[11px] text-muted">
            <span title="Matches in the lines currently held">
              {props.matchCount === 0
                ? "0"
                : `${props.matchIndex + 1}/${props.matchCount}`}
            </span>
            <button
              type="button"
              onClick={props.onPrevMatch}
              disabled={props.matchCount === 0}
              aria-label="Previous match"
              className="px-0.5 disabled:opacity-30"
            >
              ↑
            </button>
            <button
              type="button"
              onClick={props.onNextMatch}
              disabled={props.matchCount === 0}
              aria-label="Next match"
              className="px-0.5 disabled:opacity-30"
            >
              ↓
            </button>
          </span>
        )}
      </span>

      <Toggle
        on={props.showTimestamps}
        onClick={props.onToggleTimestamps}
        title="Show each line's wall-clock timestamp"
      >
        ts
      </Toggle>
      <Toggle
        on={props.renderAnsi}
        onClick={props.onToggleAnsi}
        title="Render ANSI colours instead of stripping them"
      >
        ansi
      </Toggle>
      <Toggle
        on={props.following}
        onClick={props.onToggleFollow}
        title="Follow the tail (f). Turns itself off when you scroll up."
      >
        ⇩ tail
      </Toggle>

      <Button onClick={props.onCopyVisible} title="Copy the lines currently shown">
        Copy
      </Button>
      <Button
        onClick={props.onDownload}
        title="Download every line still held as a .log file"
      >
        ⇩ .log
      </Button>
    </div>
  );
}

function Toggle({
  on,
  onClick,
  title,
  children,
}: {
  on: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      title={title}
      className={clsx(
        "rounded border px-1.5 py-0.5 text-[11px] transition-colors duration-150",
        on
          ? "border-running/50 bg-running/15 text-running"
          : "border-line text-muted hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}
