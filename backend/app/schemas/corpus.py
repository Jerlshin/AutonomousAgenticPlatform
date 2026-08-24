"""Request/response schemas for `/corpus` — RAG management and retrieval inspection.

ARCHITECTURE.md §8.2. `CollectionLiteral` intentionally excludes `run_memory` from
ingestion: episodic memory is written exclusively by the Reporter after a successful run
(`AGENTS.md` §7.8), never by a human uploading a document, so `CorpusDocumentCreate` cannot
target it even though `/corpus/search` can inspect it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IngestCollection = Literal["rd_corpus", "code_exemplars"]
SearchCollection = Literal["rd_corpus", "code_exemplars", "run_memory"]


class CorpusDocumentCreate(BaseModel):
    """Payload for ingesting one document into the corpus."""

    title: str = Field(..., min_length=1, max_length=255)
    text: str = Field(
        ..., min_length=1, description="Raw document text to chunk and index."
    )
    source_uri: str | None = Field(
        default=None,
        description="Where this came from. Defaults to a synthetic upload URI.",
    )
    collection: IngestCollection = Field(default="rd_corpus")
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CorpusDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_uri: str
    title: str
    collection: str
    sha256: str
    chunk_count: int
    metadata_json: dict[str, Any] | None = None
    ingested_at: datetime


class CorpusDocumentListResponse(BaseModel):
    total: int
    documents: list[CorpusDocumentRead]


class CorpusSearchRequest(BaseModel):
    """Debug retrieval directly — what the Researcher would see for this query."""

    query: str = Field(..., min_length=1)
    collection: SearchCollection = Field(default="rd_corpus")
    top_k: int = Field(default=6, ge=1, le=50)
    task_kind: str | None = Field(
        default=None, description="Filters code_exemplars/run_memory to this task kind."
    )


class CorpusSearchHit(BaseModel):
    point_id: str
    score: float
    source_uri: str = ""
    title: str = ""
    section: str = ""
    text: str
    trust_level: str = "curated"


class CorpusSearchResponse(BaseModel):
    query: str
    collection: str
    hits: list[CorpusSearchHit]
