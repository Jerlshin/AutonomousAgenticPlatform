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
    RESEARCH_MAX_ROUNDS,
    TERMINAL_NODE,
    route_after_code,
    route_after_debug,
    route_after_eval,
    route_after_exec,
    route_after_plan,
    route_after_research,
)
from app.engine.state import (
    Budgets,
    CodeRevision,
    ContextPack,
    Diagnosis,
    ErrorKind,
    ErrorRecord,
    EvalDecision,
    Plan,
    PlanStep,
    SandboxOutcome,
    StepKind,
    StepStatus,
    SuccessCriterion,
    Usage,
    ValidationReport,
    Verdict,
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


def sufficient_pack() -> ContextPack:
    """Context already researched — used by tests that are not about research routing."""
    return ContextPack(sufficiency="sufficient")


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
        bare = {"plan": plan_with(StepKind.TRAIN), "context_pack": sufficient_pack()}
        assert route_after_plan(bare) == "coder"

    def test_a_budget_with_room_left_does_not_divert(self):
        ready = state(plan=plan_with(StepKind.TRAIN), context_pack=sufficient_pack())
        assert route_after_plan(ready) == "coder"


class TestRouteAfterPlan:
    def test_no_plan_means_planning_failed(self):
        assert route_after_plan(state(plan=None)) == TERMINAL_NODE

    @pytest.mark.parametrize("kind", [StepKind.IMPLEMENT, StepKind.TRAIN])
    def test_an_executable_step_with_sufficient_context_goes_to_the_coder(self, kind):
        ready = state(plan=plan_with(kind), context_pack=sufficient_pack())
        assert route_after_plan(ready) == "coder"

    @pytest.mark.parametrize("kind", [StepKind.IMPLEMENT, StepKind.TRAIN])
    def test_an_executable_step_with_no_context_yet_researches_first(self, kind):
        """§5.1 rule 5 — an implement/train step with nothing researched yet is sent to
        the Researcher before the Coder ever sees it."""
        assert route_after_plan(state(plan=plan_with(kind))) == "researcher"

    @pytest.mark.parametrize("kind", [StepKind.IMPLEMENT, StepKind.TRAIN])
    def test_an_executable_step_with_insufficient_context_researches_again(self, kind):
        thin = state(
            plan=plan_with(kind),
            context_pack=ContextPack(
                sufficiency="insufficient", gaps=["still missing"]
            ),
        )
        assert route_after_plan(thin) == "researcher"

    @pytest.mark.parametrize("kind", [StepKind.IMPLEMENT, StepKind.TRAIN])
    def test_partial_context_is_good_enough_to_code_from(self, kind):
        partial = state(
            plan=plan_with(kind), context_pack=ContextPack(sufficiency="partial")
        )
        assert route_after_plan(partial) == "coder"

    def test_an_evaluate_step_goes_to_the_evaluator(self):
        """§5.1 rule 7 — the Evaluator is the node that performs an `evaluate` step."""
        assert route_after_plan(state(plan=plan_with(StepKind.EVALUATE))) == "evaluator"

    def test_a_report_step_goes_to_the_reporter(self):
        """§5.1 rule 8. TERMINAL_NODE *is* the reporter."""
        assert route_after_plan(state(plan=plan_with(StepKind.REPORT))) == TERMINAL_NODE

    def test_a_standalone_research_step_is_evaluated_rather_than_looped_on(self):
        """Nothing can retire a standalone research step without `advance_step`, so
        routing to the Researcher would spin. The run is judged on what it has."""
        assert route_after_plan(state(plan=plan_with(StepKind.RESEARCH))) == "evaluator"

    def test_a_plan_with_nothing_pending_goes_to_the_evaluator(self):
        """§5.1 rule 3 — the criteria contract is the only thing left to settle."""
        plan = plan_with(StepKind.TRAIN)
        done = state(plan=plan, step_status={"s1": StepStatus.SUCCEEDED})
        assert route_after_plan(done) == "evaluator"


class TestRouteAfterResearch:
    def test_sufficient_context_goes_to_the_coder(self):
        ready = state(
            context_pack=sufficient_pack(), context_history=[sufficient_pack()]
        )
        assert route_after_research(ready) == "coder"

    def test_partial_context_is_good_enough_to_code_from(self):
        partial = state(
            context_pack=ContextPack(sufficiency="partial"),
            context_history=[ContextPack(sufficiency="partial")],
        )
        assert route_after_research(partial) == "coder"

    def test_insufficient_context_with_rounds_left_researches_again(self):
        thin = ContextPack(sufficiency="insufficient")
        assert (
            route_after_research(state(context_pack=thin, context_history=[thin]))
            == "researcher"
        )

    def test_insufficient_context_with_rounds_exhausted_goes_to_the_coder(self):
        """Insufficient context is never fatal — the Coder is told to stay conservative."""
        thin = ContextPack(sufficiency="insufficient")
        exhausted = state(
            context_pack=thin, context_history=[thin] * RESEARCH_MAX_ROUNDS
        )
        assert route_after_research(exhausted) == "coder"


class TestRouteAfterCode:
    def test_a_revision_goes_to_the_sandbox(self):
        assert route_after_code(state(current_revision=_revision())) == "sandbox_exec"

    def test_no_revision_means_the_coder_exhausted_its_repairs(self):
        assert route_after_code(state(current_revision=None)) == TERMINAL_NODE


def _revision() -> CodeRevision:
    return CodeRevision(revision=1, content="print(1)\n", sha256="0" * 64)


def outcome(
    classification: str = "CLEAN", *, metrics: dict | None = None
) -> SandboxOutcome:
    return SandboxOutcome(
        execution_id=uuid.uuid4(),
        profile="train",
        classification=classification,
        exit_code=0 if classification == "CLEAN" else 1,
        duration_ms=10,
        metrics=metrics,
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
    def test_a_clean_train_step_with_metrics_routes_to_mlops(self):
        """§5.4 rule 4 — a TRAIN step's metrics are logged before the run ends."""
        ready = state(
            plan=plan_with(StepKind.TRAIN),
            current_step_id="s1",
            last_outcome=outcome("CLEAN", metrics={"metrics": {"accuracy": 0.97}}),
        )
        assert route_after_exec(ready) == "mlops"

    def test_a_clean_execution_with_no_plan_ends_the_run(self):
        """Unreachable in practice, and terminal by choice: a run with no plan has no
        criteria contract, and "no criteria" would evaluate as a vacuous pass."""
        assert route_after_exec(state(last_outcome=outcome("CLEAN"))) == TERMINAL_NODE

    def test_a_clean_implement_step_goes_to_the_evaluator(self):
        """§5.4 rule 6. Only a TRAIN step's metrics belong in MLflow; an IMPLEMENT step
        has none, so with the plan complete it is judged directly.

        `sandbox_exec` marks the step it just ran before this router sees the state, which
        is what makes the plan "complete" here."""
        ready = state(
            plan=plan_with(StepKind.IMPLEMENT),
            current_step_id="s1",
            step_status={"s1": StepStatus.SUCCEEDED},
            last_outcome=outcome("CLEAN"),
        )
        assert route_after_exec(ready) == "evaluator"

    def test_a_clean_train_step_with_no_metrics_skips_mlops(self):
        """Defensive: exit 0 with no metrics is already CONTRACT_VIOLATION in practice
        (rule 3 catches it first), but mlops must never be asked to log nothing."""
        ready = state(
            plan=plan_with(StepKind.TRAIN),
            current_step_id="s1",
            step_status={"s1": StepStatus.SUCCEEDED},
            last_outcome=outcome("CLEAN"),
        )
        assert route_after_exec(ready) == "evaluator"

    def test_a_plan_with_executable_work_left_is_not_judged_early(self):
        """§5.4 rule 5 — `advance_step` is what would sequence to the next step, and it is
        not built. Evaluating here would replan a run that is merely unfinished."""
        two_steps = Plan(
            steps=[
                PlanStep(
                    id="s1",
                    index=0,
                    title="a",
                    description="d",
                    kind=StepKind.IMPLEMENT,
                ),
                PlanStep(
                    id="s2",
                    index=1,
                    title="b",
                    description="d",
                    kind=StepKind.TRAIN,
                    depends_on=["s1"],
                ),
            ],
            success_criteria=[
                SuccessCriterion(
                    id="c1", metric="accuracy", comparator="gte", threshold=0.9
                )
            ],
            task_kind="tabular-classification",
            primary_metric="accuracy",
        )
        ready = state(
            plan=two_steps,
            current_step_id="s1",
            step_status={"s1": StepStatus.SUCCEEDED},
            last_outcome=outcome("CLEAN"),
        )
        assert route_after_exec(ready) == TERMINAL_NODE

    def test_a_pending_report_step_does_not_count_as_executable_work(self):
        """It is performed by the node this router is heading towards."""
        with_report = Plan(
            steps=[
                PlanStep(
                    id="s1",
                    index=0,
                    title="a",
                    description="d",
                    kind=StepKind.IMPLEMENT,
                ),
                PlanStep(
                    id="s2",
                    index=1,
                    title="write it up",
                    description="d",
                    kind=StepKind.REPORT,
                    depends_on=["s1"],
                ),
            ],
            success_criteria=[
                SuccessCriterion(
                    id="c1", metric="accuracy", comparator="gte", threshold=0.9
                )
            ],
            task_kind="tabular-classification",
            primary_metric="accuracy",
        )
        ready = state(
            plan=with_report,
            current_step_id="s1",
            step_status={"s1": StepStatus.SUCCEEDED},
            last_outcome=outcome("CLEAN"),
        )
        assert route_after_exec(ready) == "evaluator"

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

    def test_requires_research_routes_to_the_researcher(self):
        """§5.5 rule 3 — a diagnosis blocked on an unknown API researches first."""
        researching = state(
            debug_iterations=1,
            last_diagnosis=diagnosis(requires_research=True),
            errors=[error()],
        )
        assert route_after_debug(researching) == "researcher"

    def test_requires_research_with_rounds_exhausted_falls_back_to_the_coder(self):
        """§5.2 — thin context is never fatal; the Coder writes conservative code."""
        thin = ContextPack(sufficiency="insufficient")
        exhausted = state(
            debug_iterations=1,
            last_diagnosis=diagnosis(requires_research=True),
            errors=[error()],
            context_history=[thin] * RESEARCH_MAX_ROUNDS,
        )
        assert route_after_debug(exhausted) == "coder"

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


def verdict(decision: EvalDecision = EvalDecision.ACCEPT, **overrides) -> Verdict:
    base = {
        "decision": decision,
        "passed": decision is EvalDecision.ACCEPT,
        "score": 1.0 if decision is EvalDecision.ACCEPT else 0.4,
        "summary": "test verdict",
    }
    return Verdict(**{**base, **overrides})


class TestRouteAfterEval:
    """§5.6 — the two back edges that close loops 2 and 3, and their bounds."""

    @pytest.mark.parametrize("decision", [EvalDecision.ACCEPT, EvalDecision.ABORT])
    def test_a_settled_verdict_goes_to_the_reporter(self, decision):
        settled = verdict(decision)
        assert (
            route_after_eval(state(verdict=settled, verdicts=[settled]))
            == TERMINAL_NODE
        )

    def test_refine_with_budget_left_returns_to_the_coder(self):
        """Loop 2: same plan, better code."""
        refining = verdict(EvalDecision.REFINE, refine_directive="add a scaler")
        routed = state(
            verdict=refining, verdicts=[refining], debug_iterations=0, replan_count=0
        )
        assert route_after_eval(routed) == "coder"

    def test_replan_with_replans_left_returns_to_the_planner(self):
        """Loop 3: a different approach, not a retry of this one."""
        replanning = verdict(EvalDecision.REPLAN, replan_directive="change the family")
        routed = state(
            verdict=replanning,
            verdicts=[replanning],
            replan_count=0,
            budgets=Budgets(max_replans=2),
        )
        assert route_after_eval(routed) == "planner"

    def test_replan_with_the_replan_budget_spent_reports_instead(self):
        replanning = verdict(EvalDecision.REPLAN, replan_directive="change the family")
        spent = state(
            verdict=replanning,
            verdicts=[replanning],
            replan_count=2,
            budgets=Budgets(max_replans=2),
        )
        assert route_after_eval(spent) == TERMINAL_NODE

    def test_the_verdict_being_routed_on_does_not_count_against_its_own_budget(self):
        """The newest REFINE has not been acted on yet, so it is not a spent cycle."""
        refining = verdict(EvalDecision.REFINE, refine_directive="add a scaler")
        first = state(
            verdict=refining,
            verdicts=[refining],
            debug_iterations=0,
            budgets=Budgets(max_debug_iterations=1),
        )
        assert route_after_eval(first) == "coder"

    def test_refinements_and_debug_iterations_share_one_budget(self):
        """§6.2 — loop 2 shares `max_debug_iterations` with loop 1."""
        refining = verdict(EvalDecision.REFINE, refine_directive="add a scaler")
        spent = state(
            verdict=refining,
            verdicts=[refining, refining],  # one already granted, plus this one
            debug_iterations=3,
            budgets=Budgets(max_debug_iterations=4),
        )
        assert route_after_eval(spent) == TERMINAL_NODE

    def test_four_refinements_are_granted_before_the_budget_stops_the_loop(self):
        """The bound is exactly `max_debug_iterations` cycles, not one fewer."""
        refining = verdict(EvalDecision.REFINE, refine_directive="add a scaler")
        budgets = Budgets(max_debug_iterations=4)
        granted = [
            route_after_eval(
                state(
                    verdict=refining,
                    verdicts=[refining] * (n + 1),
                    debug_iterations=0,
                    budgets=budgets,
                )
            )
            for n in range(5)
        ]
        assert granted == ["coder"] * 4 + [TERMINAL_NODE]

    def test_replan_verdicts_do_not_spend_the_refinement_budget(self):
        """The two loops have separate counters; only REFINE spends this one."""
        replanning = verdict(EvalDecision.REPLAN, replan_directive="change it")
        refining = verdict(EvalDecision.REFINE, refine_directive="add a scaler")
        routed = state(
            verdict=refining,
            verdicts=[replanning, replanning, replanning, refining],
            debug_iterations=0,
            budgets=Budgets(max_debug_iterations=2),
        )
        assert route_after_eval(routed) == "coder"

    def test_no_verdict_at_all_terminates_rather_than_guessing(self):
        assert route_after_eval(state(verdict=None)) == TERMINAL_NODE

    def test_cancellation_beats_a_refine_verdict(self):
        refining = verdict(EvalDecision.REFINE, refine_directive="add a scaler")
        cancelled = state(verdict=refining, verdicts=[refining], cancel_requested=True)
        assert route_after_eval(cancelled) == TERMINAL_NODE
