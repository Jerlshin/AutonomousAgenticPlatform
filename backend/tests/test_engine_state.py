"""State schema, reducers and criteria arithmetic (AGENTS.md §3, §7.6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.engine.criteria import check_criteria, is_known_metric
from app.engine.state import (
    Budgets,
    Plan,
    PlanStep,
    StepKind,
    StepStatus,
    SuccessCriterion,
    Usage,
    append,
    merge_step_status,
    merge_usage,
    next_pending_step,
)


def step(
    step_id: str, index: int, *, kind: StepKind = StepKind.TRAIN, depends_on=()
) -> PlanStep:
    return PlanStep(
        id=step_id,
        index=index,
        title=f"step {step_id}",
        description="…",
        kind=kind,
        depends_on=list(depends_on),
    )


class TestPlanValidation:
    def test_a_step_cannot_depend_on_itself(self):
        with pytest.raises(ValidationError, match="depend on itself"):
            step("s1", 0, depends_on=["s1"])

    def test_unknown_dependency_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown steps"):
            _plan([step("s1", 0, depends_on=["s9"])])

    def test_forward_dependency_is_rejected(self):
        """A step may only depend on one that comes before it — the DAG is ordered."""
        with pytest.raises(ValidationError, match="comes after it"):
            _plan([step("s1", 0, depends_on=["s2"]), step("s2", 1)])

    def test_ordered_dag_is_accepted(self):
        plan = _plan([step("s1", 0), step("s2", 1, depends_on=["s1"])])
        assert plan.step("s2").depends_on == ["s1"]
        assert plan.step("nope") is None


class TestSuccessCriterion:
    def test_training_metrics_are_refused(self):
        """MLOPS.md §5.1 — declaring victory on training accuracy is refused at the schema."""
        with pytest.raises(ValidationError, match="training-set metric"):
            SuccessCriterion(
                id="c1", metric="train_accuracy", comparator="gte", threshold=0.9
            )

    @pytest.mark.parametrize(
        "metric,known",
        [
            ("accuracy", True),
            ("cv_f1_macro_mean", True),
            ("baseline_rmse", True),
            ("made_up_score", False),
        ],
    )
    def test_metric_vocabulary(self, metric, known):
        assert is_known_metric(metric) is known


class TestCheckCriteria:
    def _criteria(self) -> list[SuccessCriterion]:
        return [
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=0.95, weight=2.0
            ),
            SuccessCriterion(
                id="c2",
                metric="roc_auc",
                comparator="gte",
                threshold=0.99,
                required=False,
            ),
        ]

    def test_required_pass_and_weighted_score(self):
        results, passed, score = check_criteria(
            self._criteria(), {"accuracy": 0.97, "roc_auc": 0.995}
        )
        assert passed is True
        assert score == pytest.approx(1.0)
        assert [r.passed for r in results] == [True, True]

    def test_stretch_goal_miss_still_passes(self):
        _results, passed, score = check_criteria(self._criteria(), {"accuracy": 0.97})
        assert passed is True
        assert score == pytest.approx(2 / 3)

    def test_absent_metric_fails_rather_than_passing(self):
        """Absence is not success — the asymmetry that stops a silent omission winning."""
        results, passed, _ = check_criteria(self._criteria(), {})
        assert passed is False
        assert results[0].observed is None
        assert results[0].note == "metric absent from metrics.json"

    def test_nan_is_reported_as_a_numerical_failure_not_a_quality_miss(self):
        results, passed, _ = check_criteria(
            self._criteria(), {"accuracy": float("nan")}
        )
        assert passed is False
        assert "not actually computed" in results[0].note

    def test_comparators(self):
        criteria = [
            SuccessCriterion(id="c1", metric="rmse", comparator="lte", threshold=0.6),
            SuccessCriterion(
                id="c2", metric="r2", comparator="approx", threshold=0.5, tolerance=0.05
            ),
        ]
        _results, passed, _ = check_criteria(criteria, {"rmse": 0.55, "r2": 0.53})
        assert passed is True


class TestReducers:
    def test_append_accumulates_history(self):
        assert append(None, "a") == ["a"]
        assert append(["a"], "b") == ["a", "b"]
        assert append(["a"], ["b", "c"]) == ["a", "b", "c"]

    def test_append_does_not_mutate_the_current_value(self):
        current = ["a"]
        append(current, "b")
        assert current == ["a"]

    def test_merge_usage_is_additive_and_pins_the_start_time(self):
        started = datetime.now(UTC)
        merged = merge_usage(
            Usage(tokens_in=10, llm_calls=1, node_visits=1, started_at=started),
            Usage(
                tokens_in=5,
                tokens_out=3,
                llm_calls=1,
                node_visits=1,
                sandbox_executions=1,
            ),
        )
        assert (merged.tokens_in, merged.tokens_out, merged.llm_calls) == (15, 3, 2)
        assert (merged.node_visits, merged.sandbox_executions) == (2, 1)
        assert merged.started_at == started

    def test_merge_usage_is_associative(self):
        a, b, c = (
            Usage(tokens_in=1, node_visits=1),
            Usage(tokens_in=2, node_visits=1),
            Usage(tokens_in=4, node_visits=1),
        )
        assert merge_usage(merge_usage(a, b), c) == merge_usage(a, merge_usage(b, c))

    def test_merge_step_status_is_a_right_biased_overlay(self):
        current = {"s1": StepStatus.SUCCEEDED, "s2": StepStatus.PENDING}
        merged = merge_step_status(current, {"s2": StepStatus.FAILED})
        assert merged == {"s1": StepStatus.SUCCEEDED, "s2": StepStatus.FAILED}
        assert current["s2"] is StepStatus.PENDING


class TestNextPendingStep:
    def test_skips_steps_whose_dependencies_have_not_succeeded(self):
        plan = _plan([step("s1", 0), step("s2", 1, depends_on=["s1"])])
        state = {
            "plan": plan,
            "step_status": {"s1": StepStatus.PENDING, "s2": StepStatus.PENDING},
        }
        assert next_pending_step(state).id == "s1"

    def test_advances_once_the_dependency_succeeds(self):
        plan = _plan([step("s1", 0), step("s2", 1, depends_on=["s1"])])
        state = {
            "plan": plan,
            "step_status": {"s1": StepStatus.SUCCEEDED, "s2": StepStatus.PENDING},
        }
        assert next_pending_step(state).id == "s2"

    def test_returns_none_when_everything_is_done(self):
        plan = _plan([step("s1", 0)])
        assert (
            next_pending_step(
                {"plan": plan, "step_status": {"s1": StepStatus.SUCCEEDED}}
            )
            is None
        )

    def test_returns_none_without_a_plan(self):
        assert next_pending_step({}) is None


def test_budgets_default_to_the_specified_envelope():
    budgets = Budgets()
    assert (
        budgets.max_debug_iterations,
        budgets.max_replans,
        budgets.max_node_visits,
    ) == (4, 2, 60)


def test_usage_elapsed_is_zero_before_the_clock_starts():
    assert Usage().elapsed_seconds == 0.0


def _plan(steps: list[PlanStep]) -> Plan:
    return Plan(
        steps=steps,
        success_criteria=[
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=0.9
            )
        ],
        task_kind="tabular-classification",
        primary_metric="accuracy",
    )
