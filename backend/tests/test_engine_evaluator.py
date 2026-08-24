"""The Evaluator node (AGENTS.md §7.6, §6.2–§6.3).

Two things are under test here and they are tested separately on purpose.

*Stage 1 and the decision engine* are pure arithmetic over state — no model, no I/O — so
every row of §7.6's decision table is a one-line assertion. That is the half that decides
whether a run passed, and it must be provable rather than plausible.

*Stage 2* is a local model's opinion, and the assertions about it are all about what it is
**not** allowed to do: it cannot pass a failing run, it cannot upgrade a REPLAN, and it
cannot cost the run its verdict by being unreachable.
"""

from __future__ import annotations

import uuid

import pytest

from app.engine.criteria import check_criteria
from app.engine.nodes.evaluator import (
    REFINE_GAP_CEILING,
    RubricAssessment,
    build_verdict,
    decide,
    deterministic_verdict,
    evaluator_node,
    gap_sentence,
    nearest_gap,
    relative_gap,
)
from app.engine.state import (
    Budgets,
    CodeRevision,
    CriterionResult,
    EvalDecision,
    Plan,
    PlanStep,
    RunOutcome,
    SandboxOutcome,
    StepKind,
    StepStatus,
    SuccessCriterion,
    ValidationReport,
    Verdict,
)
from tests.conftest import rubric_reply
from tests.fakes import FakeChatModel, FakeDbSessionFactory, run

METRICS_DOC = {
    "schema_version": "1.0",
    "task_kind": "tabular-classification",
    "framework": "scikit-learn",
    "metrics": {"accuracy": 0.9737, "f1_macro": 0.9712},
}


def criterion(**overrides) -> SuccessCriterion:
    base = {
        "id": "c1",
        "metric": "accuracy",
        "comparator": "gte",
        "threshold": 0.95,
        "required": True,
        "weight": 1.0,
    }
    return SuccessCriterion(**{**base, **overrides})


def plan_with(*criteria: SuccessCriterion, kind: StepKind = StepKind.TRAIN) -> Plan:
    return Plan(
        steps=[PlanStep(id="s1", index=0, title="t", description="d", kind=kind)],
        success_criteria=list(criteria) or [criterion()],
        task_kind="tabular-classification",
        primary_metric="accuracy",
    )


