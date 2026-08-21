import logging
from typing import Any

from langchain_core.tools import tool

from app.services.vector_store import VectorStoreService

logger = logging.getLogger(__name__)


@tool
async def search_knowledge_base(query: str) -> str:
    """Searches the Qdrant vector database for relevant domain context, documents, or baseline code.

    Args:
        query: Natural language research query or technical keywords.

    Returns:
        Formatted string containing relevant context snippets.
    """
    logger.info("Agent invoking search_knowledge_base with query: '%s'", query)

    try:
        vector_service = VectorStoreService()
        results = await vector_service.search_similar(query=query, limit=4)

        if not results:
            return "No relevant context found in local knowledge base."

        formatted_snippets = []
        for idx, res in enumerate(results, 1):
            score = round(res["score"], 3)
            snippet = res["text"].strip()
            formatted_snippets.append(f"--- Context Result #{idx} (Score: {score}) ---\n{snippet}")

        return "\n\n".join(formatted_snippets)
    except Exception as exc:
        logger.error("Error executing vector knowledge base search: %s", exc)
        return f"Failed to retrieve context from knowledge base due to error: {exc}"