"use client";

import { useState } from "react";
import { Segmented } from "@/components/ui/tabs";
import { DocumentTable } from "./DocumentTable";
import { SearchPlayground } from "./SearchPlayground";

/**
 * `/corpus` (§8.9) — two halves, switched rather than stacked.
 *
 * §8.9 describes documents and the retrieval playground as two halves of one view. They
 * are tabs rather than a split screen because both are wide: a document table with a
 * metadata line and a results column with full chunk text each want the viewport, and
 * side by side neither is readable.
 *
 * The tab lives in component state rather than the URL. Nothing links to a specific half,
 * and a persisted tab is a §6.1 `uiStore` concern to add when something needs to deep-link
 * here.
 */

type Half = "documents" | "search";

const TABS = [
  { value: "documents" as const, label: "Documents", title: "Ingested corpus documents" },
  {
    value: "search" as const,
    label: "Retrieval playground",
    title: "What the Researcher would retrieve for a query",
  },
];

export function CorpusClient() {
  const [half, setHalf] = useState<Half>("documents");

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <Segmented
        options={TABS}
        value={half}
        onChange={setHalf}
        label="Corpus view"
        className="self-start"
      />
      {half === "documents" ? <DocumentTable /> : <SearchPlayground />}
    </div>
  );
}
