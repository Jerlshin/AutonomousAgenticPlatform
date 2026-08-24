"""RAG corpus ORM models: Postgres is authoritative, Qdrant is derived.

ARCHITECTURE.md §7 states the rule these two tables exist to enforce: Qdrant never holds
authoritative document text, only vectors and a mirror of the payload. `CorpusDocument` and
`CorpusChunk` are that authoritative copy, so `make rebuild-derived` can always reconstruct
every Qdrant point from what is in Postgres, and a document survives a Qdrant data loss.

`metadata_json` is named that way rather than `metadata` — matching `Artifact.metadata_json`
and `AgentLog.metadata_json` elsewhere in this package — because `metadata` is a reserved
attribute name on SQLAlchemy's `DeclarativeBase`.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    pass


class CorpusDocument(Base):
    """One ingested document, mirroring ARCHITECTURE.md §7.1's `corpus_documents`."""

    __tablename__ = "corpus_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the corpus document.",
    )
    source_uri: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Where this document came from (file path, upload id, or a synthetic URI).",
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable title shown in retrieval hits and the corpus listing.",
    )
    collection: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="rd_corpus",
        index=True,
        comment="The Qdrant collection this document was chunked into.",
    )
    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="Content hash of the raw document text; the idempotency key for re-ingestion.",
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of chunks this document was split into.",
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Tags and source-specific attributes carried into every chunk's payload.",
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="UTC timestamp of ingestion.",
    )

    chunks: Mapped[list["CorpusChunk"]] = relationship(
        "CorpusChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<CorpusDocument id={self.id} title='{self.title}' collection={self.collection}>"


class CorpusChunk(Base):
    """One chunk of a `CorpusDocument`, pointing at its Qdrant point (§7.1)."""

    __tablename__ = "corpus_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="corpus_chunks_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the chunk.",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("corpus_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Foreign key referencing the owning CorpusDocument.",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="0-based position of this chunk within the document.",
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The chunk's raw text — the authoritative copy of what Qdrant also holds.",
    )
    qdrant_point_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="The point id this chunk was upserted as, in `document.collection`.",
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Chunk-specific attributes beyond what the document-level metadata carries.",
    )

    document: Mapped["CorpusDocument"] = relationship(
        "CorpusDocument", back_populates="chunks"
    )

    def __repr__(self) -> str:
        return f"<CorpusChunk id={self.id} document_id={self.document_id} index={self.chunk_index}>"
