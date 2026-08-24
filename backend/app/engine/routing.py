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
plan itself has to change. Phase 3 adds `route_after_research` and the branches of
`route_after_plan` and `route_after_debug` that reach it. Phase 5 adds `route_after_eval`
and the two branches of the §5 tables that reach the Evaluator, closing the quality loop
(§6.2, back to `coder`) and the strategic loop (§6.3, back to `planner`).

**The quality loop needs a bound the Evaluator can actually count.** §6.2 says loop 2 shares
`max_debug_iterations`, but §3.5 gives `debug_iterations` to the Debugger alone, so a
`REFINE` cycle increments nothing. `route_after_eval` therefore counts `REFINE` verdicts out
of the history (`state.refine_cycles`) and spends the same budget from the other end. Without
that the loop would be bounded only by `max_node_visits`, which terminates but produces the
uninformative "budget exhausted" ending §6.4 exists to avoid.

`advance_step` — §5.4 rule 5, the deterministic node that sequences a plan with more than one
executable step — is the one branch of the §5 tables still unbuilt; where it would appear,
the comments below say so.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable

from app.engine.errors import consecutive_repeats
from app.engine.state import (
    AgentState,
    Budgets,
    EvalDecision,
    Plan,
    StepKind,
    next_pending_step,
    refine_cycles,
)

logger = logging.getLogger(__name__)

# The node every guard and every failure path routes to. `reporter` is `finalizer`'s sole
# predecessor, so routing here is what makes §6.4's corollary hold: every terminating path
# passes through the node that writes a report, including the cancelled and budget-spent
# ones.
TERMINAL_NODE = "reporter"

# Kinds that need the Coder and a container. `evaluate` and `report` steps are executed by
# the `evaluator` and `reporter` nodes instead (§5.1 rules 7–8) and are routed separately
# below; a standalone `research` step still has no node of its own to sequence past it —
# that needs `advance_step`. The Researcher runs *for* an implement/train step (§5.1
# rule 5), which needs no step advancement at all.
EXECUTABLE_KINDS = frozenset({StepKind.IMPLEMENT, StepKind.TRAIN})

# Consecutive failures sharing one fingerprint before the approach — not the code — is
# treated as the problem (§5.5 rule 4).
STAGNATION_WINDOW = 3

# How many research rounds `route_after_research` allows before giving up on a query and
# sending the Coder on with what it has (§5.2). Insufficient context is never fatal.
RESEARCH_MAX_ROUNDS = 2

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
    """Planner → researcher, coder, evaluator, or the terminal node (AGENTS.md §5.1).

    `plan is None` means the Planner exhausted its retries: `RETRY_THEN_REPORT` leaves the
    channel it owns unwritten, and this is the branch that reads it. The run still ends
    with a deliverable explaining that planning failed.
    """
    plan = state.get("plan")
    if plan is None:
        return TERMINAL_NODE

    step = next_pending_step(state)
    if step is None:
        # §5.1 rule 3: nothing pending, so the criteria contract is what is left to settle.
        return "evaluator"
    if step.kind is StepKind.EVALUATE:
        return "evaluator"  # rule 7
    if step.kind is StepKind.REPORT:
        return TERMINAL_NODE  # rule 8 — TERMINAL_NODE *is* the reporter
    if step.kind not in EXECUTABLE_KINDS:
        # A standalone `research` step (§5.1 rule 4). The Researcher has no way to mark it
        # done and move on without `advance_step`, so routing there would spin; the run is
        # evaluated on what it has instead of looping on a step nothing can retire.
        return "evaluator"
    if _context_is_thin(state):
        # §5.1 rule 5: an implement/train step with no sufficient context researches
        # first. `route_after_plan` is only reached once per step in this phase (there is
        # no `advance_step` yet to re-enter it), so `context_pack is None` is exactly "this
        # step has not yet triggered research" — nothing more to track.
        return "researcher"
    return "coder"


def _context_is_thin(state: AgentState) -> bool:
    pack = state.get("context_pack")
    return pack is None or pack.sufficiency == "insufficient"