def outcome(
    metrics: dict | None = None, classification: str = "CLEAN"
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


def state(**overrides) -> dict:
    base = {
        "run_id": str(uuid.uuid4()),
        "prompt": "Build a classifier reaching 95% accuracy.",
        "plan": plan_with(),
        "last_outcome": outcome(METRICS_DOC),
        "budgets": Budgets(),
        "current_revision": CodeRevision(
            revision=1, content="print(1)\n", sha256="0" * 64
        ),
    }
    base.update(overrides)
    return base


def result(**overrides) -> CriterionResult:
    base = {
        "criterion_id": "c1",
        "metric": "accuracy",
        "comparator": "gte",
        "threshold": 0.95,
        "observed": 0.91,
        "passed": False,
        "required": True,
        "weight": 1.0,
    }
    return CriterionResult(**{**base, **overrides})


class TestStageOneIsAuthoritative:
    def test_the_verdict_reads_passed_and_score_from_the_criteria_arithmetic(self):
        verdict, _ = build_verdict(state())
        expected, passed, score = check_criteria(
            plan_with().success_criteria, METRICS_DOC["metrics"]
        )
        assert verdict.passed is passed is True
        assert verdict.score == score
        assert [r.model_dump() for r in verdict.criteria_results] == [
            r.model_dump() for r in expected
        ]

    def test_an_absent_metric_fails_rather_than_being_ignored(self):
        """Absence is not success (§7.6)."""
        missing = {**METRICS_DOC, "metrics": {"accuracy": 0.99}}
        verdict, run_outcome = build_verdict(
            state(
                plan=plan_with(
                    criterion(), criterion(id="c2", metric="f1_macro", threshold=0.9)
                ),
                last_outcome=outcome(missing),
            )
        )
        assert verdict.passed is False
        assert verdict.criteria_results[1].observed is None
        assert "absent" in verdict.criteria_results[1].note
        assert run_outcome is None  # not settled — the loop gets a chance

    def test_a_non_required_criterion_does_not_block_acceptance(self):
        low = {**METRICS_DOC, "metrics": {"accuracy": 0.96, "roc_auc": 0.5}}
        verdict, run_outcome = build_verdict(
            state(
                plan=plan_with(
                    criterion(),
                    criterion(
                        id="c2", metric="roc_auc", threshold=0.99, required=False
                    ),
                ),
                last_outcome=outcome(low),
            )
        )
        assert verdict.passed is True
        assert verdict.decision is EvalDecision.ACCEPT
        assert run_outcome is RunOutcome.SUCCEEDED
        assert verdict.score == pytest.approx(0.5)  # half the weight earned

    def test_a_weighted_score_rewards_clearing_the_stretch_goals(self):
        both = {**METRICS_DOC, "metrics": {"accuracy": 0.96, "roc_auc": 0.995}}
        verdict, _ = build_verdict(
            state(
                plan=plan_with(
                    criterion(),
                    criterion(
                        id="c2", metric="roc_auc", threshold=0.99, required=False
                    ),
                ),
                last_outcome=outcome(both),
            )
        )
        assert verdict.score == 1.0


class TestOutcomeAgreesWithTheFinalizer:
    """The Evaluator and the Finalizer both write `outcome` (§3.5). They must agree.

    `criteria.determine_outcome` is what `reporter` and `finalizer` decide the run outcome
    with, and `finalizer` runs last — so a disagreement does not show up as an error, it
    shows up as an `evaluations` row that contradicts the report the user reads.
    """

    @pytest.mark.parametrize(
        ("metrics", "classification"),
        [
            ({"metrics": {"accuracy": 0.97}}, "CLEAN"),
            ({"metrics": {"accuracy": 0.50}}, "CLEAN"),
            (None, "CLEAN"),  # ran fine, wrote no metrics.json
            (None, "RUNTIME_ERROR"),
            (None, "CONTRACT_VIOLATION"),
        ],
    )
    def test_a_settled_verdict_matches_determine_outcome(self, metrics, classification):
        from app.engine.criteria import determine_outcome

        spent = Budgets(max_debug_iterations=0, max_replans=0)
        node_state = state(last_outcome=outcome(metrics, classification), budgets=spent)
        _verdict, run_outcome = build_verdict(node_state)
        assert run_outcome is not None  # the budgets are spent, so every case settles
        assert run_outcome is determine_outcome(node_state)[0]

    def test_a_clean_run_that_wrote_no_metrics_is_partial_not_failed(self):
        """It produced a real, reproducible run. It just did not measure anything."""
        _verdict, run_outcome = build_verdict(
            state(
                last_outcome=outcome(None),
                budgets=Budgets(max_debug_iterations=0, max_replans=0),
            )
        )
        assert run_outcome is RunOutcome.PARTIAL

    def test_a_missing_metric_is_still_an_infinite_gap(self):
        """Not forgiven, just classified as a quality miss rather than a crash."""
        verdict, _ = build_verdict(state(last_outcome=outcome(None)))
        assert verdict.passed is False
        assert nearest_gap(verdict.criteria_results) == float("inf")


class TestGapArithmetic:
    def test_a_shortfall_is_measured_as_a_fraction_of_the_threshold(self):
        assert relative_gap(result(observed=0.912, threshold=0.95)) == pytest.approx(
            0.04
        )

    def test_an_overshoot_on_a_lte_criterion_uses_the_same_formula(self):
        rmse = result(metric="rmse", comparator="lte", threshold=0.60, observed=0.75)
        assert relative_gap(rmse) == pytest.approx(0.25)

    def test_a_metric_that_was_never_computed_is_infinitely_far_away(self):
        """A number that does not exist is not "close to" anything."""
        assert relative_gap(result(observed=None)) == float("inf")

    def test_a_passing_criterion_has_no_gap(self):
        assert relative_gap(result(observed=0.97, passed=True)) == 0.0

    def test_a_zero_threshold_falls_back_to_the_absolute_shortfall(self):
        assert relative_gap(result(threshold=0.0, observed=0.2)) == pytest.approx(0.2)

    def test_an_approx_criterion_only_counts_the_shortfall_beyond_its_tolerance(self):
        approx = result(comparator="approx", threshold=1.0, observed=1.05)
        assert relative_gap(approx, tolerance=0.02) == pytest.approx(0.03)

    def test_the_nearest_unmet_required_threshold_is_the_one_that_counts(self):
        """§7.6 says nearest, and the rubric's licence to downgrade is the counterweight."""
        results = [
            result(criterion_id="c1", observed=0.94),  # 1.1% short
            result(criterion_id="c2", metric="f1_macro", observed=0.30),  # 68% short
        ]
        assert nearest_gap(results) == pytest.approx(0.0105, abs=1e-3)

    def test_criteria_that_are_not_required_are_excluded_from_the_gap(self):
        results = [
            result(criterion_id="c1", observed=0.97, passed=True),
            result(criterion_id="c2", observed=0.1, required=False),
        ]
        assert nearest_gap(results) == float("inf")

    def test_the_gap_sentence_states_the_numbers_the_criteria_will_be_checked_against(
        self,
    ):
        sentence = gap_sentence([result(observed=0.9123)])
        assert "accuracy 0.9123" in sentence
        assert "≥ 0.95" in sentence
        assert "0.03" in sentence  # the absolute gap

    def test_the_gap_sentence_names_a_metric_that_was_never_written(self):
        sentence = gap_sentence([result(observed=None)])
        assert "never written to metrics.json" in sentence


class TestDecisionTable:
    """Every row of §7.6, as arithmetic."""

    def call(self, **overrides):
        base = {
            "passed": False,
            "clean": True,
            "gap": 0.05,
            "debug_iterations": 0,
            "refines_granted": 0,
            "replan_count": 0,
            "budgets": Budgets(),
        }
        return decide(**{**base, **overrides})

    def test_all_required_criteria_met_accepts(self):
        assert self.call(passed=True) == (EvalDecision.ACCEPT, RunOutcome.SUCCEEDED)

    def test_a_run_that_never_executed_cleanly_aborts_as_failed(self):
        assert self.call(clean=False) == (EvalDecision.ABORT, RunOutcome.FAILED)

    def test_both_budgets_spent_aborts_as_partial(self):
        """A real, reproducible result that is not good enough is not a crash."""
        assert self.call(
            debug_iterations=4,
            replan_count=2,
            budgets=Budgets(max_debug_iterations=4, max_replans=2),
        ) == (EvalDecision.ABORT, RunOutcome.PARTIAL)

    def test_a_narrow_gap_with_budget_left_refines(self):
        assert self.call(gap=REFINE_GAP_CEILING) == (EvalDecision.REFINE, None)

    def test_a_wide_gap_with_replans_left_replans(self):
        assert self.call(gap=0.9) == (EvalDecision.REPLAN, None)

    def test_a_narrow_gap_with_the_quality_budget_spent_replans_instead(self):
        assert self.call(
            gap=0.01, debug_iterations=4, budgets=Budgets(max_debug_iterations=4)
        ) == (EvalDecision.REPLAN, None)

    def test_refinements_count_against_the_same_budget_as_debug_iterations(self):
        """§6.2 — loop 2 shares loop 1's bound."""
        assert self.call(
            gap=0.01,
            debug_iterations=2,
            refines_granted=2,
            budgets=Budgets(max_debug_iterations=4),
        ) == (EvalDecision.REPLAN, None)

    def test_a_wide_gap_with_no_replans_left_still_tries_one_more_revision(self):
        """Out of replans is not out of options — the same rule `route_after_debug` uses."""
        assert self.call(gap=0.9, replan_count=2, budgets=Budgets(max_replans=2)) == (
            EvalDecision.REFINE,
            None,
        )

    def test_no_outcome_is_attached_to_a_decision_that_continues_the_run(self):
        for gap in (0.01, 0.9):
            _decision, run_outcome = self.call(gap=gap)
            assert run_outcome is None


class TestRubricIsAdvisory:
    def test_the_rubric_can_downgrade_a_refine_to_a_replan(self):
        """It can see the approach is structurally wrong when the numbers cannot."""
        near_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.94, "f1_macro": 0.93}}
        assessment = RubricAssessment(
            proposed_decision=EvalDecision.REPLAN,
            replan_directive="the target leaks; the split is meaningless",
        )
        verdict, run_outcome = build_verdict(
            state(last_outcome=outcome(near_miss)), assessment
        )
        assert verdict.decision is EvalDecision.REPLAN
        assert "leaks" in verdict.replan_directive
        assert run_outcome is None

    def test_the_rubric_cannot_upgrade_a_replan_to_a_refine(self):
        far_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.4, "f1_macro": 0.3}}
        assessment = RubricAssessment(
            proposed_decision=EvalDecision.REFINE, refine_directive="just tune it"
        )
        verdict, _ = build_verdict(state(last_outcome=outcome(far_miss)), assessment)
        assert verdict.decision is EvalDecision.REPLAN

    def test_the_rubric_cannot_accept_a_run_that_missed_a_required_criterion(self):
        missed = {**METRICS_DOC, "metrics": {"accuracy": 0.5, "f1_macro": 0.4}}
        assessment = RubricAssessment(
            proposed_decision=EvalDecision.ACCEPT, summary="looks good to me"
        )
        verdict, run_outcome = build_verdict(
            state(last_outcome=outcome(missed)), assessment
        )
        assert verdict.decision is not EvalDecision.ACCEPT
        assert verdict.passed is False
        assert run_outcome is None

    def test_the_rubric_cannot_abort_a_run_the_table_would_refine(self):
        near_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.94, "f1_macro": 0.935}}
        assessment = RubricAssessment(proposed_decision=EvalDecision.ABORT)
        verdict, _ = build_verdict(state(last_outcome=outcome(near_miss)), assessment)
        assert verdict.decision is EvalDecision.REFINE

    def test_a_downgrade_is_refused_when_no_replans_remain(self):
        near_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.94, "f1_macro": 0.935}}
        assessment = RubricAssessment(proposed_decision=EvalDecision.REPLAN)
        verdict, _ = build_verdict(
            state(
                last_outcome=outcome(near_miss),
                replan_count=2,
                budgets=Budgets(max_replans=2),
            ),
            assessment,
        )
        assert verdict.decision is EvalDecision.REFINE

    def test_the_rubric_is_normalised_to_one_score_per_dimension(self):
        assessment = RubricAssessment(
            rubric=[
                {"dimension": "methodology", "score": 4, "justification": "first"},
                {"dimension": "methodology", "score": 1, "justification": "duplicate"},
                {"dimension": "code_quality", "score": 3, "justification": "ok"},
            ]
        )
        verdict, _ = build_verdict(state(), assessment)
        assert [s.dimension for s in verdict.rubric] == ["methodology", "code_quality"]
        assert verdict.rubric[0].justification == "first"
        assert verdict.rubric_mean == pytest.approx(3.5)


