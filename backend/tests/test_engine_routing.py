"""Routing predicates (AGENTS.md §5, §12).

Routers are the graph's control flow, so this file aims at every branch. A router that is
wrong in one condition does not fail loudly — it silently sends a run somewhere plausible,
which is the hardest class of bug to notice in a system that is supposed to be autonomous.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.engine.routing import (
    TERMINAL_NODE,
    route_after_code,
    route_after_debug,
    route_after_exec,
    route_after_plan,
)
from app.engine.state import (
    Budgets,
    CodeRevision,
    Diagnosis,
    ErrorKind,
    ErrorRecord,
    Plan,
    PlanStep,
    SandboxOutcome,
    StepKind,
    StepStatus,
    SuccessCriterion,
    Usage,
    ValidationReport,
)


def plan_with(kind: StepKind) -> Plan:
    return Plan(
        steps=[PlanStep(id="s1", index=0, title="t", description="d", kind=kind)],
        success_criteria=[
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=0.9
            )
        ],
        task_kind="tabular-classification",
        primary_metric="accuracy",
    )


def state(**overrides) -> dict:
    base = {
        "usage": Usage(node_visits=1, started_at=datetime.now(UTC)),
        "budgets": Budgets(),
        "cancel_requested": False,
    }
    base.update(overrides)
    return base


class TestGuards:
    def test_cancellation_short_circuits_every_router(self):
        cancelled = state(cancel_requested=True, plan=plan_with(StepKind.TRAIN))
        assert route_after_plan(cancelled) == TERMINAL_NODE
        assert (
            route_after_code({**cancelled, "current_revision": _revision()})
            == TERMINAL_NODE
        )

    def test_node_visit_exhaustion_routes_to_the_terminal_node(self):
        """Step 4 of the termination proof — Φ has reached zero."""
        exhausted = state(
            plan=plan_with(StepKind.TRAIN),
            usage=Usage(node_visits=60),
            budgets=Budgets(max_node_visits=60),
        )
        assert route_after_plan(exhausted) == TERMINAL_NODE

    def test_token_exhaustion_routes_to_the_terminal_node(self):
        exhausted = state(
            plan=plan_with(StepKind.TRAIN),
            usage=Usage(tokens_in=200_000, tokens_out=50_000),
            budgets=Budgets(max_tokens=250_000),
        )
        assert route_after_plan(exhausted) == TERMINAL_NODE

    def test_wallclock_exhaustion_routes_to_the_terminal_node(self):
        exhausted = state(
            plan=plan_with(StepKind.TRAIN),
            usage=Usage(started_at=datetime.now(UTC) - timedelta(seconds=100)),
            budgets=Budgets(wallclock_seconds=60),
        )
        assert route_after_plan(exhausted) == TERMINAL_NODE

    def test_a_state_without_accounting_yet_is_not_treated_as_exhausted(self):
        """`init` has not run; missing counters must not look like a spent budget."""
        bare = {"plan": plan_with(StepKind.TRAIN)}
        assert route_after_plan(bare) == "coder"

    def test_a_budget_with_room_left_does_not_divert(self):
        assert route_after_plan(state(plan=plan_with(StepKind.TRAIN))) == "coder"


class TestRouteAfterPlan:
    def test_no_plan_means_planning_failed(self):
        assert route_after_plan(state(plan=None)) == TERMINAL_NODE

    @pytest.mark.parametrize("kind", [StepKind.IMPLEMENT, StepKind.TRAIN])
    def test_executable_steps_go_to_the_coder(self, kind):
        assert route_after_plan(state(plan=plan_with(kind))) == "coder"

    @pytest.mark.parametrize(
        "kind", [StepKind.RESEARCH, StepKind.EVALUATE, StepKind.REPORT]
    )
    def test_steps_this_phase_cannot_run_terminate_the_run(self, kind):
        """Phase 3 and 5 replace these branches with `researcher` and `evaluator`."""
        assert route_after_plan(state(plan=plan_with(kind))) == TERMINAL_NODE

    def test_a_plan_with_nothing_pending_terminates(self):
        plan = plan_with(StepKind.TRAIN)
        done = state(plan=plan, step_status={"s1": StepStatus.SUCCEEDED})
        assert route_after_plan(done) == TERMINAL_NODE


class TestRouteAfterCode:
    def test_a_revision_goes_to_the_sandbox(self):
        assert route_after_code(state(current_revision=_revision())) == "sandbox_exec"

    def test_no_revision_means_the_coder_exhausted_its_repairs(self):
        assert route_after_code(state(current_revision=None)) == TERMINAL_NODE


def _revision() -> CodeRevision:
    return CodeRevision(revision=1, content="print(1)\n", sha256="0" * 64)


def outcome(classification: str = "CLEAN") -> SandboxOutcome:
    return SandboxOutcome(
        execution_id=uuid.uuid4(),
        profile="train",
        classification=classification,
        exit_code=0 if classification == "CLEAN" else 1,
        duration_ms=10,
        validation=ValidationReport(passed=True),
        revision=1,
    )


def error(fingerprint: str = "KeyError:target", revision: int = 1) -> ErrorRecord:
    return ErrorRecord(
        kind=ErrorKind.DATA,
        fingerprint=fingerprint,
        message="'target'",
        revision=revision,
    )


def diagnosis(**overrides) -> Diagnosis:
    base = {
        "error_fingerprint": "KeyError:target",
        "root_cause": "wrong column name",
        "fix_strategy": "read the right one",
        "targeted_changes": ["rename the column"],
        "confidence": 0.8,
    }
    return Diagnosis(**{**base, **overrides})


class TestRouteAfterExec:
    def test_a_clean_execution_ends_the_run(self):
        """Phase 3 and 5 replace this branch with `mlops`, `advance_step`, `evaluator`."""
        assert route_after_exec(state(last_outcome=outcome("CLEAN"))) == TERMINAL_NODE

    @pytest.mark.parametrize(
        "classification",
        [
            "RUNTIME_ERROR",
            "TIMEOUT",
            "OOM",
            "CONTRACT_VIOLATION",
            "VALIDATION_REJECTED",
            "UNKNOWN_FAILURE",
        ],
    )
    def test_every_failure_classification_enters_the_debug_loop(self, classification):
        failed = state(last_outcome=outcome(classification), debug_iterations=0)
        assert route_after_exec(failed) == "debugger"

    def test_the_debug_budget_ends_the_loop_before_another_diagnosis(self):
        spent = state(
            last_outcome=outcome("RUNTIME_ERROR"),
            debug_iterations=4,
            budgets=Budgets(max_debug_iterations=4),
        )
        assert route_after_exec(spent) == TERMINAL_NODE

    def test_the_sandbox_budget_ends_the_loop_even_with_debug_iterations_left(self):
        """Containers are the expensive resource; either ceiling stops the loop."""
        spent = state(
            last_outcome=outcome("RUNTIME_ERROR"),
            debug_iterations=1,
            budgets=Budgets(max_debug_iterations=99, max_sandbox_executions=3),
            usage=Usage(sandbox_executions=3, started_at=datetime.now(UTC)),
        )
        assert route_after_exec(spent) == TERMINAL_NODE

    def test_no_outcome_at_all_terminates_rather_than_looping(self):
        assert route_after_exec(state(last_outcome=None)) == TERMINAL_NODE


class TestRouteAfterDebug:
    def test_an_ordinary_diagnosis_goes_back_to_the_coder(self):
        debugged = state(
            debug_iterations=1, last_diagnosis=diagnosis(), errors=[error()]
        )
        assert route_after_debug(debugged) == "coder"

    def test_exceeding_the_debug_budget_terminates(self):
        spent = state(debug_iterations=5, budgets=Budgets(max_debug_iterations=4))
        assert route_after_debug(spent) == TERMINAL_NODE

    def test_requires_replan_escalates_to_the_planner(self):
        escalating = state(
            debug_iterations=1,
            last_diagnosis=diagnosis(requires_replan=True),
            replan_count=0,
            errors=[error()],
        )
        assert route_after_debug(escalating) == "planner"

    def test_requires_replan_with_no_replans_left_falls_back_to_the_coder(self):
        """Out of replans is not out of options — one more revision is still cheap."""
        exhausted = state(
            debug_iterations=1,
            last_diagnosis=diagnosis(requires_replan=True),
            replan_count=2,
            budgets=Budgets(max_replans=2),
            errors=[error()],
        )
        assert route_after_debug(exhausted) == "coder"

    def test_requires_research_continues_to_the_coder_until_a_researcher_exists(self):
        """§5.2 — thin context is never fatal; the Coder writes conservative code."""
        researching = state(
            debug_iterations=1,
            last_diagnosis=diagnosis(requires_research=True),
            errors=[error()],
        )
        assert route_after_debug(researching) == "coder"

    def test_three_identical_fingerprints_escalate_to_the_planner(self):
        """The anti-thrash guard: the approach is wrong, not the code (§5.5 rule 4)."""
        stagnant = state(
            debug_iterations=3,
            last_diagnosis=diagnosis(),
            replan_count=0,
            errors=[error(revision=n) for n in (1, 2, 3)],
        )
        assert route_after_debug(stagnant) == "planner"

    def test_two_identical_fingerprints_are_not_yet_stagnation(self):
        nearly = state(
            debug_iterations=2,
            last_diagnosis=diagnosis(),
            errors=[error(revision=1), error(revision=2)],
        )
        assert route_after_debug(nearly) == "coder"

    def test_three_different_fingerprints_are_progress_not_stagnation(self):
        """Distinct failures mean each fix worked and uncovered the next problem."""
        progressing = state(
            debug_iterations=3,
            last_diagnosis=diagnosis(),
            errors=[
                error("KeyError:target", 1),
                error("ValueError:shape", 2),
                error("TypeError:str", 3),
            ],
        )
        assert route_after_debug(progressing) == "coder"

    def test_stagnation_with_no_replans_left_still_returns_to_the_coder(self):
        exhausted = state(
            debug_iterations=3,
            last_diagnosis=diagnosis(),
            replan_count=2,
            budgets=Budgets(max_replans=2),
            errors=[error(revision=n) for n in (1, 2, 3)],
        )
        assert route_after_debug(exhausted) == "coder"

    def test_a_degraded_debugger_that_wrote_no_diagnosis_still_routes(self):
        assert route_after_debug(state(debug_iterations=1)) == "coder"
