import type { NextConfig } from "next";

/**
 * The dashboard talks to the API over an absolute URL from the browser
 * (`NEXT_PUBLIC_API_BASE`), not through a Next.js rewrite.
 *
 * A rewrite would proxy REST cleanly and then quietly fail on the thing this app is
 * actually for: the WebSocket in §9 has to reach `/api/v1/ws/runs/{id}` directly, and a
 * dev-server rewrite does not carry an upgrade. Keeping both on the same absolute origin
 * means REST and the socket cannot disagree about where the backend is.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  typescript: { ignoreBuildErrors: false },
  eslint: { ignoreDuringBuilds: false },
};

export default nextConfig;
