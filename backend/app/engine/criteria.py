"""Deterministic success-criteria arithmetic (AGENTS.md §7.6, stage 1).

This module is the whole of the platform's notion of "did it work". It is pure Python
over `metrics.json`: no model is consulted, and no model output can override its result.
`reporter` and `finalizer` both decide the run outcome through `determine_outcome` here;
the `evaluator` node will call the identical function in Phase 5, so the three can never
drift apart.

A criterion whose metric is absent from `metrics.json` **fails**. Absence is not success —
that asymmetry is the one that stops a run that silently forgot to compute its headline
number from being reported as a win.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from app.engine.state import (
    AgentState,
    CriterionResult,
    Plan,
    RunOutcome,
    SuccessCriterion,
)
from app.schemas.metrics import observed_metrics

# (observed, threshold, tolerance) -> bool
COMPARATORS: dict[str, Callable[[float, float, float], bool]] = {
    "gte": lambda o, t, _: o >= t,
    "lte": lambda o, t, _: o <= t,
    "gt": lambda o, t, _: o > t,
    "lt": lambda o, t, _: o < t,
    "eq": lambda o, t, _: o == t,
    "approx": lambda o, t, tol: abs(o - t) <= tol,
}

COMPARATOR_SYMBOLS: dict[str, str] = {
    "gte": "≥",
    "lte": "≤",
    "gt": ">",
    "lt": "<",
    "eq": "=",
    "approx": "≈",
}


def check_criteria(
    criteria: list[SuccessCriterion],
    metrics: dict[str, float] | None,
) -> tuple[list[CriterionResult], bool, float]:
    """Evaluate every criterion against the observed metrics.

    Returns `(results, all_required_passed, weighted_score)`. `weighted_score` is the
    fraction of total criterion weight earned, so a run that meets its stretch goals
    scores above one that only clears the required bar.
    """
    observed_all = metrics or {}
    results: list[CriterionResult] = []
    total_w = 0.0
    earned_w = 0.0

    for c in criteria:
        raw = observed_all.get(c.metric)
        observed = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )

        if observed is None:
            note = "metric absent from metrics.json"
            passed = False
        elif not math.isfinite(observed):
            # NaN/Inf compares False against every threshold, which would read as an
            # ordinary quality miss rather than the numerical blow-up it actually is.
            note = f"metric is {observed} — the value was not actually computed"
            passed = False
        else:
            note = ""
            passed = COMPARATORS[c.comparator](observed, c.threshold, c.tolerance)

        results.append(
            CriterionResult(
                criterion_id=c.id,
                metric=c.metric,
                comparator=c.comparator,
                threshold=c.threshold,
                observed=observed,
                passed=passed,
                required=c.required,
                weight=c.weight,
                note=note,
            )
        )
        total_w += c.weight
        if passed:
            earned_w += c.weight

    all_required_passed = all(r.passed for r in results if r.required)
    score = earned_w / total_w if total_w else 0.0
    return results, all_required_passed, score


def determine_outcome(state: AgentState) -> tuple[RunOutcome, list[CriterionResult]]:
    """Decide the run outcome from execution state and the criteria contract.

    A run that executed cleanly but missed a required threshold is `PARTIAL`, not
    `FAILED`: it produced a real, reproducible result that simply is not good enough, and
    conflating that with a crash makes the two indistinguishable in the KPI table.

    It lives beside `check_criteria` rather than in `finalizer` because the Reporter has
    to state the outcome in its first paragraph and the Finalizer has to write it to
    `runs.final_state`. Two implementations of "did this run succeed" would eventually
    disagree, and the report contradicting the API is the worst possible way to find out.
    """
    if state.get("cancel_requested"):
        return RunOutcome.CANCELLED, []

    last = state.get("last_outcome")
    if last is None or last.classification != "CLEAN":
        return RunOutcome.FAILED, []

    plan: Plan | None = state.get("plan")
    criteria = plan.success_criteria if plan else []
    if not criteria:
        return RunOutcome.SUCCEEDED, []

    results, all_required_passed, _score = check_criteria(
        criteria, observed_metrics(last.metrics)
    )
    return (
        RunOutcome.SUCCEEDED if all_required_passed else RunOutcome.PARTIAL
    ), results


def format_criteria_table(criteria: list[SuccessCriterion]) -> str:
    """The criteria contract as a compact table for the Coder prompt."""
    if not criteria:
        return "(none specified)"
    lines = ["| criterion | metric | target | required |", "|---|---|---|---|"]
    for c in criteria:
        symbol = COMPARATOR_SYMBOLS.get(c.comparator, c.comparator)
        target = f"{symbol} {c.threshold:g}"
        if c.comparator == "approx":
            target += f" ± {c.tolerance:g}"
        lines.append(
            f"| {c.id} | `{c.metric}` | {target} | {'yes' if c.required else 'no'} |"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------------------------
#  Metric vocabulary  (MLOPS.md §5.1)
# ------------------------------------------------------------------------------------

# Free-form metric naming makes cross-run comparison impossible: `acc`, `accuracy`,
# `test_accuracy` and `Accuracy` become four incomparable columns. The Planner may only
# name metrics from this vocabulary in `success_criteria`.
KNOWN_METRICS: frozenset[str] = frozenset(
    {
        # classification
        "accuracy",
        "balanced_accuracy",
        "f1_macro",
        "f1_weighted",
        "f1_binary",
        "precision_macro",
        "recall_macro",
        "roc_auc",
        "pr_auc",
        "log_loss",
        "matthews_corrcoef",
        "cohen_kappa",
        # regression
        "rmse",
        "mae",
        "mape",
        "median_ae",
        "r2",
        "explained_variance",
        # forecasting
        "smape",
        "mase",
        # clustering
        "silhouette",
        "calinski_harabasz",
        "davies_bouldin",
    }
)

# Prefixes a criterion may wrap a vocabulary metric in. `train_` is deliberately absent —
# it is rejected at the schema boundary by SuccessCriterion.
_ALLOWED_PREFIXES: tuple[str, ...] = ("val_", "baseline_", "final_")


def is_known_metric(name: str) -> bool:
    """Whether `name` is in the vocabulary, allowing the documented prefix forms."""
    if name in KNOWN_METRICS:
        return True
    if name.startswith("cv_") and name.endswith(("_mean", "_std")):
        base = name[3:].rsplit("_", 1)[0]
        return base in KNOWN_METRICS
    return any(
        name.startswith(prefix) and name[len(prefix) :] in KNOWN_METRICS
        for prefix in _ALLOWED_PREFIXES
    )