class TestDirectives:
    def test_a_refine_directive_opens_with_the_measured_gap(self):
        near_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.9123, "f1_macro": 0.9412}}
        assessment = RubricAssessment(
            proposed_decision=None, refine_directive="Add StandardScaler and grid C."
        )
        verdict, _ = build_verdict(state(last_outcome=outcome(near_miss)), assessment)
        assert verdict.decision is EvalDecision.REFINE
        assert verdict.refine_directive.startswith("accuracy 0.9123")
        assert "Add StandardScaler" in verdict.refine_directive
        assert verdict.replan_directive is None

    def test_a_directive_is_never_empty_even_when_the_model_wrote_nothing(self):
        """`route_after_eval` sends the run to a node whose only instruction is this."""
        near_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.9123, "f1_macro": 0.9412}}
        verdict, _ = build_verdict(state(last_outcome=outcome(near_miss)), None)
        assert verdict.refine_directive
        assert "0.9123" in verdict.refine_directive

    def test_an_accepted_run_carries_no_directive(self):
        verdict, _ = build_verdict(state())
        assert verdict.refine_directive is None
        assert verdict.replan_directive is None


class TestDegradeDeterministic:
    def test_a_failed_rubric_still_produces_a_real_verdict(self):
        """§6.5 — if the LLM rubric fails, hard criteria alone decide."""
        update = deterministic_verdict(state(), RuntimeError("ollama is unreachable"))
        verdict = update["verdict"]
        assert verdict.passed is True
        assert verdict.decision is EvalDecision.ACCEPT
        assert verdict.rubric == []
        assert verdict.rubric_mean is None
        assert update["outcome"] is RunOutcome.SUCCEEDED

    def test_an_unreachable_model_does_not_stop_the_node_returning_a_verdict(self):
        class BrokenModel:
            async def ainvoke(self, _messages, **_kwargs):
                raise RuntimeError("ollama is unreachable")

        update = run(
            evaluator_node(
                state(),
                {"configurable": {"llm_clients": {"evaluator": BrokenModel()}}},
            )
        )
        assert update["verdict"].decision is EvalDecision.ACCEPT
        assert update["verdict"].rubric == []
        assert "evaluator_rubric_error" in update["metadata"]


