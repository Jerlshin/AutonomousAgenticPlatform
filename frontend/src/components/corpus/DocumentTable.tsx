"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { api, ApiError } from "@/lib/api";
import {
  asIngestCollection,
  INGEST_COLLECTIONS,
  SEARCH_COLLECTIONS,
  type IngestCollection,
} from "@/lib/domain";
import { formatAbsolute, formatRelative, shortHash } from "@/lib/format";
import { qk } from "@/lib/queryKeys";
import type { CorpusDocumentRead } from "@/lib/rest";
import {
  Banner,
  Button,
  Chip,
  Empty,
  ErrorState,
  Panel,
  Skeleton,
} from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/dialog";
import { Field, Select, TextArea, TextInput } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/**
 * The corpus document table, and ingestion (§8.9).
 *
 * Two behaviours here are specified rather than incidental, and both are about telling
 * the truth about what the API will accept:
 *
 * * **A duplicate is not an error.** `POST /corpus/documents` answers `409` when the
 *   `sha256` is already ingested. That is the API confirming the document is present, so
 *   it surfaces as *"this document is already ingested"* in the ordinary tone, not as a
 *   red failure the operator has to interpret.
 * * **`run_memory` is not an ingest target.** It is written exclusively by the Reporter
 *   after a *successful* run, and the schema refuses anything else. The collection
 *   selector offers only what `POST` accepts (`INGEST_COLLECTIONS`) while the *filter*
 *   above the table offers all three, because reading and writing genuinely differ here.
 */

const PAGE_SIZE = 20;

