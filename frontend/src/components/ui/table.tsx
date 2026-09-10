import clsx from "clsx";

/**
 * A table that is actually a `<table>` (§11: "Tables are `<table>`").
 *
 * A grid of divs looks identical and is opaque to a screen reader, loses column
 * association on every row, and cannot be copied into a spreadsheet. The styling here is
 * the §10 density — 12–13 px, `leading-5` rows, tabular figures — applied once rather
 * than re-typed per table.
 */

export function Table({
  children,
  className,
  caption,
}: {
  children: React.ReactNode;
  className?: string;
  caption?: string;
}) {
  return (
    <table className={clsx("w-full border-collapse text-xs", className)}>
      {caption && <caption className="sr-only">{caption}</caption>}
      {children}
    </table>
  );
}

export function THead({ children }: { children: React.ReactNode }) {
  return (
    <thead className="sticky top-0 z-10 bg-surface">
      <tr className="border-b border-line text-left">{children}</tr>
    </thead>
  );
}

export function TH({
  children,
  className,
  numeric,
  scope = "col",
}: {
  children?: React.ReactNode;
  className?: string;
  numeric?: boolean;
  scope?: "col" | "row";
}) {
  return (
    <th
      scope={scope}
      className={clsx(
        "px-3 py-1.5 text-[10px] font-semibold uppercase tracking-widest text-muted",
        numeric && "text-right",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function TBody({ children }: { children: React.ReactNode }) {
  return <tbody>{children}</tbody>;
}

export function TR({
  children,
  onClick,
  className,
  selected,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  className?: string;
  selected?: boolean;
}) {
  return (
    <tr
      onClick={onClick}
      // A clickable row is still reachable by keyboard: the cell that carries the row's
      // meaning holds a real link, and this handler is the convenience on top of it.
      className={clsx(
        "border-b border-line/60 transition-colors duration-150",
        onClick && "cursor-pointer hover:bg-raised",
        selected && "bg-raised",
        className,
      )}
    >
      {children}
    </tr>
  );
}

export function TD({
  children,
  className,
  numeric,
  mono,
  title,
  colSpan,
}: {
  children?: React.ReactNode;
  className?: string;
  numeric?: boolean;
  mono?: boolean;
  title?: string;
  colSpan?: number;
}) {
  return (
    <td
      title={title}
      colSpan={colSpan}
      className={clsx(
        "px-3 py-1.5 align-middle leading-5",
        numeric && "tnum text-right",
        mono && "font-mono text-[11px]",
        className,
      )}
    >
      {children}
    </td>
  );
}
