import type { Metadata } from "next";
import Link from "next/link";
import { Providers } from "@/components/providers";
import { ConnectionBadge } from "@/components/shell/ConnectionBadge";
import { NavBar } from "@/components/shell/NavBar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pluton R&D Engine",
  description: "Autonomous multi-agent R&D platform — the live run control deck",
};

/**
 * The app shell (§8.1). A Server Component: it holds no state and touches no socket.
 *
 * `min-h-0` on `main` is not decoration. Without it a flex child with an internal scroll
 * area grows to its content height, and the four-pane run deck scrolls the *page* instead
 * of the pane — which is the one thing §8.5.1 forbids outright.
 */
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-ink text-fg antialiased">
        <Providers>
          <div className="flex h-screen flex-col">
            <header className="flex h-12 shrink-0 items-center gap-6 border-b border-line px-4">
              <Link
                href="/"
                className="whitespace-nowrap text-sm font-semibold tracking-tight"
              >
                Pluton <span className="text-muted">R&amp;D Engine</span>
              </Link>
              <NavBar />
              <div className="ml-auto flex items-center gap-2">
                <ConnectionBadge />
              </div>
            </header>
            <main className="flex min-h-0 flex-1 flex-col">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
