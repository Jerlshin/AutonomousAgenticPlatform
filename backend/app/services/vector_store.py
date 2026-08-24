"""Async Qdrant service — hybrid dense+sparse retrieval with RRF fusion.

Specification: `ARCHITECTURE.md` §7.3. Three collections (`rd_corpus`, `code_exemplars`,
`run_memory`) share one vector configuration: a 768-dim dense vector (`nomic-embed-text`,
cosine) for semantic recall, plus a sparse vector for lexical precision. Pure dense
retrieval systematically misses exact identifiers — API names, error codes,
hyperparameter names — which is precisely what a coding agent needs to find, so the two
are combined with Qdrant's native Reciprocal Rank Fusion (`Fusion.RRF` in `query_points`)
rather than a hand-rolled blend (defect D-005: the previous version called the deprecated,
dense-only `search`).

The sparse side is a hashed bag-of-words: each token is folded into a fixed index space by
`blake2b`, so no vocabulary state has to be built or shared between ingestion and query.
Qdrant applies IDF scaling server-side (`modifier=Modifier.IDF` on the collection), which
is the part of BM25 that is a property of the whole corpus rather than of one document —
the client only ever has to supply term frequency.

Qdrant and the embedding model are both optional at import time, matching `engine/llm.py`:
the whole engine test suite runs against a `FakeChatModel` and a fake vector store with
neither installed (`AGENTS.md` §12.2), so `qdrant_client` is imported lazily, inside the
methods that need it, never at module scope.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

CollectionName = Literal["rd_corpus", "code_exemplars", "run_memory"]

# Sparse vector dimensionality for the hashing trick. Large enough that collisions across
# a corpus of a few thousand chunks are rare, small enough that a query stays a short
# sparse vector rather than approaching the width of the dense one.
SPARSE_DIM = 2**18

_TOKEN = re.compile(r"[a-zA-Z0-9_]+")

# ARCHITECTURE.md §7.3.4.
PREFETCH_LIMIT = 24
RD_CORPUS_LIMIT = 8
CODE_EXEMPLAR_LIMIT = 4
FINAL_TOP_K = 6
SCORE_FLOOR = 0.35
RUN_MEMORY_SCORE_FLOOR = 0.82


@dataclass(frozen=True)
class CollectionSpec:
    """One collection's payload-index requirements (§7.3.1–§7.3.3).

    Unindexed filters force a full scan, so every field a node or an API endpoint filters
    on is declared here and created once per process.
    """

    name: str
    payload_indexes: tuple[tuple[str, str], ...]


COLLECTIONS: dict[str, CollectionSpec] = {
    "rd_corpus": CollectionSpec(
        name="rd_corpus",
        payload_indexes=(
            ("tags", "keyword"),
            ("lang", "keyword"),
            ("doc_id", "keyword"),
            ("content_type", "keyword"),
            ("ingested_at", "datetime"),
        ),
    ),
    "code_exemplars": CollectionSpec(
        name="code_exemplars",
        payload_indexes=(
            ("task_kind", "keyword"),
            ("framework", "keyword"),
            ("tested", "bool"),
            ("doc_id", "keyword"),
        ),
    ),
    "run_memory": CollectionSpec(
        name="run_memory",
        payload_indexes=(
            ("task_kind", "keyword"),
            ("outcome", "keyword"),
            ("error_fingerprint", "keyword"),
        ),
    ),
}

# Collections ensured (created + payload-indexed) in this process. Module-level rather
# than per-instance: `VectorStoreService()` is cheap and created fresh per node call, and
# without a process-wide cache every call would re-issue the same idempotent-but-wasted
# create_collection/create_payload_index round trips.
_ENSURED: set[str] = set()


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def sparse_vector(text: str) -> Any:
    """A hashed bag-of-words sparse vector: per-document term frequency.

    Qdrant applies IDF scaling server-side, so the client only supplies the part of BM25
    that belongs to one document. The same tokenizer and hash run at ingestion and at
    query time, which is the only property that actually matters here — the index space
    does not need to correspond to a real vocabulary.
    """
    from qdrant_client.http import models as rest_models

    counts: dict[int, int] = {}
    for token in _tokenize(text):
        index = (
            int.from_bytes(
                hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "big"
            )
            % SPARSE_DIM
        )
        counts[index] = counts.get(index, 0) + 1
    if not counts:
        return rest_models.SparseVector(indices=[], values=[])
    indices, values = zip(*sorted(counts.items()), strict=True)
    return rest_models.SparseVector(
        indices=list(indices), values=[float(v) for v in values]
    )


def _chunk_dict(collection: str, point: Any) -> dict[str, Any]:
    """A Qdrant scored point, shaped exactly like `engine.state.RetrievedChunk`'s fields."""
    payload = point.payload or {}
    return {
        "point_id": str(point.id),
        "collection": collection,
        "score": float(point.score),
        "source_uri": payload.get("source_uri", ""),
        "title": payload.get("title", ""),
        "section": payload.get("section", ""),
        "text": payload.get("text", ""),
        "trust_level": payload.get("trust_level", "curated"),
    }


