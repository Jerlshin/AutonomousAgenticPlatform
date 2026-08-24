"""Ollama client construction and per-role model routing (ARCHITECTURE.md §11.1).

Different agent roles have genuinely different requirements — instruction-following models
plan well and write poor code, code models write well and plan poorly — so the model,
temperature and context window are looked up per role rather than shared.

`langchain_ollama` is imported lazily. Nothing in the engine needs a live model to be
*importable*: the graph is assembled, the state schema is validated and the whole test
suite runs against a fake client. Paying an import-time dependency on the Ollama stack
would make all of that impossible in a stripped environment (defect D-004 also retires the
deprecated `langchain_community` imports this module used to carry).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleProfile:
    """Sampling and context settings for one agent role (AGENTS.md §7)."""

    temperature: float
    num_ctx: int


# Temperatures are deliberately low. The Planner has the only non-zero-ish value among the
# structured roles because plan diversity is useful on a replan; the Reporter is warmer
# because it writes prose for a human.
ROLE_PROFILES: dict[str, RoleProfile] = {
    "planner": RoleProfile(temperature=0.15, num_ctx=8192),
    "researcher": RoleProfile(temperature=0.0, num_ctx=8192),
    "coder": RoleProfile(temperature=0.0, num_ctx=16384),
    "debugger": RoleProfile(temperature=0.0, num_ctx=16384),
    "evaluator": RoleProfile(temperature=0.0, num_ctx=8192),
    "reporter": RoleProfile(temperature=0.35, num_ctx=16384),
}

_DEFAULT_PROFILE = RoleProfile(temperature=0.2, num_ctx=8192)

_INSTALL_HINT = (
    "langchain-ollama is not installed. Install it to run the graph against a live model: "
    "pip install langchain-ollama"
)


def get_chat_model(
    role: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    num_ctx: int | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """The chat client routed to `role`, honouring the per-role model and sampling.

    Raises `RuntimeError` with an actionable message when the Ollama integration is not
    installed — a node that cannot reach a model should say so plainly rather than fail
    with an ImportError from three frames down.
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(_INSTALL_HINT) from exc

    profile = ROLE_PROFILES.get(role, _DEFAULT_PROFILE)
    target_model = model or settings.model_for_role(role)
    logger.debug(
        "Routing role '%s' to model '%s' at %s",
        role,
        target_model,
        settings.OLLAMA_BASE_URL,
    )

    return ChatOllama(
        base_url=settings.OLLAMA_BASE_URL,
        model=target_model,
        temperature=profile.temperature if temperature is None else temperature,
        num_ctx=profile.num_ctx if num_ctx is None else num_ctx,
        keep_alive=settings.OLLAMA_KEEP_ALIVE,
        client_kwargs={"timeout": settings.OLLAMA_REQUEST_TIMEOUT_S},
        **kwargs,
    )


def get_llm(model: str | None = None, temperature: float = 0.2) -> BaseChatModel:
    """Backwards-compatible constructor for callers that do not name a role."""
    return get_chat_model("default", model=model, temperature=temperature)


def get_embeddings(model: str | None = None) -> Any:
    """Embedding client for the vector store."""
    try:
        from langchain_ollama import OllamaEmbeddings
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(_INSTALL_HINT) from exc

    return OllamaEmbeddings(
        base_url=settings.OLLAMA_BASE_URL,
        model=model or settings.EMBEDDING_MODEL,
    )


def model_routing_snapshot() -> dict[str, str]:
    """The role → model map, recorded in state so a run's routing is auditable later."""
    return {role: settings.model_for_role(role) for role in ROLE_PROFILES}