class TestTheNode:
    def invoke(self, node_state, *, replies=None, sessions=None):
        llm = FakeChatModel(replies or [rubric_reply()])
        sessions = sessions if sessions is not None else FakeDbSessionFactory()
        update = run(
            evaluator_node(
                node_state,
                {
                    "configurable": {
                        "llm_clients": {"evaluator": llm},
                        "db_session_factory": sessions,
                    }
                },
            )
        )
        return update, llm, sessions

    def test_the_verdict_is_written_to_both_channels(self):
        update, _, _ = self.invoke(state())
        assert isinstance(update["verdict"], Verdict)
        assert update["verdicts"] is update["verdict"]  # `append` reducer

    def test_the_prompt_shows_the_model_the_arithmetic_as_a_fact(self):
        update, llm, _ = self.invoke(state())
        system = llm.calls[0][0].content
        assert "every required criterion was MET" in system
        assert "0.9737" in system
        assert update["verdict"].rubric_mean == pytest.approx(4.2)

    def test_the_prompt_states_the_shortfall_when_criteria_were_missed(self):
        near_miss = {**METRICS_DOC, "metrics": {"accuracy": 0.9123, "f1_macro": 0.9412}}
        _update, llm, _ = self.invoke(state(last_outcome=outcome(near_miss)))
        system = llm.calls[0][0].content
        assert "at least one required criterion was NOT met" in system
        assert "accuracy 0.9123" in system

    def test_the_prompt_carries_earlier_attempts_so_a_stalled_loop_is_visible(self):
        earlier = Verdict(
            decision=EvalDecision.REFINE,
            passed=False,
            score=0.3,
            refine_directive="add a scaler",
            summary="close",
        )
        _update, llm, _ = self.invoke(state(verdicts=[earlier]))
        system = llm.calls[0][0].content
        assert "Earlier attempts in this run" in system
        assert "add a scaler" in system

    def test_an_evaluate_step_is_marked_done_by_the_node_that_performs_it(self):
        plan = Plan(
            steps=[
                PlanStep(
                    id="s1", index=0, title="t", description="d", kind=StepKind.TRAIN
                ),
                PlanStep(
                    id="s2",
                    index=1,
                    title="judge",
                    description="d",
                    kind=StepKind.EVALUATE,
                    depends_on=["s1"],
                ),
            ],
            success_criteria=[criterion()],
            task_kind="tabular-classification",
            primary_metric="accuracy",
        )
        update, _, _ = self.invoke(
            state(plan=plan, step_status={"s1": StepStatus.SUCCEEDED})
        )
        assert update["step_status"] == {"s2": StepStatus.SUCCEEDED}

    def test_the_verdict_is_persisted_for_the_api_to_read_back(self):
        _update, _llm, sessions = self.invoke(
            state(task_id="0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")
        )
        assert len(sessions.rows) == 1
        row = sessions.rows[0]
        assert row.decision == "ACCEPT"
        assert row.passed is True
        assert row.score == 1.0
        assert len(row.rubric_scores) == 5
        assert row.criteria_results[0]["metric"] == "accuracy"

    def test_a_database_failure_does_not_cost_the_run_its_verdict(self):
        class BrokenSessions:
            def __call__(self):
                raise RuntimeError("postgres is unreachable")

        update, _, _ = self.invoke(state(), sessions=BrokenSessions())
        assert update["verdict"].decision is EvalDecision.ACCEPT

    def test_the_node_reports_its_own_token_usage(self):
        update, _, _ = self.invoke(state())
        assert update["usage"].llm_calls == 1
        assert update["usage"].tokens_in > 0
