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

Phases 2–6 extend this decorator with `run_steps` rows, OTel spans and WebSocket
`node.started`/`node.completed` events. It is deliberately the only place those hooks will
need to be added.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.engine.state import AgentState, RunPhase, Usage

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

            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                logger.info(
                    "node.started",
                    extra={
                        "node": name,
                        "run_id": state.get("run_id"),
                        "attempt": attempt,
                    },
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
                    if policy is FailurePolicy.FAIL_RUN:
                        # Infrastructural. Masking it would send the Debugger chasing a
                        # code bug that does not exist.
                        raise
                    if attempt < attempts:
                        continue
                    break
                else:
                    logger.info(
                        "node.completed",
                        extra={"node": name, "run_id": state.get("run_id")},
                    )
                    return _finalise(update, phase)

            update = _degrade(name, policy, last_exc, state)
            if fallback is not None:
                update = {**update, **_synthesise(name, fallback, state, last_exc)}
            return _finalise(update, phase)

        return wrapper

    return decorator


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


def run_metadata(state: AgentState, key: str, default: Any = None) -> Any:
    return (state.get("metadata") or {}).get(key, default)
