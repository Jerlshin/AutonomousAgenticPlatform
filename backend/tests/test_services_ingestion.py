"""Chunking for the RAG corpus (ARCHITECTURE.md §7.3.4).

Pure functions, no Qdrant and no Postgres required — `chunk_text` and `document_sha256`
are exercised directly, the same way `engine/reporting.py`'s pure helpers are tested apart
from the node that calls them.
"""

from __future__ import annotations

from app.services.ingestion import chunk_text, document_sha256


class TestChunkText:
    def test_short_text_is_a_single_chunk(self):
        assert chunk_text("a short document") == ["a short document"]

    def test_empty_text_produces_no_chunks(self):
        assert chunk_text("   ") == []

    def test_long_text_is_split_into_multiple_chunks(self):
        text = ("word " * 400).strip()  # well past the 900-char default
        chunks = chunk_text(text)
        assert len(chunks) > 1
        assert all(
            len(c) <= 900 + 1 for c in chunks
        )  # boundary trimming, not a hard cut

    def test_no_content_is_lost_across_chunk_boundaries(self):
        """Overlap must not drop words — every word from the source appears somewhere."""
        text = " ".join(f"token{i}" for i in range(400))
        chunks = chunk_text(text)
        recovered = " ".join(chunks)
        for i in range(400):
            assert f"token{i}" in recovered

    def test_a_paragraph_boundary_is_preferred_over_a_mid_word_cut(self):
        paragraph_a = "sentence one. " * 40
        paragraph_b = "sentence two. " * 40
        text = paragraph_a.strip() + "\n\n" + paragraph_b.strip()
        chunks = chunk_text(text, size=len(paragraph_a) + 10, overlap=10)
        assert chunks[0].strip() == paragraph_a.strip()

    def test_custom_size_and_overlap_are_honoured(self):
        text = "abcdefghij" * 10  # 100 chars, no whitespace to break on
        chunks = chunk_text(text, size=20, overlap=5)
        assert len(chunks) > 1
        assert all(len(c) <= 20 for c in chunks)


class TestDocumentHash:
    def test_identical_content_hashes_identically(self):
        assert document_sha256("hello world") == document_sha256("hello world")

    def test_surrounding_whitespace_does_not_change_the_hash(self):
        """The hash is the re-ingestion idempotency key; incidental whitespace from a
        copy-paste must not make the same document look new."""
        assert document_sha256("hello world") == document_sha256("  hello world  \n")

    def test_different_content_hashes_differently(self):
        assert document_sha256("hello world") != document_sha256("goodbye world")