@guarded
def route_after_research(state: AgentState) -> str:
    """Researcher → researcher (another round) or coder (AGENTS.md §5.2).

    There is no path from here to the terminal node other than the `@guarded` budget
    check: insufficient context is never fatal. Once `RESEARCH_MAX_ROUNDS` rounds have
    run, the Coder is told the context is thin and writes conservative code, which fails
    better than refusing to try.
    """
    pack = state.get("context_pack")
    history = state.get("context_history") or []
    if (
        pack is not None
        and pack.sufficiency == "insufficient"
        and len(history) < RESEARCH_MAX_ROUNDS
    ):
        return "researcher"
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

    # CLEAN. Rule 4: a TRAIN step's metrics are logged to MLflow before anything judges
    # them. `outcome.metrics is not None` is retained as a defensive assertion — a TRAIN
    # step with exit_code == 0 and no valid metrics.json is already classified
    # CONTRACT_VIOLATION above, so rule 3 catches it first in practice.
    plan: Plan | None = state.get("plan")
    if plan is None:
        # Unreachable in practice: the sandbox runs code, and code needs a plan to have
        # been written for. Terminal rather than `evaluator`, because a run with no plan
        # has no criteria contract and "no criteria" would evaluate as a vacuous pass.
        return TERMINAL_NODE

    step = plan.step(state.get("current_step_id"))
    if step is not None and step.kind is StepKind.TRAIN and outcome.metrics is not None:
        return "mlops"

    # Rule 5: another executable step remains, and `advance_step` is the node that would
    # retire the one just finished and move to it. It does not exist, so this ends at the
    # Reporter rather than handing the Evaluator a plan that is only half executed — a
    # verdict on criteria the *next* step was going to satisfy would refine or replan a run
    # that is merely unfinished.
    if _executable_work_remains(state):
        return TERMINAL_NODE

    # Rule 6: nothing executable left, so the criteria contract is what settles the run.
    return "evaluator"


def _executable_work_remains(state: AgentState) -> bool:
    """Whether a step the Coder and the sandbox could still run is pending.

    Pending `evaluate` and `report` steps do not count: they are performed by the
    `evaluator` and `reporter` nodes, which are exactly where this router is heading.
    """
    step = next_pending_step(state)
    return step is not None and step.kind in EXECUTABLE_KINDS


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

    # §5.5 rule 3: a diagnosis blocked on an unknown API researches before another
    # revision, bounded by the same `RESEARCH_MAX_ROUNDS` as an ordinary research round —
    # insufficient context is never fatal (§5.2), so exhausting it still falls through to
    # the Coder rather than the terminal node.
    if diagnosis is not None and diagnosis.requires_research:
        history = state.get("context_history") or []
        if len(history) < RESEARCH_MAX_ROUNDS:
            logger.info("Diagnosis requires research; routing to researcher")
            return "researcher"
        logger.info(
            "Diagnosis requires research but research rounds are exhausted; "
            "continuing to coder"
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


@guarded
def route_after_eval(state: AgentState) -> str:
    """Evaluator → reporter, coder, or planner (AGENTS.md §5.6).

    The two back edges here are loops 2 and 3. `REFINE` returns to the Coder with a
    quantitative directive and the same plan (§6.2); `REPLAN` returns to the Planner with
    the verdict history and an instruction to change the approach, not retry it (§6.3).
    Both are bounded before they are taken, and the bound is checked here rather than
    trusted to the node on the other end.

    A missing verdict routes terminal. The Evaluator writes one on every path it has —
    including its `DEGRADE_DETERMINISTIC` fallback, which is why that fallback computes a
    real verdict rather than returning an empty update — so this is defensive only, and
    guessing a decision would be worse than reporting what the run actually has.
    """
    verdict = state.get("verdict")
    if verdict is None:
        return TERMINAL_NODE

    if verdict.decision in {EvalDecision.ACCEPT, EvalDecision.ABORT}:
        return TERMINAL_NODE

    budgets: Budgets = state.get("budgets") or Budgets()

    if verdict.decision is EvalDecision.REFINE:
        # `verdicts` already contains the verdict being routed on, and it has not been
        # acted upon yet, so it does not count against the budget it is asking to spend.
        granted = max(refine_cycles(state.get("verdicts")) - 1, 0)
        spent = (state.get("debug_iterations") or 0) + granted
        if spent < budgets.max_debug_iterations:
            return "coder"
        logger.info(
            "Quality budget spent (%d debug iterations + refinements of %d); reporting "
            "the shortfall instead of refining again",
            spent,
            budgets.max_debug_iterations,
        )
        return TERMINAL_NODE

    replans = state.get("replan_count") or 0
    if replans < budgets.max_replans:
        return "planner"
    logger.info(
        "Replan budget spent (%d/%d); reporting the shortfall instead of replanning",
        replans,
        budgets.max_replans,
    )
    return TERMINAL_NODE
