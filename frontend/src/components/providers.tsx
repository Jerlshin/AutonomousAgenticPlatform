"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * TanStack Query for server state (§6.2).
 *
 * Three defaults, each of which is a decision rather than a preference:
 *
 * * **`refetchOnWindowFocus: false`.** A focus refetch mid-stream produces a visible
 *   flicker as REST state briefly overwrites fresher stream state. REST is the resting
 *   state of a run; the socket is its motion.
 * * **`retry: 1` for reads.** One retry absorbs a dropped connection; more of them turn a
 *   backend that is down into a client that looks hung.
 * * **`retry: 0` for mutations.** A retried `POST /tasks/{id}/runs` that the
 *   `Idempotency-Key` does not cover is a second run, and a second run is a second
 *   container and a second bill.
 *
 * The client is created in state rather than at module scope so a Fast Refresh in
 * development does not hand two component trees the same cache.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
          mutations: {
            retry: 0,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
