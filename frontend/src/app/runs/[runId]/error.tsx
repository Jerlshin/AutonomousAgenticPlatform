"use client";

import Link from "next/link";
import { Button } from "@/components/ui/primitives";

export default function RunError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="flex max-w-lg flex-col items-center gap-3 text-center">
        <p className="text-sm font-semibold text-fail">✗ This run view failed.</p>
        <p className="text-xs text-muted">{error.message}</p>
        <div className="flex gap-2">
          <Button onClick={reset}>Try again</Button>
          <Link
            href="/tasks"
            className="rounded border border-line px-2 py-1 text-xs transition-colors hover:bg-raised"
          >
            Back to tasks
          </Link>
        </div>
      </div>
    </div>
  );
}
