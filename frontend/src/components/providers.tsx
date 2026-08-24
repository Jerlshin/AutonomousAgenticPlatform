"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * TanStack Query for server state (§18.1).
 *
 * `refetchOnWindowFocus` is off and `staleTime` is generous on purpose: the live run view
 * is fed by the WebSocket, and a refetch storm every time the user alt-tabs back would
 * re-request bodies the socket has already superseded. REST is for the first paint and
 * for the pages that have no stream.
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
            staleTime: 15_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
