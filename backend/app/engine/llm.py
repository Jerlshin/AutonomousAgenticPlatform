import logging
from typing import Optional

from langchain_community.chat_models import ChatOllama
from langchain_community.embeddings import OllamaEmbeddings

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_llm(
    model: Optional[str] = None,
    temperature: float = 0.2,
) -> ChatOllama:
    """Instantiates a LangChain-compatible ChatOllama instance.

    Defaults to settings.DEFAULT_MODEL targeting the configured local Ollama engine.
    """
    target_model = model or settings.DEFAULT_MODEL
    logger.debug("Initializing ChatOllama with model '%s' at %s", target_model, settings.OLLAMA_BASE_URL)

    return ChatOllama(
        base_url=settings.OLLAMA_BASE_URL,
        model=target_model,
        temperature=temperature,
    )


def get_embeddings(
    model: Optional[str] = "nomic-embed-text",
) -> OllamaEmbeddings:
    """Instantiates an OllamaEmbeddings instance for vectorizing document chunks.

    Defaults to 'nomic-embed-text' for fast local vector generation.
    """
    logger.debug("Initializing OllamaEmbeddings with model '%s'", model)

    return OllamaEmbeddings(
        base_url=settings.OLLAMA_BASE_URL,
        model=model,
    )