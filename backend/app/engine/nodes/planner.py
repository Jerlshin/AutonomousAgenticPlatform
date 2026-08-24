"""`planner` — decompose the goal and define how success will be measured.

Specification: AGENTS.md §7.1. The Planner's most important output is not the step list,
it is `success_criteria`: the machine-checkable contract that makes evaluation objective.
Everything downstream — the Coder's target, the Evaluator's arithmetic, the report's
verdict — reads from it, and no model gets a vote on whether a criterion was met.

Schema validity is necessary but not sufficient, so a validated `Plan` is put through the
semantic checks in §7.1 (dataset bindings resolve, metrics are in the vocabulary, the
primary metric is actually a criterion) and the model is re-prompted once with the exact
problems. Re-prompting with a specific complaint is far cheaper than discovering the same
problem four nodes later as a `CONTRACT_VIOLATION`.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.engine.criteria import is_known_metric
from app.engine.nodes.base import FailurePolicy, get_chat_client, node
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.state import (
    AgentState,
    Plan,
    PlanStep,
    RunPhase,
    StepKind,
    StepStatus,
)
from app.engine.structured import call_structured
from app.services.datasets import find_dataset, manifest_for_prompt

logger = logging.getLogger(__name__)

MIN_STEPS = 3
MAX_STEPS = 6

# Kinds this phase can actually execute. `research` needs the Researcher (phase 3) and
# `evaluate` needs the Evaluator (phase 5); a plan may legitimately contain them, and the
# steps are marked SKIPPED rather than silently treated as done.
EXECUTABLE_KINDS = frozenset({StepKind.IMPLEMENT, StepKind.TRAIN})


@node(name="planner", phase=RunPhase.PLANNING, policy=FailurePolicy.RETRY_THEN_REPORT)
async def planner_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    llm = get_chat_client(config, "planner")
    prompt = load_prompt("planner")
    manifest = manifest_for_prompt()

    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        dataset_manifest=wrap_untrusted("dataset_manifest", manifest),
        failure_history=_failure_history(state),
        timeout=settings.SANDBOX_TRAIN_TIMEOUT_S,
        memory=settings.SANDBOX_TRAIN_MEMORY,
        min_steps=MIN_STEPS,
        max_steps=MAX_STEPS,
        goal=state.get("prompt", ""),
    )
    user = (
        "Produce the plan now, as a single JSON object matching the schema. "
        "Remember: every `train` step needs a dataset binding copied verbatim from the "
        "manifest, and `success_criteria` must be measurable from metrics.json."
    )

    result = await call_structured(llm, output_model=Plan, system=system, user=user)
    plan = _normalise(result.value)
    usage = result.usage

    problems = validate_plan(plan)
    if problems:
        logger.info("Plan failed semantic validation; re-prompting once: %s", problems)
        repair = (
            user
            + "\n\nYour previous plan was rejected:\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
        retry = await call_structured(
            llm, output_model=Plan, system=system, user=repair
        )
        plan = _normalise(retry.value)
        usage = usage.model_copy(
            update={
                "tokens_in": usage.tokens_in + retry.usage.tokens_in,
                "tokens_out": usage.tokens_out + retry.usage.tokens_out,
                "llm_calls": usage.llm_calls + retry.usage.llm_calls,
            }
        )
        remaining = validate_plan(plan)
        if remaining:
            # Accepted with its problems recorded. A flawed plan that runs and fails
            # legibly beats a rejected plan that produces nothing at all, and the
            # rejections travel into state where the report can quote them.
            logger.warning("Plan still has problems after repair: %s", remaining)
            plan.assumptions.append(
                "Plan validation problems accepted after one repair: "
                + "; ".join(remaining)
            )

    # Re-entering the Planner with a plan already in state is a replan — the Debugger or
    # the Evaluator judged the approach itself unworkable. The counter is the Planner's to
    # own (§3.5), and it is what `route_after_debug` reads to decide whether escalating
    # again is still affordable.
    replans = (state.get("replan_count") or 0) + (1 if state.get("plan") else 0)
    plan.revision = replans + 1
    current_step = _first_executable(plan)
    step_status = _initial_step_status(plan, current_step)

    return {
        "plan": plan,
        "plan_history": plan,
        "task_kind": plan.task_kind,
        "current_step_id": current_step.id if current_step else None,
        "step_status": step_status,
        "replan_count": replans,
        "usage": usage,
        "messages": [AIMessage(content=_plan_summary(plan))],
        "metadata": {
            **(state.get("metadata") or {}),
            "prompt_version_planner": prompt.version,
        },
    }


def validate_plan(plan: Plan) -> list[str]:
    """The §7.1 semantic checks the JSON Schema cannot express."""
    problems: list[str] = []
    criteria_metrics = {c.metric for c in plan.success_criteria}

    if not plan.success_criteria:
        problems.append(
            "`success_criteria` is empty; at least one measurable criterion is required."
        )
    if not any(c.required for c in plan.success_criteria):
        problems.append(
            "no criterion is marked `required: true`; at least one must be."
        )
    if plan.primary_metric not in criteria_metrics:
        problems.append(
            f"`primary_metric` is '{plan.primary_metric}' but no success criterion measures "
            f"it. Known criteria metrics: {sorted(criteria_metrics)}."
        )
    for criterion in plan.success_criteria:
        if not is_known_metric(criterion.metric):
            problems.append(
                f"criterion {criterion.id} names metric '{criterion.metric}', which is not "
                "in the standard vocabulary (accuracy, f1_macro, roc_auc, rmse, mae, r2, "
                "silhouette, …)."
            )

    if not MIN_STEPS <= len(plan.steps) <= MAX_STEPS:
        problems.append(
            f"the plan has {len(plan.steps)} steps; it must have between {MIN_STEPS} and "
            f"{MAX_STEPS}."
        )

    for step in plan.steps:
        if step.kind is not StepKind.TRAIN:
            continue
        if step.dataset is None:
            problems.append(
                f"step {step.id} has kind=train but no `dataset` binding. Copy an entry "
                "from the manifest verbatim."
            )
            continue
        entry = find_dataset(step.dataset.dataset_id)
        if entry is None:
            # An unseeded registry cannot arbitrate bindings, and rejecting every plan
            # until `make seed-datasets` has run would make the graph unusable. The
            # binding is left as the model wrote it and the sandbox will fail honestly
            # if the path does not exist.
            logger.warning(
                "Dataset '%s' bound by step %s is not in the manifest",
                step.dataset.dataset_id,
                step.id,
            )
        elif (
            entry.get("sha256")
            and step.dataset.sha256
            and entry["sha256"] != step.dataset.sha256
        ):
            problems.append(
                f"step {step.id} binds dataset '{step.dataset.dataset_id}' with sha256 "
                f"'{step.dataset.sha256}', but the manifest records '{entry['sha256']}'."
            )

    return problems


def _normalise(plan: Plan) -> Plan:
    """Renumber steps and reset execution state the model has no business setting."""
    for index, step in enumerate(plan.steps):
        step.index = index
        step.status = StepStatus.PENDING
        step.attempts = 0
    return plan


def _first_executable(plan: Plan) -> PlanStep | None:
    return next((step for step in plan.steps if step.kind in EXECUTABLE_KINDS), None)


def _initial_step_status(plan: Plan, current: PlanStep | None) -> dict[str, StepStatus]:
    """PENDING throughout, except steps this phase has no node to run.

    Marking them SKIPPED rather than leaving them PENDING keeps the record honest: the
    report can say a research step was planned and not performed, instead of implying it
    succeeded.
    """
    status: dict[str, StepStatus] = {}
    for step in plan.steps:
        if (
            current is not None
            and step.index < current.index
            and step.kind not in EXECUTABLE_KINDS
        ):
            status[step.id] = StepStatus.SKIPPED
        else:
            status[step.id] = StepStatus.PENDING
    return status


def _failure_history(state: AgentState) -> str:
    """Prior plans and their failures, for a replan. Empty on the first pass."""
    history = state.get("plan_history") or []
    errors = state.get("errors") or []
    verdicts = state.get("verdicts") or []
    if not (history or errors or verdicts):
        return "(No previous attempt — this is the first plan for this run.)"

    lines: list[str] = []
    for plan in history:
        lines.append(f"Plan revision {plan.revision} ({plan.task_kind}):")
        lines += [f"  - {step.kind.value}: {step.title}" for step in plan.steps]
    for error in errors:
        lines.append(
            f"Failure [{error.kind.value}] {error.fingerprint}: {error.message}"
        )
    for verdict in verdicts:
        lines.append(
            f"Verdict {verdict.decision.value} (score {verdict.score:.2f}): {verdict.summary}"
        )

    return wrap_untrusted("failure_history", "\n".join(lines))


def _plan_summary(plan: Plan) -> str:
    steps = "\n".join(
        f"{step.index + 1}. [{step.kind.value}] {step.title}" for step in plan.steps
    )
    criteria = ", ".join(
        f"{c.metric} {c.comparator} {c.threshold:g}" for c in plan.success_criteria
    )
    return f"Plan ({plan.task_kind}), primary metric {plan.primary_metric}:\n{steps}\nCriteria: {criteria}"
