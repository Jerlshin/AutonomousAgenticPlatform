"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Dot, Panel, Skeleton } from "@/components/ui/primitives";

/**
 * `GET /health/deep` every 30 s (§8.2).
 *
 * A `503` renders each failing service in `fail` tone with its message rather than
 * collapsing to "unhealthy": when a run stalls, *which* dependency is down is the whole
 * question, and an aggregate answer sends someone to read container logs to find out
 * something the API already said.
 */
export function DependencyStrip() {
  const { data, isPending, isError } = useQuery({
    queryKey: qk.healthDeep,
    queryFn: api.healthDeep,
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 0,
  });

  return (
    <Panel title="Dependencies" className="border border-line">
      {isPending ? (
        <Skeleton rows={3} />
      ) : isError || !data ? (
        <p className="px-3 py-4 text-xs text-fail">
          ✗ The API did not answer <code className="font-mono">/health/deep</code>.
        </p>
      ) : (
        <ul className="grid grid-cols-2 gap-x-3 p-2">
          {Object.entries(data.services).map(([name, service]) => {
            const healthy = service.status === "healthy";
            return (
              <li
                key={name}
                className="flex items-center gap-2 py-0.5 text-xs"
                title={service.message ?? undefined}
              >
                <Dot
                  tone={healthy ? "ok" : service.required ? "fail" : "warn"}
                  label={`${name} ${service.status}`}
                />
                <span className="truncate font-mono text-[11px]">{name}</span>
                {!healthy && (
                  <span className="ml-auto truncate text-[10px] text-muted">
                    {service.required ? "required" : "optional"}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
