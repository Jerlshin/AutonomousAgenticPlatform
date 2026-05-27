import os
import aiohttp
from typing import List
from qdrant_client import AsyncQdrantClient
from core.config import settings

class VectorResearchTool:
    def __init__(self):
        self.qdrant_host = os.getenv("QDRANT_HOST", "localhost")
        self.qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
        self.client = AsyncQdrantClient(host=self.qdrant_host, port=self.qdrant_port)
        self.ollama_base = settings.ollama_base_url.rstrip("/")
        
    async def get_embedding(self, text: str) -> List[float]:
        async with aiohttp.ClientSession() as session:
            payload = {"model": "nomic-embed-text", "prompt": text}
            async with session.post(f"{self.ollama_base}/api/embeddings", json=payload) as resp:
                data = await resp.json()
                return data.get("embedding", [])

    async def search(self, query: str, limit: int = 5) -> List[str]:
        try:
            embedding = await self.get_embedding(query)
            if not embedding:
                return ["No embedding generated."]
            
            # Note: collection must be created and populated elsewhere
            results = await self.client.search(
                collection_name="knowledge_base",
                query_vector=embedding,
                limit=limit
            )
            return [hit.payload.get("text", "") for hit in results if hit.payload]
        except Exception as e:
            return [f"Vector search unavailable: {e}"]

research_tool = VectorResearchTool()
