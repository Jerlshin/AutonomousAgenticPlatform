import type { Metadata } from "next";
import Link from "next/link";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pluton R&D Engine",
  description: "Autonomous multi-agent R&D platform — live run view",
};

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/tasks", label: "Tasks" },
  { href: "/corpus", label: "Corpus" },
  { href: "/benchmarks", label: "Benchmarks" },
];

/**
 * The app shell. A Server Component: it holds no state and touches no socket, which is
 * the split §18.1 asks for — Server Components for static shells, Client Components for
 * anything touching the WebSocket.
 */
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-ink text-fg antialiased">
        <Providers>
          <div className="flex min-h-screen flex-col">
            <header className="flex h-12 shrink-0 items-center gap-6 border-b border-line px-4">
              <Link href="/" className="text-sm font-semibold tracking-tight">
                Pluton <span className="text-muted">R&amp;D Engine</span>
              </Link>
              <nav className="flex items-center gap-4 text-sm text-muted">
                {NAV.map((item) => (
                  <Link
                    key={item.href}
                    href={item.href}
                    className="transition-colors hover:text-fg"
                  >
                    {item.label}
                  </Link>
                ))}
              </nav>
            </header>
            <main className="flex min-h-0 flex-1 flex-col">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
