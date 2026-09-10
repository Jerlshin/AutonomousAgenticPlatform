"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, type Tone } from "@/components/ui/primitives";
import { Tooltip } from "@/components/ui/tooltip";

/**
 * The global API health indicator in the header.
 *
 * Distinct from the run view's own connection badge, which reports the *socket*. This one
 * answers "is the platform reachable at all", which is the first question when a pane
 * stops updating and the reason it lives in the shell rather than in a view.
 *
 * `/health/deep` answers `503` with a body when a hard dependency is down; the client
 * reads that body rather than treating a non-200 as no data, so the tooltip can name
 * which service failed instead of shrugging.
 */
export function ConnectionBadge() {
  const { data, isError } = useQuery({
    queryKey: qk.healthDeep,
    queryFn: api.healthDeep,
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 0,
  });

  if (isError) {
    return (
      <Chip tone="fail" title="The API did not answer /health/deep.">
        API unreachable
      </Chip>
    );
  }
  if (!data) return <Chip tone="idle">API …</Chip>;

  const failing = Object.entries(data.services ?? {}).filter(
    ([, service]) => service.status !== "healthy",
  );
  const tone: Tone =
    data.status === "healthy" ? "ok" : data.status === "degraded" ? "warn" : "fail";

  return (
    <Tooltip
      side="bottom"
      content={
        <span className="flex flex-col gap-0.5">
          {Object.entries(data.services ?? {}).map(([name, service]) => (
            <span key={name} className="flex items-baseline gap-2">
              <span
                className={
                  service.status === "healthy"
                    ? "text-ok"
                    : service.required
                      ? "text-fail"
                      : "text-warn"
                }
              >
                {service.status === "healthy" ? "✓" : "✗"}
              </span>
              <span className="font-mono">{name}</span>
              {service.message && <span className="text-muted">{service.message}</span>}
            </span>
          ))}
        </span>
      }
    >
      <Chip tone={tone}>
        API {data.status}
        {failing.length > 0 && ` · ${failing.length} down`}
      </Chip>
    </Tooltip>
  );
}
