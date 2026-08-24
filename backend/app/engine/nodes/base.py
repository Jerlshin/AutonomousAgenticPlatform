"""The common node envelope (AGENTS.md §7.0).

Every node in the graph is wrapped by `@node`, which owns the bookkeeping no node should
implement for itself: incrementing `usage.node_visits`, setting `phase` on entry, binding
log context, and applying the node's declared failure policy.

`usage.node_visits` matters more than it looks. It is the potential function in the
termination proof (§6.4): every node increments it by exactly one, every router refuses to
continue once it reaches `max_node_visits`, so the graph provably cannot cycle forever.
That guarantee is only as good as this decorator being the single place the counter moves.

**Failure policies are about the node itself raising**, as distinct from the agent's work
failing. A Coder that writes broken code has not failed — that is the debug loop's job. A
Coder whose model connection drops has.

Phase 6 added the event hook the paragraph above always promised: every `node.started`,
`node.completed`, `node.failed` and `node.retrying` frame in the WebSocket protocol
(§9.4) is emitted from this wrapper and from nowhere else, for the same reason
`node_visits` moves in exactly one place. A node body that emitted its own lifecycle
events could forget to emit one on the path where it failed, which is the path a UI most
needs them on.

The wrapper also publishes the ambient emitter for the duration of the node body, so the
call sites several frames below it — `structured.call_text` streaming tokens, the sandbox
driver's log pump emitting stdout lines — reach it without every function in between
growing a parameter. See `engine/events.py` for why that is a `contextvars` variable and
not an argument.

`run_steps` rows and OTel spans remain outstanding, and belong here when they land.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig

from app.core import metrics
from app.core.config import settings
from app.core.logging import bind_run_context, unbind_run_context
from app.engine.events import (
    RunEmitter,
    emitter_from_config,
    reset_current_node,
    reset_emitter,
    set_current_node,
    set_emitter,
)
from app.engine.state import AgentState, RunPhase, Usage
from app.schemas.events import EventType

logger = logging.getLogger(__name__)


class NodeFn(Protocol):
    """A graph node: reads `AgentState`, performs effects, returns a partial update.

    A Protocol rather than a `Callable[...]` alias because LangGraph matches nodes against
    protocols with *named* parameters (`state`, `config`), and a positional-only Callable
    does not satisfy those.
    """

    def __call__(
        self, state: AgentState, config: RunnableConfig
    ) -> Awaitable[dict[str, Any]]: ...


class FailurePolicy(StrEnum):
    """What happens when the node body raises (AGENTS.md §6.5)."""

    FAIL_RUN = "FAIL_RUN"
    RETRY_THEN_REPORT = "RETRY_THEN_REPORT"
    DEGRADE = "DEGRADE"
    DEGRADE_DETERMINISTIC = "DEGRADE_DETERMINISTIC"
    SYNTHESISE_FALLBACK = "SYNTHESISE_FALLBACK"
    BEST_EFFORT = "BEST_EFFORT"


# A node's last resort: given the state it was called with and the exception that beat it,
# produce the state update it would have produced. This is what `DEGRADE` and
# `SYNTHESISE_FALLBACK` actually mean — "the Debugger emits a minimal Diagnosis", "a
# deterministic template renders the report" — and putting it in the decorator rather than
# in a try/except inside each body is what makes the guarantee structural. A body that
# handles its own failure can forget to; a declared fallback cannot.
FallbackFn = Callable[[AgentState, Exception | None], dict[str, Any]]


def node(
    *,
    name: str,
    phase: RunPhase,
    policy: FailurePolicy,
    retries: int | None = None,
    fallback: FallbackFn | None = None,
) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node body with visit accounting, phase setting and its failure policy."""

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(
            state: AgentState, config: RunnableConfig | None = None
        ) -> dict[str, Any]:
            config = config or {}
            attempts = 1 + (settings.MAX_AGENT_RETRIES if retries is None else retries)
            if policy is not FailurePolicy.RETRY_THEN_REPORT:
                attempts = 1

            emitter = emitter_from_config(config)
            emitter_token = set_emitter(emitter)
            node_token = set_current_node(name)
            # §12.3: `node`, `agent` and `step_id` are bound once, here, so every log line
            # this node produces — including ones written several frames down in the
            # sandbox driver or the structured-output ladder — carries them without any
            # call site passing them. `run_id` and `worker_id` are bound by the job.
            bind_run_context(
                node=name,
                agent=name.replace("_", " ").title(),
                step_id=state.get("current_step_id"),
                run_id=state.get("run_id"),
            )
            started = time.monotonic()
            try:
                return await _run_attempts(
                    fn,
                    state,
                    config,
                    emitter,
                    started,
                    name=name,
                    phase=phase,
                    policy=policy,
                    attempts=attempts,
                    fallback=fallback,
                )
            finally:
                # Unwound in the reverse order they were set, and unconditionally: a node
                # that raises out of `FAIL_RUN` must not leave the next node inheriting
                # its name on every token it streams.
                unbind_run_context("node", "agent", "step_id")
                reset_current_node(node_token)
                reset_emitter(emitter_token)

        return wrapper

    return decorator


