import logging
import uuid
from typing import Any, Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as rest_models # rest_models

from app.core.config import settings
from app.engine.llm import get_embeddings

logger = logging.getLogger(__name__)

# Asych client wrapper around Qdrant that provides vector embedding, ingestion, and semantic similarity search capabilities for document knowledge bases.

class VectorStoreService:
    """Asynchronous management wrapper for Qdrant vector storage operations."""

    def __init__(self, collection_name: str = "research_knowledge_base") -> None:
        self.collection_name = collection_name
        self.client = AsyncQdrantClient(url=settings.QDRANT_URL) # connects to the Qdrant vector database
        self.embeddings = get_embeddings(model="nomic-embed-text") # loads the embedding client

    # checks whether the target collection exists in Qdrant
    async def ensure_collection_exists(self, vector_size: int = 768) -> None: # 768 dim vector
        """Verifies or creates the target vector collection in Qdrant."""
        try:
            collections = await self.client.get_collections()
            existing_names = [col.name for col in collections.collections]

            if self.collection_name not in existing_names:
                logger.info("Creating Qdrant collection '%s'", self.collection_name)
                await self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=rest_models.VectorParams(
                        size=vector_size,
                        distance=rest_models.Distance.COSINE,
                    ),
                )
        except Exception as exc:
            logger.error("Failed to ensure Qdrant collection existence: %s", exc)
            raise
    
    # accepts a list of raw text strings and optional metadata dict
    async def add_documents(
        self,
        texts: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """Vectorizes and upserts a list of document strings into Qdrant."""
        await self.ensure_collection_exists() # ensure it exists

        if not texts:
            return []

        # Generate dense vector embeddings using local Ollama model
        embeddings_list = self.embeddings.embed_documents(texts) # embed the new doc
        point_ids: list[str] = [] # unique UUID identifier
        points: list[rest_models.PointStruct] = [] # 

        for idx, (text, vector) in enumerate(zip(texts, embeddings_list, strict=False)):
            point_id = str(uuid.uuid4())
            point_ids.append(point_id)

            payload = {"text": text}
            if metadatas and idx < len(metadatas):
                payload.update(metadatas[idx])

            points.append(
                rest_models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            )

        await self.client.upsert(
            collection_name=self.collection_name,
            points=points,
        )
        logger.info("Successfully ingested %d document points into Qdrant", len(points))
        return point_ids

    async def search_similar(
        self,
        query: str,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """Executes vector similarity search against stored knowledge base."""
        await self.ensure_collection_exists()

        query_vector = self.embeddings.embed_query(query)
        search_results = await self.client.search(
            collection_name=self.collection_name,
            query_vector=query_vector,
            limit=limit,
        )

        results = []
        for hit in search_results:
            results.append(
                {
                    "id": hit.id,
                    "score": hit.score,
                    "text": hit.payload.get("text", "") if hit.payload else "",
                    "payload": hit.payload or {},
                }
            )

        return results