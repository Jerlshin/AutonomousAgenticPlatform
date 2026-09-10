import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="flex max-w-lg flex-col items-center gap-3 text-center">
        <p className="text-sm font-semibold">This page does not exist.</p>
        <p className="text-xs text-muted">
          The run or task may have been deleted, or the link may be from an older version
          of the dashboard.
        </p>
        <Link
          href="/"
          className="rounded border border-line px-2 py-1 text-xs transition-colors hover:bg-raised"
        >
          Back to the dashboard
        </Link>
      </div>
    </div>
  );
}
