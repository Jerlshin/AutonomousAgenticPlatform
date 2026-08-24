"""RAG corpus management and retrieval inspection (ARCHITECTURE.md §8.2, §7.3).

Postgres (`corpus_documents`/`corpus_chunks`) is authoritative; Qdrant is the derived
vector index `app.services.ingestion` keeps in step with it. `/corpus/search` exists so an
operator can see exactly what the Researcher would retrieve for a query — the same hybrid
search, the same collections — without running a whole graph to find out.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import require_token
from app.db.models.corpus import CorpusDocument
from app.schemas.common import StandardResponse
from app.schemas.corpus import (
    CorpusDocumentCreate,
    CorpusDocumentListResponse,
    CorpusDocumentRead,
    CorpusSearchHit,
    CorpusSearchRequest,
    CorpusSearchResponse,
)
from app.services import ingestion
from app.services.vector_store import VectorStoreService

# §13.2: every non-health endpoint requires `Authorization: Bearer {PLATFORM_API_TOKEN}`.
# Declared on the router rather than per-endpoint so a route added later inherits it —
# authentication that has to be remembered on each handler is authentication that will
# eventually be forgotten on one.
router = APIRouter(dependencies=[Depends(require_token)])


@router.post(
    "/documents",
    response_model=CorpusDocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a corpus document",
)
async def create_document(
    payload: CorpusDocumentCreate, db: AsyncSession = Depends(get_db)
) -> Any:
    """Chunk, embed and index a document into `rd_corpus` or `code_exemplars`."""
    sha256 = ingestion.document_sha256(payload.text)
    existing = await db.scalar(
        select(CorpusDocument).where(CorpusDocument.sha256 == sha256)
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A document with this content already exists (id={existing.id}).",
        )

    try:
        document = await ingestion.ingest_document(
            title=payload.title,
            text=payload.text,
            source_uri=payload.source_uri or f"upload://{payload.title}",
            collection=payload.collection,
            metadata={"tags": payload.tags, **payload.metadata},
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await db.commit()
    await db.refresh(document)
    return document


@router.get(
    "/documents",
    response_model=CorpusDocumentListResponse,
    summary="List corpus documents",
)
async def list_documents(
    collection: str | None = Query(default=None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Any:
    stmt = select(CorpusDocument)
    count_stmt = select(func.count(CorpusDocument.id))
    if collection is not None:
        stmt = stmt.where(CorpusDocument.collection == collection)
        count_stmt = count_stmt.where(CorpusDocument.collection == collection)

    total = await db.scalar(count_stmt)
    result = await db.execute(
        stmt.order_by(CorpusDocument.ingested_at.desc()).offset(skip).limit(limit)
    )
    documents = result.scalars().all()
    return CorpusDocumentListResponse(
        total=total or 0,
        documents=[CorpusDocumentRead.model_validate(doc) for doc in documents],
    )


@router.delete(
    "/documents/{doc_id}",
    response_model=StandardResponse,
    summary="Delete a corpus document",
)
async def remove_document(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Any:
    """Delete a document and every Qdrant point it was chunked into."""
    document = await db.get(CorpusDocument, doc_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {doc_id} not found.",
        )

    await ingestion.delete_document(document, db=db)
    await db.commit()
    return StandardResponse(
        message=f"Document {doc_id} and its indexed chunks were deleted."
    )


@router.post(
    "/search", response_model=CorpusSearchResponse, summary="Inspect retrieval directly"
)
async def search_corpus(payload: CorpusSearchRequest) -> Any:
    """Run the same retrieval the Researcher uses, without a graph run — for debugging
    what a query actually surfaces before trusting it inside a prompt."""
    store = VectorStoreService()

    if payload.collection == "run_memory":
        hits = await store.search_run_memory(
            fingerprint=payload.query,
            message="",
            task_kind=payload.task_kind or "",
            limit=payload.top_k,
        )
        results = [
            CorpusSearchHit(
                point_id=str(hit.get("run_id", "")),
                score=1.0,
                source_uri=str(hit.get("run_id", "")),
                title=hit.get("error_fingerprint", ""),
                text=hit.get("fix_summary", ""),
                trust_level="verified",
            )
            for hit in hits
        ]
    elif payload.collection == "code_exemplars":
        raw = await store.search_code_exemplars(
            payload.query, task_kind=payload.task_kind or "", limit=payload.top_k
        )
        results = [_hit_from_chunk(hit) for hit in raw]
    else:
        raw = await store.search_rd_corpus(payload.query, limit=payload.top_k)
        results = [_hit_from_chunk(hit) for hit in raw]

    return CorpusSearchResponse(
        query=payload.query, collection=payload.collection, hits=results
    )


def _hit_from_chunk(chunk: dict[str, Any]) -> CorpusSearchHit:
    """A `VectorStoreService` chunk dict carries a `collection` field `CorpusSearchHit`
    does not — the response is already scoped to one collection — so this maps explicitly
    rather than splatting the dict into the model."""
    return CorpusSearchHit(
        point_id=chunk["point_id"],
        score=chunk["score"],
        source_uri=chunk["source_uri"],
        title=chunk["title"],
        section=chunk["section"],
        text=chunk["text"],
        trust_level=chunk["trust_level"],
    )
