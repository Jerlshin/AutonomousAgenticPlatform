"""Routing predicates (AGENTS.md §5).

Every router is a pure function of state: it reads structured fields — an enum, a counter,
a `None` — and never model prose. An LLM proposes inside a validated schema; the graph
disposes by reading that field. This is design principle P1, and it is what makes the
control flow reproducible and testable without a model.

Every router is wrapped in `@guarded`, which honours cancellation and global budget
exhaustion before the router's own logic runs. That guard is also step 4 of the
termination proof (§6.4): when `node_visits` reaches `max_node_visits`, every routing
decision becomes the terminal node, so no cycle can outlive the budget.

Phase 2 adds the correctness cycle — `sandbox_exec → debugger → coder` — and with it the
two decisions that actually bound it: `route_after_exec` spends the iteration budget and
`route_after_debug` decides whether another code revision can plausibly help or whether the
plan itself has to change. The `researcher`, `mlops`, `evaluator` and `advance_step`
branches of the §5 tables arrive with the nodes they route to.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable

from app.engine.errors import consecutive_repeats
from app.engine.state import AgentState, Budgets, StepKind, next_pending_step

logger = logging.getLogger(__name__)

# The node every guard and every failure path routes to. `reporter` is `finalizer`'s sole
# predecessor, so routing here is what makes §6.4's corollary hold: every terminating path
# passes through the node that writes a report, including the cancelled and budget-spent
# ones.
TERMINAL_NODE = "reporter"

# Kinds the graph can execute today. A plan may legitimately contain `research`, `evaluate`
# and `report` steps; there is simply no node to run them yet.
EXECUTABLE_KINDS = frozenset({StepKind.IMPLEMENT, StepKind.TRAIN})

# Consecutive failures sharing one fingerprint before the approach — not the code — is
# treated as the problem (§5.5 rule 4).
STAGNATION_WINDOW = 3

Router = Callable[[AgentState], str]


def guarded(router: Router) -> Router:
    """Honour cancellation and the global budget before consulting the router."""

    @functools.wraps(router)
    def wrapper(state: AgentState) -> str:
        if state.get("cancel_requested"):
            logger.info("Cancellation requested; routing to %s", TERMINAL_NODE)
            return TERMINAL_NODE

        if state.get("usage") is not None and state.get("budgets") is not None:
            exhausted = _exhausted_budget(state)
            if exhausted is not None:
                logger.warning(
                    "Budget exhausted (%s); routing to %s", exhausted, TERMINAL_NODE
                )
                return TERMINAL_NODE

        return router(state)

    return wrapper


def _exhausted_budget(state: AgentState) -> str | None:
    """Which global budget, if any, has been spent."""
    usage = state["usage"]
    budgets = state["budgets"]
    if usage.node_visits >= budgets.max_node_visits:
        return f"node_visits {usage.node_visits} >= {budgets.max_node_visits}"
    if usage.tokens_total >= budgets.max_tokens:
        return f"tokens {usage.tokens_total} >= {budgets.max_tokens}"
    if usage.elapsed_seconds >= budgets.wallclock_seconds:
        return f"wallclock {usage.elapsed_seconds:.0f}s >= {budgets.wallclock_seconds}s"
    return None


@guarded
def route_after_plan(state: AgentState) -> str:
    """Planner → coder, or straight to the terminal node when there is nothing to run.

    `plan is None` means the Planner exhausted its retries: `RETRY_THEN_REPORT` leaves the
    channel it owns unwritten, and this is the branch that reads it. The run still ends
    with a deliverable explaining that planning failed.
    """
    plan = state.get("plan")
    if plan is None:
        return TERMINAL_NODE

    step = next_pending_step(state)
    if step is None or step.kind not in EXECUTABLE_KINDS:
        # Nothing executable left. Phase 5's evaluator becomes the target here.
        return TERMINAL_NODE
    return "coder"


@guarded
def route_after_code(state: AgentState) -> str:
    """Coder → sandbox, unless the Coder produced nothing runnable."""
    if state.get("current_revision") is None:
        return TERMINAL_NODE
    return "sandbox_exec"


@guarded
def route_after_exec(state: AgentState) -> str:
    """Sandbox → debugger, or terminal (AGENTS.md §5.4).

    The most important router in the graph, and the one that reads the least: a
    `classification` computed by `sandbox_exec` from `exit_code`, `OOMKilled` and
    `timed_out`, plus two counters. No model output reaches this decision, which is what
    makes a failing run fail the same way twice.

    The two budget checks come before the diversion to the Debugger rather than after it.
    Hitting `max_debug_iterations` at visit 18 produces a report that says "could not fix a
    persistent ValueError after 4 attempts"; letting the loop run on until
    `max_node_visits` produces one that says "budget exhausted", which tells the reader
    nothing (§6.4).
    """
    outcome = state.get("last_outcome")
    if outcome is None:
        # sandbox_exec raises rather than returning nothing, so this is unreachable in
        # practice; routing to the terminal node keeps it harmless if that ever changes.
        return TERMINAL_NODE

    if outcome.classification != "CLEAN":
        budgets: Budgets = state.get("budgets") or Budgets()
        iterations = state.get("debug_iterations") or 0
        if iterations >= budgets.max_debug_iterations:
            logger.info(
                "Debug budget spent (%d/%d); routing to %s",
                iterations,
                budgets.max_debug_iterations,
                TERMINAL_NODE,
            )
            return TERMINAL_NODE

        usage = state.get("usage")
        if usage is not None and (
            usage.sandbox_executions >= budgets.max_sandbox_executions
        ):
            logger.info(
                "Sandbox budget spent (%d/%d); routing to %s",
                usage.sandbox_executions,
                budgets.max_sandbox_executions,
                TERMINAL_NODE,
            )
            return TERMINAL_NODE

        return "debugger"

    # CLEAN. §5.4 rules 4–6 fan out to `mlops`, `advance_step` and `evaluator`; none of
    # those nodes exists yet, so a clean execution is the end of the run's work and the
    # Reporter writes it up.
    return TERMINAL_NODE


@guarded
def route_after_debug(state: AgentState) -> str:
    """Debugger → coder, planner, or terminal (AGENTS.md §5.5).

    Rule 4 — three consecutive failures with one fingerprint escalate to the Planner — is
    the anti-thrash guard, and it is the reason `errors` accumulates instead of being
    overwritten. Without it the single most common multi-agent failure mode is a
    Coder–Debugger pair producing cosmetically different code that fails identically until
    the iteration budget expires. Three identical fingerprints is decisive evidence that
    the *approach* is wrong rather than the code, so the graph escalates a level instead of
    spending what is left proving the same thing again.
    """
    budgets: Budgets = state.get("budgets") or Budgets()
    iterations = state.get("debug_iterations") or 0
    if iterations > budgets.max_debug_iterations:
        return TERMINAL_NODE

    replans = state.get("replan_count") or 0
    diagnosis = state.get("last_diagnosis")
    can_replan = replans < budgets.max_replans

    if diagnosis is not None and diagnosis.requires_replan and can_replan:
        logger.info("Diagnosis requires a replan; routing to planner")
        return "planner"

    # §5.5 rule 3 sends `requires_research` to the Researcher. There is no Researcher yet,
    # and insufficient context is never fatal (§5.2) — the Coder is told the context is
    # thin and writes conservative code, which fails better than refusing to try.
    if diagnosis is not None and diagnosis.requires_research:
        logger.info(
            "Diagnosis requests research; no researcher yet, continuing to coder"
        )

    repeats = consecutive_repeats(state.get("errors"))
    if repeats >= STAGNATION_WINDOW and can_replan:
        logger.warning(
            "Stagnation: %d consecutive failures with fingerprint %s; escalating to a "
            "replan",
            repeats,
            (state.get("errors") or [])[-1].fingerprint,
        )
        return "planner"

    return "coder"
