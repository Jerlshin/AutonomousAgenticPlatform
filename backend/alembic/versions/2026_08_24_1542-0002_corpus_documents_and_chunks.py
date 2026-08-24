"""RAG corpus: corpus_documents, corpus_chunks.

Revision ID: 0002_corpus
Revises: 0001_baseline
Create Date: 2026-08-24 15:42:42.000000+00:00

Postgres tables for the corpus Phase 3 adds (ARCHITECTURE.md §7.1, §7.3). They mirror the
text and metadata that Qdrant also holds, per the rebuildability rule in §7: Qdrant is a
derived index and may be reconstructed from these tables at any time, but the reverse is
never true.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_corpus'
down_revision: str | None = '0001_baseline'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'corpus_documents',
        sa.Column('id', sa.Uuid(), nullable=False, comment='Unique identifier for the corpus document.'),
        sa.Column('source_uri', sa.Text(), nullable=False, comment='Where this document came from (file path, upload id, or a synthetic URI).'),
        sa.Column('title', sa.String(length=255), nullable=False, comment='Human-readable title shown in retrieval hits and the corpus listing.'),
        sa.Column('collection', sa.String(length=50), nullable=False, comment='The Qdrant collection this document was chunked into.'),
        sa.Column('sha256', sa.String(length=64), nullable=False, comment='Content hash of the raw document text; the idempotency key for re-ingestion.'),
        sa.Column('chunk_count', sa.Integer(), nullable=False, comment='Number of chunks this document was split into.'),
        sa.Column('metadata_json', sa.JSON(), nullable=True, comment="Tags and source-specific attributes carried into every chunk's payload."),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False, comment='UTC timestamp of ingestion.'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sha256'),
    )
    op.create_index(op.f('ix_corpus_documents_collection'), 'corpus_documents', ['collection'], unique=False)
    op.create_index(op.f('ix_corpus_documents_sha256'), 'corpus_documents', ['sha256'], unique=False)

    op.create_table(
        'corpus_chunks',
        sa.Column('id', sa.Uuid(), nullable=False, comment='Unique identifier for the chunk.'),
        sa.Column('document_id', sa.Uuid(), nullable=False, comment='Foreign key referencing the owning CorpusDocument.'),
        sa.Column('chunk_index', sa.Integer(), nullable=False, comment='0-based position of this chunk within the document.'),
        sa.Column('text', sa.Text(), nullable=False, comment='The chunk\'s raw text — the authoritative copy of what Qdrant also holds.'),
        sa.Column('qdrant_point_id', sa.String(length=64), nullable=False, comment='The point id this chunk was upserted as, in `document.collection`.'),
        sa.Column('metadata_json', sa.JSON(), nullable=True, comment='Chunk-specific attributes beyond what the document-level metadata carries.'),
        sa.ForeignKeyConstraint(['document_id'], ['corpus_documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'chunk_index', name='corpus_chunks_unique'),
    )
    op.create_index(op.f('ix_corpus_chunks_document_id'), 'corpus_chunks', ['document_id'], unique=False)
    op.create_index(op.f('ix_corpus_chunks_qdrant_point_id'), 'corpus_chunks', ['qdrant_point_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_corpus_chunks_qdrant_point_id'), table_name='corpus_chunks')
    op.drop_index(op.f('ix_corpus_chunks_document_id'), table_name='corpus_chunks')
    op.drop_table('corpus_chunks')
    op.drop_index(op.f('ix_corpus_documents_sha256'), table_name='corpus_documents')
    op.drop_index(op.f('ix_corpus_documents_collection'), table_name='corpus_documents')
    op.drop_table('corpus_documents')