class VectorStoreService:
    """Ingestion and hybrid retrieval over the three Qdrant collections."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._embeddings: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                url=settings.QDRANT_URL, prefer_grpc=settings.QDRANT_PREFER_GRPC
            )
        return self._client

    @property
    def embeddings(self) -> Any:
        if self._embeddings is None:
            from app.engine.llm import get_embeddings

            self._embeddings = get_embeddings(model=settings.EMBEDDING_MODEL)
        return self._embeddings

    # ------------------------------------------------------------------------------
    #  Collection lifecycle
    # ------------------------------------------------------------------------------

    async def ensure_collections(self) -> None:
        """Create every collection and its payload indexes, once per process."""
        pending = [spec for name, spec in COLLECTIONS.items() if name not in _ENSURED]
        if not pending:
            return
        for spec in pending:
            await self._ensure_collection(spec)
            _ENSURED.add(spec.name)

    async def _ensure_collection(self, spec: CollectionSpec) -> None:
        from qdrant_client.http import models as rest_models

        existing = {c.name for c in (await self.client.get_collections()).collections}
        if spec.name not in existing:
            logger.info("Creating Qdrant collection '%s'", spec.name)
            await self.client.create_collection(
                collection_name=spec.name,
                vectors_config={
                    "dense": rest_models.VectorParams(
                        size=settings.EMBEDDING_DIM,
                        distance=rest_models.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    "sparse": rest_models.SparseVectorParams(
                        modifier=rest_models.Modifier.IDF,
                    ),
                },
                hnsw_config=rest_models.HnswConfigDiff(
                    m=16, ef_construct=128, full_scan_threshold=10000
                ),
                quantization_config=rest_models.ScalarQuantization(
                    scalar=rest_models.ScalarQuantizationConfig(
                        type=rest_models.ScalarType.INT8,
                        quantile=0.99,
                        always_ram=True,
                    )
                ),
                optimizers_config=rest_models.OptimizersConfigDiff(
                    default_segment_number=2, indexing_threshold=20000
                ),
                on_disk_payload=True,
            )

        schema_map = {
            "keyword": rest_models.PayloadSchemaType.KEYWORD,
            "bool": rest_models.PayloadSchemaType.BOOL,
            "datetime": rest_models.PayloadSchemaType.DATETIME,
        }
        for field, kind in spec.payload_indexes:
            try:
                await self.client.create_payload_index(
                    collection_name=spec.name,
                    field_name=field,
                    field_schema=schema_map[kind],
                )
            except Exception as exc:  # noqa: BLE001 - already-indexed is not an error
                logger.debug(
                    "Payload index %s.%s not (re)created: %s", spec.name, field, exc
                )

    # ------------------------------------------------------------------------------
    #  Embedding
    # ------------------------------------------------------------------------------

    async def _embed_documents(self, texts: list[str]) -> list[list[float]]:
        embed = getattr(self.embeddings, "aembed_documents", None)
        if embed is not None:
            return await embed(texts)
        import asyncio

        return await asyncio.to_thread(self.embeddings.embed_documents, texts)

    async def _embed_query(self, text: str) -> list[float]:
        embed = getattr(self.embeddings, "aembed_query", None)
        if embed is not None:
            return await embed(text)
        import asyncio

        return await asyncio.to_thread(self.embeddings.embed_query, text)

    # ------------------------------------------------------------------------------
    #  Ingestion
    # ------------------------------------------------------------------------------

    async def add_documents(
        self,
        collection: CollectionName,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Embed and upsert `texts` into `collection`. Returns the assigned point ids."""
        await self.ensure_collections()
        if not texts:
            return []
        from qdrant_client.http import models as rest_models

        dense_vectors = await self._embed_documents(texts)
        point_ids: list[str] = []
        points: list[Any] = []
        for index, (text, dense) in enumerate(zip(texts, dense_vectors, strict=True)):
            point_id = str(uuid.uuid4())
            point_ids.append(point_id)
            payload: dict[str, Any] = {"text": text}
            if metadatas and index < len(metadatas):
                payload.update(metadatas[index])
            points.append(
                rest_models.PointStruct(
                    id=point_id,
                    vector={"dense": dense, "sparse": sparse_vector(text)},
                    payload=payload,
                )
            )

        await self.client.upsert(collection_name=collection, points=points)
        logger.info("Ingested %d point(s) into '%s'", len(points), collection)
        return point_ids

    async def delete_points(
        self, collection: CollectionName, point_ids: list[str]
    ) -> None:
        if not point_ids:
            return
        from qdrant_client.http import models as rest_models

        await self.client.delete(
            collection_name=collection,
            points_selector=rest_models.PointIdsList(points=point_ids),
        )

    # ------------------------------------------------------------------------------
    #  Hybrid retrieval
    # ------------------------------------------------------------------------------

    async def hybrid_search(
        self,
        *,
        collection: CollectionName,
        query: str,
        limit: int = FINAL_TOP_K,
        prefetch_limit: int = PREFETCH_LIMIT,
        score_threshold: float | None = SCORE_FLOOR,
        query_filter: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Dense + sparse prefetch, fused by Qdrant-native RRF (§7.3.4)."""
        await self.ensure_collections()
        from qdrant_client.http import models as rest_models

        # The clock starts before the embedding call on purpose: from the Researcher's
        # point of view a retrieval takes as long as it takes, and on this hardware the
        # embedding round trip is usually the larger half. Timing only the Qdrant call
        # would produce a latency panel that stays flat while retrieval gets slower.
        started = time.perf_counter()
        dense = await self._embed_query(query)
        sparse = sparse_vector(query)

        result = await self.client.query_points(
            collection_name=collection,
            prefetch=[
                rest_models.Prefetch(
                    query=dense,
                    using="dense",
                    limit=prefetch_limit,
                    filter=query_filter,
                ),
                rest_models.Prefetch(
                    query=sparse,
                    using="sparse",
                    limit=prefetch_limit,
                    filter=query_filter,
                ),
            ],
            query=rest_models.FusionQuery(fusion=rest_models.Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        metrics.observe_retrieval(
            collection,
            duration_s=time.perf_counter() - started,
            scores=[
                float(getattr(point, "score", 0.0) or 0.0) for point in result.points
            ],
        )
        return [_chunk_dict(collection, point) for point in result.points]

    async def search_rd_corpus(
        self,
        query: str,
        *,
        limit: int = RD_CORPUS_LIMIT,
        score_threshold: float = SCORE_FLOOR,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self.hybrid_search(
            collection="rd_corpus",
            query=query,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=self._tags_filter(tags),
        )

    async def search_code_exemplars(
        self, query: str, *, task_kind: str, limit: int = CODE_EXEMPLAR_LIMIT
    ) -> list[dict[str, Any]]:
        """Verified snippets only — `tested == true` is enforced server-side (§7.3.2)."""
        from qdrant_client.http import models as rest_models

        must = [
            rest_models.FieldCondition(
                key="tested", match=rest_models.MatchValue(value=True)
            )
        ]
        if task_kind:
            must.append(
                rest_models.FieldCondition(
                    key="task_kind", match=rest_models.MatchValue(value=task_kind)
                )
            )
        return await self.hybrid_search(
            collection="code_exemplars",
            query=query,
            limit=limit,
            score_threshold=None,
            query_filter=rest_models.Filter(must=must),
        )

    def _tags_filter(self, tags: list[str] | None) -> Any:
        if not tags:
            return None
        from qdrant_client.http import models as rest_models

        return rest_models.Filter(
            must=[
                rest_models.FieldCondition(
                    key="tags", match=rest_models.MatchAny(any=tags)
                )
            ]
        )

    # ------------------------------------------------------------------------------
    #  Episodic memory  (`run_memory`, ARCHITECTURE.md §7.3.3)
    # ------------------------------------------------------------------------------

    async def search_run_memory(
        self, *, fingerprint: str, task_kind: str, message: str = "", limit: int = 3
    ) -> list[dict[str, Any]]:
        """Plain dense lookup by fingerprint, filtered to prior *successful* fixes.

        Deliberately not RRF-fused: §7.3.3 is a single `query_points` call over the dense
        vector with a `score_threshold=0.82`, which only means something against a raw
        cosine score — a fused RRF score is rank-based and a fixed 0.82 floor on it would
        not correspond to "similar enough to be useful".
        """
        await self.ensure_collections()
        from qdrant_client.http import models as rest_models

        started = time.perf_counter()
        dense = await self._embed_query(f"{fingerprint} {message}".strip())
        must = [
            rest_models.FieldCondition(
                key="outcome", match=rest_models.MatchValue(value="SUCCEEDED")
            )
        ]
        if task_kind:
            must.append(
                rest_models.FieldCondition(
                    key="task_kind", match=rest_models.MatchValue(value=task_kind)
                )
            )

        result = await self.client.query_points(
            collection_name="run_memory",
            query=dense,
            using="dense",
            query_filter=rest_models.Filter(must=must),
            limit=limit,
            score_threshold=RUN_MEMORY_SCORE_FLOOR,
            with_payload=True,
        )
        metrics.observe_retrieval(
            "run_memory",
            duration_s=time.perf_counter() - started,
            scores=[
                float(getattr(point, "score", 0.0) or 0.0) for point in result.points
            ],
        )
        # The hit/miss counter is separate from the hits histogram because the question
        # this collection answers is binary — "had we seen this error before?" — and the
        # KPI in AGENTS.md §13 is a rate, not a distribution.
        metrics.record_run_memory_lookup(len(result.points))
        return [point.payload or {} for point in result.points]

    async def write_run_memory(
        self,
        *,
        run_id: str,
        task_kind: str,
        outcome: str,
        error_fingerprint: str,
        error_excerpt: str,
        fix_summary: str,
        fix_diff: str = "",
        debug_iterations: int = 0,
        final_score: float = 0.0,
    ) -> str:
        """Persist one error→fix pair. Callers must only invoke this for SUCCEEDED runs —
        recording fixes from failed runs would poison the memory with approaches that did
        not actually work (`AGENTS.md` §7.8)."""
        await self.ensure_collections()
        from qdrant_client.http import models as rest_models

        text = f"{error_fingerprint} {error_excerpt} {fix_summary}".strip()
        dense = await self._embed_query(text)
        point_id = str(uuid.uuid4())
        payload = {
            "run_id": run_id,
            "task_kind": task_kind,
            "outcome": outcome,
            "error_fingerprint": error_fingerprint,
            "error_excerpt": error_excerpt,
            "fix_summary": fix_summary,
            "fix_diff": fix_diff,
            "debug_iterations": debug_iterations,
            "final_score": final_score,
            "created_at": datetime.now(UTC).isoformat(),
            "text": text,
        }
        await self.client.upsert(
            collection_name="run_memory",
            points=[
                rest_models.PointStruct(
                    id=point_id,
                    vector={"dense": dense, "sparse": sparse_vector(text)},
                    payload=payload,
                )
            ],
        )
        logger.info(
            "Recorded run_memory point %s for fingerprint %s",
            point_id,
            error_fingerprint,
        )
        return point_id


__all__ = [
    "COLLECTIONS",
    "CollectionName",
    "CollectionSpec",
    "VectorStoreService",
    "sparse_vector",
]
