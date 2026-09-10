"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The app's navigation (§8.1).
 *
 * A Client Component only because the current route decides which link is marked
 * `aria-current`; everything else about the header is static chrome and stays on the
 * server. Pushing the boundary here rather than at `layout.tsx` keeps the shell out of
 * the client bundle.
 */

const NAV = [
  { href: "/", label: "Dashboard", exact: true },
  { href: "/tasks", label: "Tasks", exact: false },
  { href: "/corpus", label: "Corpus", exact: false },
  { href: "/benchmarks", label: "Benchmarks", exact: false },
] as const;

export function NavBar() {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary" className="flex items-center gap-4 text-sm">
      {NAV.map((item) => {
        const active = item.exact
          ? pathname === item.href
          : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={clsx(
              "transition-colors duration-150",
              active ? "text-fg" : "text-muted hover:text-fg",
            )}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
