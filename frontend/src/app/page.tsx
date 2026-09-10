import { DashboardClient } from "@/components/dashboard/DashboardClient";

/**
 * The global dashboard route (§8.2).
 *
 * A Server Component that renders the page frame and mounts one client subtree. The
 * boundary is here rather than at the layout because everything above it — the shell,
 * the nav — only renders props, and pushing `"use client"` up would ship the whole frame
 * to the browser for no interactivity gained (§2.2).
 */
export default function DashboardPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <DashboardClient />
    </div>
  );
}
