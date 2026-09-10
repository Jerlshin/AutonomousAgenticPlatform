"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/primitives";
import { log } from "@/lib/log";

/**
 * The route error boundary (§8.1).
 *
 * It reports the failure and offers a retry. What it does not do is render a stack
 * trace — an operator cannot act on one, and the digest below is what actually correlates
 * a report with a server log line.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    log.warn("route error", error);
  }, [error]);

  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="flex max-w-lg flex-col items-center gap-3 text-center">
        <p className="text-sm font-semibold text-fail">✗ This view failed to render.</p>
        <p className="text-xs text-muted">{error.message}</p>
        {error.digest && (
          <p className="font-mono text-[11px] text-idle">digest {error.digest}</p>
        )}
        <Button onClick={reset}>Try again</Button>
      </div>
    </div>
  );
}
