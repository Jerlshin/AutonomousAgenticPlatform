"use client";

import { Field, TextInput } from "@/components/ui/select";

/**
 * The LIMITS section of the submission form (§8.4).
 *
 * Two rules, both about honesty of intent:
 *
 * * **the default is placeholder text, never a pre-filled value.** A pre-filled field
 *   submits a "custom" budget identical to the default and obscures whether anybody
 *   chose it.
 * * **a value above 2× the default warns, and above 4× is refused.** The budgets are the
 *   only thing standing between a bad plan and thirty minutes of GPU time, and a typo in
 *   `wallclock_seconds` is very easy to make.
 */

/** `Budgets` in `engine/state.py`. Transcribed under §4.5's static-domain-table exemption. */
export const BUDGET_DEFAULTS = {
  max_debug_iterations: 4,
  max_replans: 2,
  max_node_visits: 60,
  max_sandbox_executions: 12,
  wallclock_seconds: 1800,
  max_tokens: 250_000,
} as const;

export type BudgetKey = keyof typeof BUDGET_DEFAULTS;

export type Budgets = Partial<Record<BudgetKey, number>>;

const LABELS: Record<BudgetKey, string> = {
  max_debug_iterations: "Debug iterations",
  max_replans: "Replans",
  max_node_visits: "Node visits",
  max_sandbox_executions: "Sandbox executions",
  wallclock_seconds: "Wallclock (s)",
  max_tokens: "Max tokens",
};

const HINTS: Record<BudgetKey, string> = {
  max_debug_iterations: "How many times the Debugger may rewrite failing code.",
  max_replans: "How many times the approach itself may change.",
  max_node_visits: "The graph's hard stop against a routing loop.",
  max_sandbox_executions: "How many containers this run may spend.",
  wallclock_seconds: "The run is cancelled at this age, mid-node if necessary.",
  max_tokens: "Total tokens across every agent.",
};

export function budgetProblem(key: BudgetKey, value: number): string | undefined {
  const base = BUDGET_DEFAULTS[key];
  if (!Number.isFinite(value) || value <= 0) return "Must be a positive number.";
  if (value > base * 4) return `Above the ceiling of ${base * 4} (4× the default).`;
  return undefined;
}

export function budgetWarning(key: BudgetKey, value: number): string | undefined {
  const base = BUDGET_DEFAULTS[key];
  return value > base * 2 ? `More than double the default of ${base}.` : undefined;
}

export function BudgetFields({
  values,
  onChange,
  disabled,
}: {
  values: Budgets;
  onChange: (values: Budgets) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
      {(Object.keys(BUDGET_DEFAULTS) as BudgetKey[]).map((key) => {
        const raw = values[key];
        const overridden = raw !== undefined && raw !== BUDGET_DEFAULTS[key];
        const problem = raw === undefined ? undefined : budgetProblem(key, raw);
        const warning = raw === undefined ? undefined : budgetWarning(key, raw);
        return (
          <Field
            key={key}
            label={LABELS[key]}
            htmlFor={`budget-${key}`}
            overridden={overridden}
            error={problem}
            hint={warning ? <span className="text-warn">⚠ {warning}</span> : HINTS[key]}
          >
            <TextInput
              id={`budget-${key}`}
              type="number"
              disabled={disabled}
              // Empty means "the platform default", and the placeholder says what that is.
              value={raw === undefined ? "" : String(raw)}
              placeholder={`default ${BUDGET_DEFAULTS[key]}`}
              onChange={(next) => {
                const trimmed = next.trim();
                if (trimmed === "") {
                  const { [key]: _cleared, ...rest } = values;
                  onChange(rest);
                  return;
                }
                onChange({ ...values, [key]: Number(trimmed) });
              }}
            />
          </Field>
        );
      })}
    </div>
  );
}
