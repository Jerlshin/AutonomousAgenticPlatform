"""`researcher` — hybrid retrieval over the local corpus, extractive and cited.

Specification: AGENTS.md §7.2, ARCHITECTURE.md §7.3. Two LLM calls, not one, because the
step description a Coder needs answered is a poor retrieval query on its own —
"GridSearchCV scoring parameter accepted values" finds something useful, "how do I do
cross-validation" does not. Retrieval itself is deterministic: the node calls Qdrant
directly between the two calls, so no model ever decides *whether* to search, only *what*
to search for (design principle P1).

The extraction call is the point of this node. An 8B model asked to summarise "how to use
GridSearchCV" produces plausible, subtly wrong signatures — the leading cause of Coder
failure this collection exists to remove. So every `api_signatures` entry the model
returns is checked against the retrieved chunks after the call: an entry that is not a
verbatim substring of something actually retrieved is dropped, not trusted. The Coder
never sees a signature the corpus did not really contain.

Failure policy `DEGRADE` (§6.5): an unreachable model or vector store yields an empty
`ContextPack` with `sufficiency="insufficient"`. Insufficient context is never fatal for
the run — `route_after_research` sends it on to the Coder anyway, which is told to stay on
APIs it already knows.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.engine.nodes.base import FailurePolicy, get_chat_client, get_vector_store, node
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.state import (
    AgentState,
    ContextPack,
    Diagnosis,
    Plan,
    PlanStep,
    RetrievedChunk,
    RunPhase,
    StepKind,
    Usage,
)
from app.engine.structured import call_structured

logger = logging.getLogger(__name__)

RD_CORPUS_LIMIT = 8
CODE_EXEMPLAR_LIMIT = 4
FINAL_TOP_K = 6
# A cap-per-source stand-in for the MMR diversity pass in ARCHITECTURE.md §7.3.4. True MMR
# needs pairwise chunk-embedding similarity, which costs a second embedding round trip
# purely to pick six chunks; capping how many chunks one source document may contribute
# captures the same practical goal — six chunks from one document teach the Coder nothing
# five did not — at zero extra cost.
MAX_CHUNKS_PER_SOURCE = 2


class _QueryPlan(BaseModel):
    """Phase 1 output: the sub-queries actually issued against the corpus."""

    queries: list[str] = Field(min_length=1, max_length=4)


class _Extraction(BaseModel):
    """Phase 2 output, before `api_signatures` and `citations` are verified against
    what was actually retrieved (`_verify_signatures`, `_verify_citations`)."""

    key_facts: list[str] = Field(default_factory=list)
    api_signatures: list[str] = Field(default_factory=list)
    citations: dict[str, list[str]] = Field(default_factory=dict)
    sufficiency: Literal["sufficient", "partial", "insufficient"] = "insufficient"
    gaps: list[str] = Field(default_factory=list)


def degraded_context_pack(
    state: AgentState, exc: Exception | None = None
) -> dict[str, Any]:
    """The `DEGRADE` fallback: an honestly empty pack, never an invented one."""
    if exc is not None:
        logger.warning("Researcher degrading to an empty ContextPack: %s", exc)
    pack = ContextPack(
        sufficiency="insufficient",
        gaps=["retrieval was unavailable for this round"],
    )
    return {"context_pack": pack, "context_history": pack, "usage": Usage()}


@node(
    name="researcher",
    phase=RunPhase.RESEARCH,
    policy=FailurePolicy.DEGRADE,
    fallback=degraded_context_pack,
)
async def researcher_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    plan: Plan | None = state.get("plan")
    step = plan.step(state.get("current_step_id")) if plan else None
    diagnosis: Diagnosis | None = state.get("last_diagnosis")
    from_debug = diagnosis is not None and diagnosis.requires_research

    llm = get_chat_client(config, "researcher")
    store = get_vector_store(config)
    usage = Usage()

    topic_block = _topic_block(step, plan, diagnosis, from_debug)
    query_plan, usage = await _plan_queries(llm, topic_block, state, usage)

    task_kind = (plan.task_kind if plan else state.get("task_kind")) or ""
    include_exemplars = step is not None and step.kind in (
        StepKind.IMPLEMENT,
        StepKind.TRAIN,
    )
    chunks = await _retrieve(
        store, query_plan, task_kind=task_kind, include_exemplars=include_exemplars
    )

    extraction, usage = await _extract(llm, topic_block, chunks, usage)
    verified_signatures, dropped = _verify_signatures(extraction.api_signatures, chunks)
    citations = _verify_citations(extraction.citations, chunks)

    sufficiency = extraction.sufficiency
    gaps = list(extraction.gaps)
    if dropped:
        logger.warning(
            "Researcher dropped %d unverified signature(s) for step %s: %s",
            len(dropped),
            step.id if step else "?",
            dropped,
        )
        gaps = gaps + [
            f"could not verify verbatim in the corpus: {sig}" for sig in dropped
        ]
        if sufficiency == "sufficient":
            sufficiency = "partial"
    if not chunks and sufficiency != "insufficient":
        # No model, however careful, gets to declare sufficiency over zero evidence.
        sufficiency = "insufficient"

    pack = ContextPack(
        query_plan=query_plan,
        chunks=chunks,
        key_facts=extraction.key_facts,
        api_signatures=verified_signatures,
        citations=citations,
        sufficiency=sufficiency,
        gaps=gaps,
    )

    logger.info(
        "Research round for step %s: %d chunks, %d/%d signatures verified, sufficiency=%s",
        step.id if step else "?",
        len(chunks),
        len(verified_signatures),
        len(verified_signatures) + len(dropped),
        sufficiency,
    )

    return {
        "context_pack": pack,
        "context_history": pack,
        "usage": usage,
        "messages": [
            AIMessage(
                content=(
                    f"Research round: {sufficiency}, {len(chunks)} chunks, "
                    f"{len(verified_signatures)} verified signatures."
                )
            )
        ],
        "metadata": {
            **(state.get("metadata") or {}),
            "prompt_version_researcher_query": load_prompt("researcher_query").version,
            "prompt_version_researcher_extract": load_prompt(
                "researcher_extract"
            ).version,
        },
    }


async def _plan_queries(
    llm: Any, topic_block: str, state: AgentState, usage: Usage
) -> tuple[list[str], Usage]:
    prompt = load_prompt("researcher_query")
    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        topic_block=topic_block,
        prior_gaps_block=_prior_gaps_block(state),
    )
    user = "Produce the retrieval queries now, as a single JSON object matching the schema."
    result = await call_structured(
        llm, output_model=_QueryPlan, system=system, user=user
    )
    queries = [q.strip() for q in result.value.queries if q.strip()]
    return (queries or [_fallback_query(topic_block)]), _accumulate(usage, result.usage)


async def _extract(
    llm: Any, topic_block: str, chunks: list[RetrievedChunk], usage: Usage
) -> tuple[_Extraction, Usage]:
    prompt = load_prompt("researcher_extract")
    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        topic_block=topic_block,
        chunks_block=_chunks_block(chunks),
    )
    user = (
        "Extract now, as a single JSON object matching the schema. Do not invent a "
        "signature that is not in the retrieved text above."
    )
    result = await call_structured(
        llm, output_model=_Extraction, system=system, user=user
    )
    return result.value, _accumulate(usage, result.usage)


async def _retrieve(
    store: Any, queries: list[str], *, task_kind: str, include_exemplars: bool
) -> list[RetrievedChunk]:
    """The §7.2 retrieval procedure: rd_corpus for every query, code_exemplars for
    implement/train steps, deduped and diversified into a final top-6."""
    pool: list[RetrievedChunk] = []
    for query in queries:
        try:
            hits = await store.search_rd_corpus(query, limit=RD_CORPUS_LIMIT)
        except Exception as exc:  # noqa: BLE001 - a retrieval failure degrades, never crashes
            logger.warning("rd_corpus search failed for %r: %s", query, exc)
            hits = []
        pool += [RetrievedChunk(**hit) for hit in hits]

        if include_exemplars:
            try:
                exemplar_hits = await store.search_code_exemplars(
                    query, task_kind=task_kind, limit=CODE_EXEMPLAR_LIMIT
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("code_exemplars search failed for %r: %s", query, exc)
                exemplar_hits = []
            pool += [RetrievedChunk(**hit) for hit in exemplar_hits]

    return _diversify(_dedupe(pool))


def _dedupe(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Highest-scoring copy of a point kept, when several queries retrieve it again."""
    seen: set[tuple[str, str]] = set()
    unique: list[RetrievedChunk] = []
    for chunk in sorted(chunks, key=lambda c: c.score, reverse=True):
        key = (chunk.collection, chunk.point_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(chunk)
    return unique


def _diversify(
    chunks: list[RetrievedChunk], k: int = FINAL_TOP_K
) -> list[RetrievedChunk]:
    per_source: dict[str, int] = {}
    picked: list[RetrievedChunk] = []
    for chunk in chunks:  # already sorted by score, highest first
        count = per_source.get(chunk.source_uri, 0)
        if count >= MAX_CHUNKS_PER_SOURCE:
            continue
        per_source[chunk.source_uri] = count + 1
        picked.append(chunk)
        if len(picked) >= k:
            break
    return picked


def _verify_signatures(
    signatures: list[str], chunks: list[RetrievedChunk]
) -> tuple[list[str], list[str]]:
    """Keep only signatures that are a verbatim substring of something retrieved."""
    haystack = "\n".join(chunk.text for chunk in chunks)
    verified: list[str] = []
    dropped: list[str] = []
    for signature in signatures:
        clean = signature.strip()
        if not clean:
            continue
        if clean in haystack:
            verified.append(clean)
        else:
            dropped.append(clean)
    return verified, dropped


def _verify_citations(
    citations: dict[str, list[str]], chunks: list[RetrievedChunk]
) -> dict[str, list[str]]:
    """Drop citations pointing at a `point_id` that was not actually retrieved."""
    known_ids = {chunk.point_id for chunk in chunks}
    verified: dict[str, list[str]] = {}
    for index, point_ids in citations.items():
        kept = [pid for pid in point_ids if pid in known_ids]
        if kept:
            verified[index] = kept
    return verified


def _topic_block(
    step: PlanStep | None,
    plan: Plan | None,
    diagnosis: Diagnosis | None,
    from_debug: bool,
) -> str:
    if from_debug and diagnosis is not None:
        lines = [
            "### What needs researching",
            "",
            "The Debugger could not diagnose the last failure without knowing an API's "
            "real signature.",
            "",
            f"Root cause so far: {diagnosis.root_cause}",
            f"Fix strategy: {diagnosis.fix_strategy}",
        ]
        return "\n".join(lines)
    if step is None:
        return (
            "### What needs researching\n\n"
            "No plan step is bound to this round; research general best practice for the "
            "task at hand."
        )
    lines = [
        "### What needs researching",
        "",
        f"Step **{step.title}** (`{step.kind.value}`): {step.description}",
    ]
    if plan is not None:
        lines.append(f"\nTask kind `{plan.task_kind}`.")
    return "\n".join(lines)


def _prior_gaps_block(state: AgentState) -> str:
    """What the previous round could not find, so a re-query targets it (§5.2)."""
    history = state.get("context_history") or []
    if not history:
        return ""
    gaps = history[-1].gaps
    if not gaps:
        return ""
    body = "\n".join(f"- {gap}" for gap in gaps)
    return "### Gaps from the previous round — target these\n\n" + body


def _chunks_block(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return wrap_untrusted(
            "qdrant", "(no chunks were retrieved for this query plan)"
        )
    parts = [
        f"[{chunk.point_id}] {chunk.source_uri}"
        + (f" § {chunk.section}" if chunk.section else "")
        + f"\n{chunk.text}"
        for chunk in chunks
    ]
    return wrap_untrusted("qdrant", "\n\n---\n\n".join(parts))


def _fallback_query(topic_block: str) -> str:
    """A query plan that came back empty still has to search for something."""
    return " ".join(topic_block.split())[:200]


def _accumulate(current: Usage, new: Usage) -> Usage:
    return Usage(
        tokens_in=current.tokens_in + new.tokens_in,
        tokens_out=current.tokens_out + new.tokens_out,
        llm_calls=current.llm_calls + new.llm_calls,
    )


__all__ = ["researcher_node", "degraded_context_pack"]