async def _run_attempts(
    fn: NodeFn,
    state: AgentState,
    config: RunnableConfig,
    emitter: RunEmitter | None,
    started: float,
    *,
    name: str,
    phase: RunPhase,
    policy: FailurePolicy,
    attempts: int,
    fallback: FallbackFn | None,
) -> dict[str, Any]:
    """The retry loop, lifted out so the wrapper is nothing but context management."""
    await _emit(
        emitter,
        EventType.NODE_STARTED,
        {
            "node": name,
            "agent": name.replace("_", " ").title(),
            "phase": phase.value,
            "model": (state.get("model_routing") or {}).get(name),
            "plan_step_id": state.get("current_step_id"),
        },
    )
    await _summarise(emitter, phase=phase.value, current_node=name)

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        logger.info(
            "node.started",
            extra={"node": name, "run_id": state.get("run_id"), "attempt": attempt},
        )
        try:
            update = await fn(state, config)
        except Exception as exc:  # noqa: BLE001 - the policy decides what happens
            last_exc = exc
            logger.warning(
                "node.failed",
                extra={
                    "node": name,
                    "run_id": state.get("run_id"),
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                exc_info=True,
            )
            await _emit(
                emitter,
                EventType.NODE_FAILED,
                {
                    "node": name,
                    "error": {
                        "kind": type(exc).__name__,
                        "message": str(exc),
                        "fingerprint": f"{type(exc).__name__}:{name}",
                    },
                    "will_retry": attempt < attempts,
                    "policy": policy.value,
                },
            )
            if policy is FailurePolicy.FAIL_RUN:
                # Infrastructural. Masking it would send the Debugger chasing a code
                # bug that does not exist. Timed here rather than in the caller because
                # this is the one exit from a node that never reaches the code below.
                metrics.observe_node(name, "failed", time.monotonic() - started)
                raise
            if attempt < attempts:
                await _emit(
                    emitter,
                    EventType.NODE_RETRYING,
                    {"node": name, "attempt": attempt + 1, "max_attempts": attempts},
                )
                continue
            break
        else:
            logger.info(
                "node.completed", extra={"node": name, "run_id": state.get("run_id")}
            )
            final = _finalise(update, phase)
            metrics.observe_node(name, "ok", time.monotonic() - started)
            await _emit_completed(emitter, name, started, update)
            return final

    update = _degrade(name, policy, last_exc, state)
    if fallback is not None:
        update = {**update, **_synthesise(name, fallback, state, last_exc)}
    final = _finalise(update, phase)
    # `degraded` is its own outcome rather than folded into `ok`: a node that fell back to
    # its deterministic path took a different amount of time doing it, and mixing the two
    # into one latency series hides exactly the regression the panel exists to show.
    metrics.observe_node(name, "degraded", time.monotonic() - started)
    # A degraded node still completed from the graph's point of view — control moves on —
    # so the UI gets a `node.completed` carrying the degradation rather than a timeline
    # entry that simply never ends.
    await _emit_completed(emitter, name, started, update, degraded=True)
    return final


async def _emit(
    emitter: RunEmitter | None, event: EventType, payload: dict[str, Any]
) -> None:
    """Emit if there is an emitter. Every event call site in this module goes through here."""
    if emitter is not None:
        await emitter.emit(event, payload)


async def _emit_completed(
    emitter: RunEmitter | None,
    name: str,
    started: float,
    update: dict[str, Any],
    *,
    degraded: bool = False,
) -> None:
    """`node.completed` with the timing and token spend the timeline pane renders.

    Read from the node's own `usage` delta rather than from accumulated state: the
    decorator sees the update before the reducer folds it in, which is the only moment
    the *incremental* cost of this one node is available.
    """
    if emitter is None:
        return
    reported: Usage = update.get("usage") or Usage()
    await emitter.emit(
        EventType.NODE_COMPLETED,
        {
            "node": name,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "tokens_in": reported.tokens_in,
            "tokens_out": reported.tokens_out,
            "llm_calls": reported.llm_calls,
            "degraded": degraded,
            "summary": _summary_of(name, update, degraded=degraded),
        },
    )


def _summary_of(name: str, update: dict[str, Any], *, degraded: bool) -> str:
    """A one-line account of what the node produced, for the timeline pane.

    Derived from the update rather than written by each node: a node's own summary string
    would be one more thing to keep in step with what the node actually did.
    """
    if degraded:
        return f"{name} degraded to its fallback"
    plan = update.get("plan")
    if plan is not None:
        return f"{len(plan.steps)}-step plan, {len(plan.success_criteria)} criteria"
    revision = update.get("current_revision")
    if revision is not None:
        return (
            f"revision {revision.revision}, {len(revision.content.splitlines())} lines"
        )
    outcome = update.get("last_outcome")
    if outcome is not None:
        return f"{outcome.classification} in {outcome.duration_ms} ms"
    verdict = update.get("verdict")
    if verdict is not None:
        return f"{verdict.decision.value}, score {verdict.score:.2f}"
    return ""


async def _summarise(emitter: RunEmitter | None, **values: Any) -> None:
    """Refresh `run:{id}:summary`, which is what a newly connected client sees first."""
    if emitter is not None:
        await emitter.summary(**values)


def _finalise(update: dict[str, Any], phase: RunPhase) -> dict[str, Any]:
    """Stamp the phase and fold this visit into the usage delta the node reported."""
    update = dict(update)
    update["phase"] = phase
    reported: Usage = update.get("usage") or Usage()
    update["usage"] = reported.model_copy(
        update={"node_visits": reported.node_visits + 1}
    )
    return update


def _degrade(
    name: str, policy: FailurePolicy, exc: Exception | None, state: AgentState
) -> dict[str, Any]:
    """The state update for a node that exhausted its policy without succeeding.

    Nothing is written to the channels the node owns, so they stay at their previous value
    — `None` for a first attempt. The routers read exactly that: `plan is None` and
    `current_revision is None` are the conditions that divert a failed run to a terminal
    deliverable rather than letting it proceed on missing state.
    """
    detail = f"{type(exc).__name__}: {exc}" if exc else "unknown failure"
    logger.error(
        "node.degraded", extra={"node": name, "policy": policy, "error": detail}
    )
    # `metadata` is last-write-wins, so the failure note is merged onto what is already
    # there rather than replacing it — the seed and the prompt versions live in the same
    # channel and are still needed by whatever runs next.
    return {"metadata": {**(state.get("metadata") or {}), f"{name}_failure": detail}}


def _synthesise(
    name: str, fallback: FallbackFn, state: AgentState, exc: Exception | None
) -> dict[str, Any]:
    """Run a node's declared fallback, absorbing a failure in the fallback itself.

    The Reporter's contract is that it cannot fail, and a fallback that raises would
    quietly turn that into a run with no deliverable. An empty update is still worse than
    a report, so the failure is logged loudly rather than swallowed silently.
    """
    try:
        return fallback(state, exc)
    except Exception as fallback_exc:  # noqa: BLE001 - the point is that nothing escapes
        logger.error(
            "node.fallback_failed",
            extra={
                "node": name,
                "error": f"{type(fallback_exc).__name__}: {fallback_exc}",
            },
            exc_info=True,
        )
        return {}


def get_chat_client(config: RunnableConfig, role: str) -> Any:
    """The chat client for `role`.

    Resolution order: an explicit client in the run config (which is how the test suite
    drives the whole graph with a scripted fake), then a factory, then the configured
    Ollama model for that role.
    """
    configurable = (config or {}).get("configurable") or {}
    clients = configurable.get("llm_clients") or {}
    if role in clients:
        return clients[role]
    if configurable.get("llm") is not None:
        return configurable["llm"]

    factory = configurable.get("llm_factory")
    if factory is not None:
        return factory(role)

    from app.engine.llm import get_chat_model

    return get_chat_model(role)


def get_sandbox(config: RunnableConfig) -> Any:
    """The sandbox driver for this run, injectable for tests."""
    configurable = (config or {}).get("configurable") or {}
    driver = configurable.get("sandbox_driver")
    if driver is not None:
        return driver

    from app.services.sandbox import get_sandbox_driver

    return get_sandbox_driver()


def get_vector_store(config: RunnableConfig) -> Any:
    """The Qdrant service for this run, injectable for tests.

    Resolution mirrors `get_chat_client` and `get_sandbox`: an explicit override in the
    run config, then the real default. Phase 3's whole retrieval and episodic-memory path
    — the Researcher's corpus search, the Debugger's `run_memory` lookup, the Reporter's
    `run_memory` write — resolves through this one function, so a test drives all three
    with a single fake store and the graph never needs a live Qdrant to run.
    """
    configurable = (config or {}).get("configurable") or {}
    store = configurable.get("vector_store")
    if store is not None:
        return store

    from app.services.vector_store import VectorStoreService

    return VectorStoreService()


def get_run_memory_searcher(config: RunnableConfig) -> Any:
    """The episodic-memory lookup callable for this run (AGENTS.md §7.5).

    `search(fingerprint=..., message=..., task_kind=...) -> list[str]`. An explicit
    `run_memory_search` override wins (what the Debugger's unit tests inject); otherwise
    the default queries `run_memory` through `get_vector_store` and reduces each hit to
    its `fix_summary`, which is the only field the Debugger's prompt actually needs.
    """
    configurable = (config or {}).get("configurable") or {}
    search = configurable.get("run_memory_search")
    if search is not None:
        return search

    store = get_vector_store(config)

    async def _default(*, fingerprint: str, message: str, task_kind: str) -> list[str]:
        hits = await store.search_run_memory(
            fingerprint=fingerprint, message=message, task_kind=task_kind
        )
        return [hit["fix_summary"] for hit in hits if hit.get("fix_summary")]

    return _default


def get_run_memory_writer(config: RunnableConfig) -> Any:
    """The episodic-memory write callable for this run (AGENTS.md §7.8).

    An explicit `run_memory_writer` override wins; otherwise the default writes through
    `get_vector_store`. Callers are responsible for only invoking this on `SUCCEEDED` runs
    — the writer itself does not know the outcome of the run it is being called from.
    """
    configurable = (config or {}).get("configurable") or {}
    writer = configurable.get("run_memory_writer")
    if writer is not None:
        return writer

    store = get_vector_store(config)

    async def _default(**kwargs: Any) -> str:
        return await store.write_run_memory(**kwargs)

    return _default


def get_mlflow_service(config: RunnableConfig) -> Any:
    """The `MLflowService` for this run, injectable for tests (`mlops`, MLOPS.md §4).

    Resolution mirrors `get_sandbox` and `get_vector_store`: an explicit override in the
    run config — what `tests.fakes.FakeMlflowClient` is wired in through — wins; otherwise
    the real service, whose own `client` property lazily imports `mlflow` on first use.
    """
    configurable = (config or {}).get("configurable") or {}
    service = configurable.get("mlflow_service")
    if service is not None:
        return service

    from app.services.mlflow_client import MLflowService

    return MLflowService()


def get_db_session_factory(config: RunnableConfig) -> Any:
    """A callable returning an async-context-manager DB session for this run.

    `mlops` is the first engine node to touch Postgres directly, so there is no existing
    convention beyond the FastAPI-side `get_db` dependency in `app.core.db`. This mirrors
    that module's `AsyncSessionLocal` — a callable session factory — rather than a bare
    session, so a fresh session is opened per call instead of one being held open for the
    life of the run. An explicit `db_session_factory` override in the run config is what a
    test injects instead of a real database.
    """
    configurable = (config or {}).get("configurable") or {}
    factory = configurable.get("db_session_factory")
    if factory is not None:
        return factory

    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal


def run_metadata(state: AgentState, key: str, default: Any = None) -> Any:
    return (state.get("metadata") or {}).get(key, default)
