"use client";

import { useState } from "react";
import { Button } from "@/components/ui/primitives";

/**
 * The report's actions (§8.8): copy Markdown · download `.md` · print · MLflow.
 *
 * The one Client Component on an otherwise server-rendered page, and it exists only
 * because clipboards, blobs and `window.print()` are browser APIs. Keeping the boundary
 * this small is the §2.2 rule in practice — the Markdown itself never becomes client
 * state beyond the string these three handlers need.
 *
 * The source text is passed down rather than re-fetched: it is already on the page, and a
 * second authenticated request for a document the reader is looking at would need the
 * browser-visible token that §2.2 exists to avoid.
 */

export function ReportActions({
  markdown,
  runId,
  mlflowUrl,
}: {
  markdown: string;
  runId: string;
  mlflowUrl: string | null;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // A denied clipboard permission is not worth an error dialog; the download below
      // is the fallback and it needs no permission.
      setCopied(false);
    }
  }

  function download() {
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `REPORT-${runId.slice(0, 8)}.md`;
    anchor.click();
    // Revoked on the next tick rather than immediately: Safari has not started reading
    // the blob when `click()` returns, and revoking synchronously yields an empty file.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <div className="flex items-center gap-1.5">
      <Button onClick={copy} title="Copy the raw Markdown to the clipboard">
        {copied ? "Copied" : "Copy Markdown"}
      </Button>
      <Button onClick={download} title="Download REPORT.md">
        Download
      </Button>
      <Button onClick={() => window.print()} title="Print with the A4 stylesheet">
        Print
      </Button>
      {mlflowUrl && (
        <a
          href={mlflowUrl}
          target="_blank"
          rel="noreferrer"
          className="rounded border border-line px-2 py-1 text-xs text-muted transition-colors hover:bg-raised hover:text-fg"
        >
          MLflow
        </a>
      )}
    </div>
  );
}
