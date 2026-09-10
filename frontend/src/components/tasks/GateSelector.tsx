"use client";

import { GATES, GATE_TIMEOUT_NOTE } from "@/lib/gates";
import type { GateName } from "@/lib/types";
import { Banner } from "@/components/ui/primitives";
import { Checkbox } from "@/components/ui/select";

/**
 * The SUPERVISION section (§8.4).
 *
 * Every checkbox states its consequence, and the timeout warning is a banner rather than
 * fine print: an operator who ticks a gate and walks away has cancelled their own run,
 * and thirty minutes later there is nothing to see but a `CANCELLED` outcome with a
 * report explaining which gate expired.
 */
export function GateSelector({
  selected,
  onChange,
  disabled,
}: {
  selected: readonly GateName[];
  onChange: (gates: GateName[]) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-col gap-2">
      {GATES.map((meta) => (
        <Checkbox
          key={meta.gate}
          id={`gate-${meta.gate}`}
          disabled={disabled}
          checked={selected.includes(meta.gate)}
          onChange={(checked) =>
            onChange(
              checked
                ? [...selected, meta.gate]
                : selected.filter((gate) => gate !== meta.gate),
            )
          }
          label={meta.label}
          hint={meta.consequence}
        />
      ))}
      <Banner tone="warn" className="mt-1 rounded border">
        {GATE_TIMEOUT_NOTE}
      </Banner>
    </div>
  );
}
