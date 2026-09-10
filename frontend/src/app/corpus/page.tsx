import { CorpusClient } from "@/components/corpus/CorpusClient";

/**
 * `/corpus` (§8.9): a Server shell around the documents table and retrieval playground.
 */
export default function CorpusPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4">
      <header>
        <h1 className="text-sm font-semibold text-fg">Corpus</h1>
        <p className="text-xs text-muted">
          What the Researcher can retrieve, and what it returns for a given question.
        </p>
      </header>
      <CorpusClient />
    </div>
  );
}
