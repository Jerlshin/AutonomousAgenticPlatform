"""Chunking and ingestion for the RAG corpus (ARCHITECTURE.md §7.3.4, §7).

Postgres is authoritative for a corpus document's text (`corpus_documents`,
`corpus_chunks`); Qdrant is a derived index of the same text plus its vectors. Ingestion
therefore always writes Postgres first and Qdrant second: a Qdrant failure midway leaves a
document Postgres still knows about and can re-embed, never one that exists nowhere.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.db.models.corpus import CorpusChunk, CorpusDocument
from app.services.vector_store import CollectionName, VectorStoreService

logger = logging.getLogger(__name__)

# ARCHITECTURE.md §7.3.4.
CHUNK_SIZE = 900
CHUNK_OVERLAP = 150


def chunk_text(
    text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """A recursive-character splitter: prefer a paragraph or word boundary, hard-cut only
    when neither is available within the window.

    Header-aware Markdown splitting (§7.3.4) is a refinement for later ingestion sources;
    this is the fallback layer every document — Markdown, prose, or code — always has, and
    it is the whole of what a plain-text document needs.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            boundary = text.rfind("\n\n", start, end)
            if boundary <= start:
                boundary = text.rfind("\n", start, end)
            if boundary <= start:
                boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def document_sha256(text: str) -> str:
    """The idempotency key for re-ingestion: identical content, identical hash."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


async def ingest_document(
    *,
    title: str,
    text: str,
    source_uri: str,
    collection: CollectionName,
    metadata: dict[str, Any],
    db: Any,
    store: VectorStoreService | None = None,
) -> CorpusDocument:
    """Chunk, embed and persist one document. Returns the (flushed, not yet committed)
    `CorpusDocument` row — the caller owns the transaction boundary."""
    pieces = chunk_text(text)
    if not pieces:
        raise ValueError("document text is empty after chunking")

    document = CorpusDocument(
        source_uri=source_uri,
        title=title,
        collection=collection,
        sha256=document_sha256(text),
        chunk_count=len(pieces),
        metadata_json=metadata,
    )
    db.add(document)
    await db.flush()  # assigns document.id inside the caller's transaction

    tags = metadata.get("tags") or []
    store = store or VectorStoreService()
    point_ids = await store.add_documents(
        collection,
        pieces,
        metadatas=[
            {
                "doc_id": str(document.id),
                "chunk_index": index,
                "source_uri": source_uri,
                "title": title,
                "tags": tags,
                **{k: v for k, v in metadata.items() if k != "tags"},
            }
            for index in range(len(pieces))
        ],
    )

    for index, (piece, point_id) in enumerate(zip(pieces, point_ids, strict=True)):
        db.add(
            CorpusChunk(
                document_id=document.id,
                chunk_index=index,
                text=piece,
                qdrant_point_id=point_id,
                metadata_json={},
            )
        )

    logger.info(
        "Ingested document '%s' (%s) into '%s': %d chunks",
        title,
        document.id,
        collection,
        len(pieces),
    )
    return document


async def delete_document(
    document: CorpusDocument, *, db: Any, store: VectorStoreService | None = None
) -> None:
    """Remove a document's Qdrant points, then the Postgres row (and its chunks, by cascade)."""
    store = store or VectorStoreService()
    point_ids = [chunk.qdrant_point_id for chunk in document.chunks]
    await store.delete_points(document.collection, point_ids)  # type: ignore[arg-type]
    await db.delete(document)


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "chunk_text",
    "delete_document",
    "document_sha256",
    "ingest_document",
]
