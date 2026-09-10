"use client";

import clsx from "clsx";

/**
 * A native `<select>`, styled.
 *
 * Deliberately not a custom listbox. The options here are short, fixed vocabularies — a
 * collection name, a task kind, a sandbox profile — and the native control already has
 * keyboard navigation, type-ahead, mobile behaviour and a screen-reader contract that a
 * div-based replacement would spend a hundred lines approximating badly.
 */

export interface Option {
  value: string;
  label: string;
  disabled?: boolean;
}

export function Select({
  value,
  onChange,
  options,
  label,
  id,
  disabled,
  title,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  options: readonly Option[];
  label: string;
  id?: string;
  disabled?: boolean;
  title?: string;
  className?: string;
}) {
  return (
    <select
      id={id}
      aria-label={label}
      title={title}
      value={value}
      disabled={disabled}
      onChange={(cause) => onChange(cause.target.value)}
      className={clsx(
        "rounded border border-line bg-surface px-2 py-1 text-xs text-fg",
        "disabled:cursor-not-allowed disabled:opacity-40",
        className,
      )}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value} disabled={option.disabled}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
  overridden,
  error,
}: {
  label: string;
  hint?: React.ReactNode;
  htmlFor?: string;
  children: React.ReactNode;
  /**
   * §8.4: every non-default value MUST be visually marked as overridden. Silent
   * divergence from platform defaults is how a run's behaviour becomes unexplainable a
   * week later.
   */
  overridden?: boolean;
  error?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={htmlFor}
        className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-widest text-muted"
      >
        {label}
        {overridden && (
          <span className="rounded bg-warn/15 px-1 text-[9px] font-semibold tracking-normal text-warn">
            overridden
          </span>
        )}
      </label>
      {children}
      {error ? (
        <p className="text-[11px] text-fail">{error}</p>
      ) : (
        hint && <p className="text-[11px] text-idle">{hint}</p>
      )}
    </div>
  );
}

export function TextInput({
  value,
  onChange,
  id,
  placeholder,
  label,
  type = "text",
  disabled,
  className,
  ref,
  ...rest
}: {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  placeholder?: string;
  label?: string;
  type?: "text" | "number" | "search";
  disabled?: boolean;
  className?: string;
  /**
   * React 19 passes `ref` to function components as an ordinary prop, so no
   * `forwardRef` wrapper is needed. The console's `/` shortcut needs it to focus the
   * search box (§11).
   */
  ref?: React.Ref<HTMLInputElement>;
} & Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  "value" | "onChange" | "type" | "id" | "ref"
>) {
  return (
    <input
      {...rest}
      ref={ref}
      id={id}
      type={type}
      aria-label={label}
      value={value}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(cause) => onChange(cause.target.value)}
      className={clsx(
        "tnum w-full rounded border border-line bg-surface px-2 py-1 text-xs text-fg",
        "placeholder:text-idle disabled:cursor-not-allowed disabled:opacity-40",
        className,
      )}
    />
  );
}

export function TextArea({
  value,
  onChange,
  id,
  rows = 8,
  placeholder,
  label,
  disabled,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  rows?: number;
  placeholder?: string;
  label?: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <textarea
      id={id}
      rows={rows}
      aria-label={label}
      value={value}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(cause) => onChange(cause.target.value)}
      className={clsx(
        "w-full rounded border border-line bg-surface px-2 py-1.5 font-mono text-xs leading-5 text-fg",
        "placeholder:text-idle disabled:cursor-not-allowed disabled:opacity-40",
        className,
      )}
    />
  );
}

export function Checkbox({
  checked,
  onChange,
  id,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-start gap-2">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(cause) => onChange(cause.target.checked)}
        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-running disabled:cursor-not-allowed disabled:opacity-40"
      />
      <label htmlFor={id} className="min-w-0 flex-1 text-xs">
        <span className="font-mono text-fg">{label}</span>
        {hint && <span className="ml-2 text-[11px] text-muted">{hint}</span>}
      </label>
    </div>
  );
}