export function DocumentTable() {
  const [page, setPage] = useState(0);
  const [collection, setCollection] = useState("");
  const [ingesting, setIngesting] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<CorpusDocumentRead | null>(null);

  const params = {
    skip: page * PAGE_SIZE,
    limit: PAGE_SIZE,
    collection: collection === "" ? undefined : collection,
  };

  const documents = useQuery({
    queryKey: qk.corpusDocs(params),
    queryFn: () => api.listDocuments(params),
    staleTime: 15_000,
    placeholderData: (previous) => previous,
  });

  const total = documents.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const rows = documents.data?.documents ?? [];

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Documents"
        count={total}
        className="border border-line"
        right={
          <>
            <Select
              label="Filter by collection"
              value={collection}
              onChange={(value) => {
                setCollection(value);
                setPage(0);
              }}
              options={[
                { value: "", label: "All collections" },
                ...SEARCH_COLLECTIONS.map((name) => ({ value: name, label: name })),
              ]}
            />
            <Button tone="primary" onClick={() => setIngesting(true)}>
              Ingest document
            </Button>
          </>
        }
      >
        {documents.isError ? (
          <ErrorState
            title="Could not load documents"
            message="The API did not answer GET /api/v1/corpus/documents."
            onRetry={() => void documents.refetch()}
          />
        ) : documents.isPending ? (
          <Skeleton rows={6} />
        ) : rows.length === 0 ? (
          <Empty
            action={
              <Button onClick={() => setIngesting(true)}>Ingest the first one</Button>
            }
          >
            {collection === ""
              ? "The corpus is empty."
              : `No documents in ${collection}.`}
          </Empty>
        ) : (
          <div className="overflow-auto">
            <Table caption="Ingested corpus documents">
              <THead>
                <TH>Title</TH>
                <TH>Collection</TH>
                <TH numeric>Chunks</TH>
                <TH>Source</TH>
                <TH>SHA-256</TH>
                <TH numeric>Ingested</TH>
                <TH />
              </THead>
              <TBody>
                {rows.map((document) => (
                  <TR key={document.id}>
                    <TD className="max-w-0">
                      <span className="block truncate" title={document.title}>
                        {document.title}
                      </span>
                      <MetadataLine metadata={document.metadata_json} />
                    </TD>
                    <TD>
                      <Chip tone="idle" dot={false}>
                        {document.collection}
                      </Chip>
                    </TD>
                    <TD numeric>{document.chunk_count}</TD>
                    <TD className="max-w-0">
                      <span
                        className="block truncate font-mono text-[11px] text-muted"
                        title={document.source_uri}
                      >
                        {document.source_uri || "—"}
                      </span>
                    </TD>
                    <TD mono title={document.sha256}>
                      {shortHash(document.sha256)}
                    </TD>
                    <TD numeric title={formatAbsolute(document.ingested_at)}>
                      {formatRelative(document.ingested_at)}
                    </TD>
                    <TD className="text-right">
                      <Button
                        tone="danger"
                        onClick={() => setPendingDelete(document)}
                        title="Delete this document and its vector points"
                      >
                        Delete
                      </Button>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}

        <footer className="flex shrink-0 items-center justify-between border-t border-line px-3 py-1.5 text-[11px] text-muted">
          <span className="tnum">
            Page {page + 1} of {pages}
            {documents.isPlaceholderData && " · loading…"}
          </span>
          <span className="flex gap-1.5">
            <Button
              disabled={page === 0}
              onClick={() => setPage((n) => Math.max(0, n - 1))}
            >
              Previous
            </Button>
            <Button disabled={page + 1 >= pages} onClick={() => setPage((n) => n + 1)}>
              Next
            </Button>
          </span>
        </footer>
      </Panel>

      <IngestDialog open={ingesting} onClose={() => setIngesting(false)} />
      <DeleteDialog
        document={pendingDelete}
        onClose={() => setPendingDelete(null)}
      />
    </div>
  );
}

/** The metadata a document carries, kept to one quiet line under the title. */
function MetadataLine({
  metadata,
}: {
  metadata?: Record<string, unknown> | null;
}) {
  const entries = Object.entries(metadata ?? {});
  if (entries.length === 0) return null;
  return (
    <span className="mt-0.5 flex flex-wrap gap-x-2 text-[10px] text-idle">
      {entries.slice(0, 4).map(([key, value]) => (
        <span key={key} className="truncate">
          {key}={typeof value === "object" ? JSON.stringify(value) : String(value)}
        </span>
      ))}
      {entries.length > 4 && <span>+{entries.length - 4}</span>}
    </span>
  );
}

function IngestDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const formId = useId();
  const queryClient = useQueryClient();

  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [sourceUri, setSourceUri] = useState("");
  const [collection, setCollection] = useState<IngestCollection>(INGEST_COLLECTIONS[0]);
  const [tags, setTags] = useState("");

  const ingest = useMutation({
    mutationFn: () =>
      api.ingestDocument({
        title: title.trim(),
        text,
        source_uri: sourceUri.trim() === "" ? null : sourceUri.trim(),
        collection,
        tags: tags
          .split(",")
          .map((tag) => tag.trim())
          .filter((tag) => tag !== ""),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["corpus", "documents"] });
      reset();
      onClose();
    },
  });

  function reset() {
    setTitle("");
    setText("");
    setSourceUri("");
    setTags("");
    ingest.reset();
  }

  // A 409 means the document is already in the corpus — the API confirming a fact, not
  // rejecting the request (§8.9).
  const duplicate = ingest.error instanceof ApiError && ingest.error.status === 409;

  return (
    <Dialog
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Ingest a document"
      description="The text is chunked, embedded, and written to the selected Qdrant collection."
      footer={
        <>
          <Button
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button
            tone="primary"
            disabled={title.trim() === "" || text.trim() === "" || ingest.isPending}
            onClick={() => ingest.mutate()}
          >
            {ingest.isPending ? "Ingesting…" : "Ingest"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {duplicate && (
          <Banner tone="idle" className="rounded border">
            This document is already ingested — a document with the same content hash is
            in the corpus, so nothing was added.
          </Banner>
        )}
        {ingest.isError && !duplicate && (
          <Banner tone="fail" className="rounded border">
            {ingest.error instanceof Error
              ? ingest.error.message
              : "The ingest failed."}
          </Banner>
        )}

        <Field label="Title" htmlFor={`${formId}-title`}>
          <TextInput id={`${formId}-title`} value={title} onChange={setTitle} />
        </Field>

        <Field
          label="Collection"
          htmlFor={`${formId}-collection`}
          hint="run_memory is written only by the Reporter after a successful run, so it cannot be ingested into."
        >
          <Select
            id={`${formId}-collection`}
            label="Collection"
            value={collection}
            onChange={(value) => setCollection(asIngestCollection(value))}
            options={INGEST_COLLECTIONS.map((name) => ({ value: name, label: name }))}
          />
        </Field>

        <Field
          label="Source URI"
          htmlFor={`${formId}-source`}
          hint="Where this came from. Shown on every retrieval hit."
        >
          <TextInput
            id={`${formId}-source`}
            value={sourceUri}
            onChange={setSourceUri}
            placeholder="https://… or a file path"
          />
        </Field>

        <Field label="Tags" htmlFor={`${formId}-tags`} hint="Comma-separated.">
          <TextInput id={`${formId}-tags`} value={tags} onChange={setTags} />
        </Field>

        <Field label="Text" htmlFor={`${formId}-text`}>
          <TextArea id={`${formId}-text`} value={text} onChange={setText} rows={10} />
        </Field>
      </div>
    </Dialog>
  );
}

/**
 * Deletion, with the consequence stated (§8.9).
 *
 * The confirmation names the vector points explicitly because deleting a corpus document
 * is not like deleting a row: the chunks in Qdrant go with it, and a Researcher that was
 * retrieving from them silently stops.
 */
function DeleteDialog({
  document,
  onClose,
}: {
  document: CorpusDocumentRead | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteDocument(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["corpus", "documents"] });
      onClose();
    },
  });

  return (
    <Dialog
      open={document !== null}
      onClose={onClose}
      title="Delete this document?"
      role="alertdialog"
      width="max-w-lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            tone="danger"
            disabled={remove.isPending}
            onClick={() => document && remove.mutate(document.id)}
          >
            {remove.isPending ? "Deleting…" : "Delete"}
          </Button>
        </>
      }
    >
      {document && (
        <div className="flex flex-col gap-2 text-xs">
          <p className="text-fg">{document.title}</p>
          <p className="text-muted">
            Its <span className="tnum">{document.chunk_count}</span> chunk
            {document.chunk_count === 1 ? "" : "s"} in{" "}
            <span className="font-mono">{document.collection}</span> are removed from
            Qdrant with it. Retrieval stops returning this content immediately, and
            re-ingesting requires the original text.
          </p>
          {remove.isError && (
            <Banner tone="fail" className="rounded border">
              {remove.error instanceof Error
                ? remove.error.message
                : "The delete failed."}
            </Banner>
          )}
        </div>
      )}
    </Dialog>
  );
}
