"""LangChain tool bindings over the Qdrant collections (AGENTS.md §8).

These are not wired into a model tool-calling loop anywhere in the graph today —
retrieval in this codebase is deterministic-first (design principle P1): the Researcher
calls `VectorStoreService` directly between two structured LLM calls
(`engine/nodes/researcher.py`), and the Debugger's episodic lookup runs before its LLM
call, never as a tool the model may decline to use (§7.5). These bindings exist for the
uses AGENTS.md §8 documents that *are* model-invoked — the Planner's "metadata-only"
knowledge-base search, and manual/agent-registry invocation via `/agents/{name}/invoke` —
and so that a future tool-calling node has a correct, tested binding to reach for instead
of writing its own.

Tool error convention (§8): a tool never raises into the model. It returns a string the
model can read describing the failure, so a retrieval outage produces a recoverable turn
rather than an exception that kills the node.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.services.vector_store import VectorStoreService

logger = logging.getLogger(__name__)


def _format_hits(hits: list[dict], *, empty: str) -> str:
    if not hits:
        return empty
    parts = []
    for index, hit in enumerate(hits, 1):
        score = round(hit.get("score", 0.0), 3)
        source = hit.get("source_uri") or hit.get("title") or "unknown source"
        text = (hit.get("text") or "").strip()
        parts.append(
            f"--- Result {index} [{hit.get('point_id', '?')}] {source} (score {score}) ---\n{text}"
        )
    return "\n\n".join(parts)


@tool
async def search_knowledge_base(query: str) -> str:
    """Hybrid search over `rd_corpus` — reference documentation and API knowledge.

    Args:
        query: A specific retrieval query naming the API, parameter, or technique needed —
            not a restatement of the whole task.

    Returns:
        Ranked chunks with their point id, source, and text, or a message that nothing
        relevant was found.
    """
    logger.info("search_knowledge_base('%s')", query)
    try:
        hits = await VectorStoreService().search_rd_corpus(query)
        return _format_hits(hits, empty="No relevant context found in rd_corpus.")
    except Exception as exc:  # noqa: BLE001 - tools never raise into the model
        logger.error("search_knowledge_base failed: %s", exc)
        return f"Retrieval failed: {exc}"


@tool
async def search_code_exemplars(query: str, task_kind: str = "") -> str:
    """Hybrid search over `code_exemplars` — verified, sandbox-tested code snippets.

    Args:
        query: A specific retrieval query naming the API or pattern needed.
        task_kind: Narrows results to snippets tagged with this task kind, when known.

    Returns:
        Ranked, `tested == true` snippets, or a message that nothing relevant was found.
    """
    logger.info("search_code_exemplars('%s', task_kind=%r)", query, task_kind)
    try:
        hits = await VectorStoreService().search_code_exemplars(
            query, task_kind=task_kind
        )
        return _format_hits(hits, empty="No verified code exemplars found.")
    except Exception as exc:  # noqa: BLE001
        logger.error("search_code_exemplars failed: %s", exc)
        return f"Retrieval failed: {exc}"


@tool
async def search_run_memory(error_fingerprint: str, task_kind: str = "") -> str:
    """Look up prior successful fixes for a specific error fingerprint.

    Args:
        error_fingerprint: The `{ExceptionType}:{slug}` fingerprint of the current error.
        task_kind: Narrows results to fixes recorded on runs of this task kind.

    Returns:
        Prior fix summaries from `run_memory`, or a message that none were found.
    """
    logger.info("search_run_memory('%s', task_kind=%r)", error_fingerprint, task_kind)
    try:
        hits = await VectorStoreService().search_run_memory(
            fingerprint=error_fingerprint, task_kind=task_kind
        )
        if not hits:
            return "No prior fix was recorded for this error fingerprint."
        return "\n\n".join(
            f"--- Prior fix (run {hit.get('run_id', '?')}) ---\n{hit.get('fix_summary', '')}"
            for hit in hits
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("search_run_memory failed: %s", exc)
        return f"Retrieval failed: {exc}"


@tool
async def write_run_memory(
    run_id: str,
    task_kind: str,
    error_fingerprint: str,
    error_excerpt: str,
    fix_summary: str,
    fix_diff: str = "",
) -> str:
    """Record a successful error→fix pair into episodic memory.

    Args:
        run_id: The run this fix came from.
        task_kind: The task kind of that run.
        error_fingerprint: The fingerprint of the error that was fixed.
        error_excerpt: A short excerpt of the original error message.
        fix_summary: A one-line description of what fixed it.
        fix_diff: A unified diff of the change, if available.

    Returns:
        The written point id, or a message that the write failed.
    """
    logger.info("write_run_memory(%s, %s)", run_id, error_fingerprint)
    try:
        point_id = await VectorStoreService().write_run_memory(
            run_id=run_id,
            task_kind=task_kind,
            outcome="SUCCEEDED",
            error_fingerprint=error_fingerprint,
            error_excerpt=error_excerpt,
            fix_summary=fix_summary,
            fix_diff=fix_diff,
        )
        return f"Recorded as {point_id}."
    except Exception as exc:  # noqa: BLE001
        logger.error("write_run_memory failed: %s", exc)
        return f"Write failed: {exc}"


__all__ = [
    "search_code_exemplars",
    "search_knowledge_base",
    "search_run_memory",
    "write_run_memory",
]
