"""`evaluator` — decide whether the run met its contract, and what happens if it did not.

Specification: AGENTS.md §7.6, loops 2 and 3 in §6.2–§6.3, routing in §5.6.

**Two stages, and they are not equal.**

*Stage 1 — deterministic criteria checking, authoritative.* `engine.criteria.check_criteria`
is pure Python over `metrics.json`. `passed` and `score` come from that function and only
that function. A criterion whose metric is missing fails: absence is not success. This is
the same function `reporter` and `finalizer` reach through `determine_outcome`, so the
verdict, the report and the API can never disagree about whether a run worked.

*Stage 2 — advisory LLM rubric, informational.* The model scores five dimensions 1–5 and,
when criteria are unmet, argues for `REFINE` over `REPLAN` with a directive. It influences
the *routing decision* and the report's narrative. It never touches `passed`.

**The asymmetry is enforced here, after parsing** (`_reconcile_decision`): the model may
downgrade `REFINE → REPLAN`, because it can see that an approach is structurally wrong even
when the arithmetic gap is small; it may not upgrade `REPLAN → REFINE`, and it can never
produce `ACCEPT` for a run that missed a required criterion. A local 8B model asked to
grade work it is also being asked to approve will approve it, so the approval is simply not
one of the things it is allowed to say.

**Why the gap is computed against the *nearest* unmet required threshold** (§7.6): the
arithmetic is deliberately optimistic — "we are close, try again" — and the model's licence
to downgrade to `REPLAN` is the counterweight. A run one point short on one criterion and
hopeless on another is exactly the case where the numbers say REFINE and reading the code
says REPLAN.

**Failure policy `DEGRADE_DETERMINISTIC`.** The rubric call is wrapped inside the node body,
the way `mlops` wraps MLflow: a rubric failure is a partial, data-carrying recovery — stage 1
has already run, so the hard criteria decide alone and `rubric` is empty. The decorator's
declared fallback (`deterministic_verdict`) is the outer net for anything else going wrong
in this module, and it produces the same stage-1-only verdict. Either way a `Verdict` is
always written, because `route_after_eval` has to read a decision out of it.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.db.models.evaluation import Evaluation
from app.engine.criteria import COMPARATOR_SYMBOLS, check_criteria
from app.engine.nodes.base import (
    FailurePolicy,
    get_chat_client,
    get_db_session_factory,
    node,
)
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.state import (
    AgentState,
    Budgets,
    CriterionResult,
    EvalDecision,
    MLflowRef,
    Plan,
    RubricScore,
    RunOutcome,
    RunPhase,
    SandboxOutcome,
    StepKind,
    StepStatus,
    SuccessCriterion,
    Usage,
    Verdict,
    next_pending_step,
    refine_cycles,
)
from app.engine.structured import call_structured
from app.schemas.metrics import observed_metrics

logger = logging.getLogger(__name__)

# §7.6 decision table: a shortfall within this fraction of the threshold is treated as
# closable by another revision of the same plan rather than by a new approach.
REFINE_GAP_CEILING = 0.25

# The five dimensions of §7.6's rubric, in the order the report reads them.
RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "methodology",
    "code_quality",
    "metric_validity",
    "reproducibility",
    "goal_alignment",
)

NO_CODE = "(no code revision was produced for this run)"


class RubricAssessment(BaseModel):
    """The Evaluator's *advisory* output contract — deliberately not a `Verdict`.

    A `Verdict` carries `passed`, `score` and `criteria_results`, which are computed before
    this call is made. Asking an 8B model to echo them back costs tokens, invites
    transcription errors, and would put model output in the one place the design says model
    output may never reach. The model is asked only for what it is actually better at:
    grading the work and arguing for what to do next.
    """

    rubric: list[RubricScore] = Field(default_factory=list)
    proposed_decision: EvalDecision | None = None
    refine_directive: str | None = None
    replan_directive: str | None = None
    summary: str = ""


# ------------------------------------------------------------------------------------
#  Stage 1 and the decision engine — pure functions, no model
# ------------------------------------------------------------------------------------


def criteria_for(state: AgentState) -> list[SuccessCriterion]:
    plan: Plan | None = state.get("plan")
    return list(plan.success_criteria) if plan else []


def executed_cleanly(state: AgentState) -> bool:
    """Whether this run ever got a program to run to completion.

    This is the last row of §7.6's decision table — "no `metrics` at all (never executed
    cleanly)" — and the parenthetical is the operative half. It deliberately does **not**
    also require `metrics is not None`, because that would put this node's `outcome` out of
    step with `criteria.determine_outcome`, which `reporter` and `finalizer` both use:
    a program that ran cleanly and wrote no `metrics.json` is `PARTIAL` there and would be
    `FAILED` here, and the API contradicting the report is the worst possible way to find
    that out. A missing metric is not silently forgiven either — it fails its criterion in
    stage 1 with an infinite gap, so it reads as a quality miss, which is what it is.

    A TRAIN step cannot reach here in that state anyway: exit 0 with no valid metrics.json
    is classified `CONTRACT_VIOLATION` by `sandbox_exec` and routed to the Debugger.
    """
    outcome: SandboxOutcome | None = state.get("last_outcome")
    return outcome is not None and outcome.classification == "CLEAN"


def relative_gap(result: CriterionResult, tolerance: float = 0.0) -> float:
    """How far short of its threshold a criterion fell, as a fraction of the threshold.

    `inf` when the metric was never produced: a number that does not exist is not "close
    to" anything, and treating absence as a narrow miss is how a run that forgot to compute
    its headline metric gets four more attempts at the same omission.
    """
    if result.passed:
        return 0.0
    if result.observed is None or not math.isfinite(result.observed):
        return math.inf

    shortfall = abs(result.observed - result.threshold)
    if result.comparator in {"approx", "eq"}:
        shortfall = max(0.0, shortfall - tolerance)
    if result.threshold == 0:
        return shortfall
    return shortfall / abs(result.threshold)


def nearest_gap(
    results: list[CriterionResult], criteria: list[SuccessCriterion] | None = None
) -> float:
    """The smallest relative shortfall among unmet *required* criteria (§7.6).

    `inf` when nothing required is unmet (the run passed) or when every unmet criterion is
    missing its metric entirely.
    """
    tolerances = {c.id: c.tolerance for c in (criteria or [])}
    gaps = [
        relative_gap(result, tolerances.get(result.criterion_id, 0.0))
        for result in results
        if result.required and not result.passed
    ]
    return min(gaps) if gaps else math.inf


def decide(
    *,
    passed: bool,
    clean: bool,
    gap: float,
    debug_iterations: int,
    refines_granted: int,
    replan_count: int,
    budgets: Budgets,
) -> tuple[EvalDecision, RunOutcome | None]:
    """§7.6's decision table, as arithmetic. Returns `(decision, outcome or None)`.

    `REFINE` and `REPLAN` return no outcome: the run is not over, and writing one would
    have the report announce a result for a run that is still working.

    The quality budget is `debug_iterations + refines_granted` against
    `max_debug_iterations`, because §6.2 says loop 2 *shares* loop 1's bound and
    `debug_iterations` is the Debugger's channel to write (§3.5) — the Evaluator cannot
    increment it, so it counts its own cycles out of the verdict history instead. Without
    that, the quality loop would be bounded only by `max_node_visits`, which is the
    "budget exhausted" ending §6.4 exists to avoid.

    The one case §7.6's table does not enumerate — a wide gap with replans spent but
    revisions still affordable — resolves to `REFINE`, matching `route_after_debug`'s
    treatment of the same situation: out of replans is not out of options, and one more
    revision is the cheapest thing left.
    """
    if passed:
        return EvalDecision.ACCEPT, RunOutcome.SUCCEEDED
    if not clean:
        return EvalDecision.ABORT, RunOutcome.FAILED

    quality_spent = debug_iterations + refines_granted >= budgets.max_debug_iterations
    replans_spent = replan_count >= budgets.max_replans

    if quality_spent and replans_spent:
        return EvalDecision.ABORT, RunOutcome.PARTIAL
    if gap <= REFINE_GAP_CEILING and not quality_spent:
        return EvalDecision.REFINE, None
    if not replans_spent:
        return EvalDecision.REPLAN, None
    if not quality_spent:
        return EvalDecision.REFINE, None
    return EvalDecision.ABORT, RunOutcome.PARTIAL


def _reconcile_decision(
    computed: EvalDecision, proposed: EvalDecision | None, *, replans_spent: bool
) -> EvalDecision:
    """Apply the model's proposal within the bounds of §7.6, and no further.

    One adjustment is permitted, in one direction: `REFINE → REPLAN`. Everything else the
    model might say about the decision is discarded here rather than being argued with in
    the prompt, because a rule enforced in code holds for every model and every
    temperature, and a rule stated in a prompt holds until the first time it does not.
    """
    if proposed is None or proposed is computed:
        return computed
    if (
        computed is EvalDecision.REFINE
        and proposed is EvalDecision.REPLAN
        and not replans_spent
    ):
        logger.info("Rubric downgraded REFINE to REPLAN: the approach is judged wrong")
        return EvalDecision.REPLAN
    logger.info(
        "Ignoring the rubric's proposed decision %s; the computed decision is %s",
        proposed.value,
        computed.value,
    )
    return computed


def gap_sentence(results: list[CriterionResult]) -> str:
    """The quantitative statement every directive opens with (§6.2).

    Deterministic on purpose: the numbers in a directive are facts the platform holds, and
    a model that paraphrases them into the Coder's prompt is one transcription error away
    from sending the next revision after the wrong target.
    """
    misses = [r for r in results if r.required and not r.passed]
    if not misses:
        return ""
    parts = []
    for r in sorted(misses, key=lambda r: relative_gap(r)):
        symbol = COMPARATOR_SYMBOLS.get(r.comparator, r.comparator)
        if r.observed is None:
            parts.append(
                f"{r.metric} was never written to metrics.json (required {symbol} "
                f"{r.threshold:g})"
            )
            continue
        gap = relative_gap(r)
        percent = "" if math.isinf(gap) else f", {gap * 100:.1f}% of the threshold"
        parts.append(
            f"{r.metric} {r.observed:.4g} vs. required {symbol} {r.threshold:g} "
            f"(gap {abs(r.observed - r.threshold):.4g}{percent})"
        )
    return "; ".join(parts) + "."


def _directives(
    decision: EvalDecision,
    results: list[CriterionResult],
    assessment: RubricAssessment | None,
) -> tuple[str | None, str | None]:
    """`(refine_directive, replan_directive)` — the gap arithmetic, then the model's prose.

    A directive is never empty. `route_after_eval` sends the run back to a node whose only
    instruction is this string, and "try again" with no target is how a quality loop turns
    into a random walk.
    """
    if decision not in {EvalDecision.REFINE, EvalDecision.REPLAN}:
        return None, None

    proposal = (
        (assessment.refine_directive if decision is EvalDecision.REFINE else None)
        or (assessment.replan_directive if decision is EvalDecision.REPLAN else None)
        if assessment
        else None
    )
    default = (
        "Keep the plan, the split and the seed, and change the modelling: scale the "
        "features, sweep the hyperparameters the estimator is most sensitive to, and "
        "re-measure on the same held-out set."
        if decision is EvalDecision.REFINE
        else "The approach cannot reach these criteria. Change the model family, the "
        "feature engineering, or the decomposition — do not resubmit this plan with "
        "cosmetic edits."
    )
    body = f"{gap_sentence(results)} {(proposal or default).strip()}".strip()
    return (body, None) if decision is EvalDecision.REFINE else (None, body)


def _clean_rubric(scores: list[RubricScore]) -> list[RubricScore]:
    """One score per dimension, in the documented order, first mention wins.

    A local model asked for five objects returns four, or six, or the same dimension twice.
    None of that is worth a repair round when the rubric is advisory — it is normalised and
    used for what survives.
    """
    by_dimension: dict[str, RubricScore] = {}
    for score in scores:
        by_dimension.setdefault(score.dimension, score)
    return [by_dimension[d] for d in RUBRIC_DIMENSIONS if d in by_dimension]


def _rubric_mean(rubric: list[RubricScore]) -> float | None:
    return sum(s.score for s in rubric) / len(rubric) if rubric else None


def _summary(
    decision: EvalDecision,
    passed: bool,
    score: float,
    results: list[CriterionResult],
    assessment: RubricAssessment | None,
) -> str:
    """One line the report can quote. The model's prose only if it wrote any."""
    if assessment is not None and assessment.summary.strip():
        return assessment.summary.strip()
    if passed:
        met = sum(1 for r in results if r.passed)
        return (
            f"All required criteria met ({met}/{len(results)} criteria, weighted score "
            f"{score:.2f}); accepting the run."
        )
    return f"{decision.value}: {gap_sentence(results)}".strip()


def build_verdict(
    state: AgentState, assessment: RubricAssessment | None = None
) -> tuple[Verdict, RunOutcome | None]:
    """Stage 1 plus the decision table, with the rubric folded in where it is allowed to.

    Called with `assessment=None` by the failure paths, which is exactly the
    `DEGRADE_DETERMINISTIC` contract: hard criteria alone decide, `rubric` is empty.
    """
    criteria = criteria_for(state)
    outcome: SandboxOutcome | None = state.get("last_outcome")
    metrics = observed_metrics(outcome.metrics if outcome else None)
    results, passed, score = check_criteria(criteria, metrics)

    budgets: Budgets = state.get("budgets") or Budgets()
    replan_count = state.get("replan_count") or 0
    # Every REFINE already in the history has been acted on: the verdict being built now is
    # not in `verdicts` yet, because this node has not returned its update.
    granted = refine_cycles(state.get("verdicts"))

    computed, run_outcome = decide(
        passed=passed,
        clean=executed_cleanly(state),
        gap=nearest_gap(results, criteria),
        debug_iterations=state.get("debug_iterations") or 0,
        refines_granted=granted,
        replan_count=replan_count,
        budgets=budgets,
    )
    decision = _reconcile_decision(
        computed,
        assessment.proposed_decision if assessment else None,
        replans_spent=replan_count >= budgets.max_replans,
    )
    if decision is not computed:
        # A downgrade to REPLAN ends the quality loop, so the outcome the table attached
        # to the computed decision no longer applies.
        run_outcome = None

    rubric = _clean_rubric(assessment.rubric) if assessment else []
    refine_directive, replan_directive = _directives(decision, results, assessment)

    verdict = Verdict(
        decision=decision,
        passed=passed,
        score=score,
        criteria_results=results,
        rubric=rubric,
        rubric_mean=_rubric_mean(rubric),
        refine_directive=refine_directive,
        replan_directive=replan_directive,
        summary=_summary(decision, passed, score, results, assessment),
    )
    return verdict, run_outcome


def _verdict_update(
    state: AgentState, verdict: Verdict, run_outcome: RunOutcome | None
) -> dict[str, Any]:
    """The state update for a verdict: the channels §3.5 says this node owns."""
    update: dict[str, Any] = {"verdict": verdict, "verdicts": verdict}
    if run_outcome is not None:
        update["outcome"] = run_outcome
    step_status = _evaluate_step_status(state)
    if step_status:
        update["step_status"] = step_status
    return update


def _evaluate_step_status(state: AgentState) -> dict[str, StepStatus]:
    """Mark a pending `evaluate` step done — this node is what performs it."""
    step = next_pending_step(state)
    if step is not None and step.kind is StepKind.EVALUATE:
        return {step.id: StepStatus.SUCCEEDED}
    return {}


def deterministic_verdict(
    state: AgentState, exc: Exception | None = None
) -> dict[str, Any]:
    """The `DEGRADE_DETERMINISTIC` fallback: stage 1 decides, with no rubric.

    Unlike the other `DEGRADE` fallbacks in this package, this one writes the channel it
    owns rather than leaving it empty. It can: the authoritative half of this node is pure
    arithmetic over state that is already present, so a run whose rubric call failed still
    gets a real verdict rather than an ambiguous absence for `route_after_eval` to guess at.
    """
    if exc is not None:
        logger.warning("Evaluator falling back to deterministic criteria only: %s", exc)
    verdict, run_outcome = build_verdict(state)
    return {**_verdict_update(state, verdict, run_outcome), "usage": Usage()}


# ------------------------------------------------------------------------------------
#  The node
# ------------------------------------------------------------------------------------


@node(
    name="evaluator",
    phase=RunPhase.EVALUATE,
    policy=FailurePolicy.DEGRADE_DETERMINISTIC,
    fallback=deterministic_verdict,
)
async def evaluator_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    criteria = criteria_for(state)
    outcome: SandboxOutcome | None = state.get("last_outcome")
    metrics = observed_metrics(outcome.metrics if outcome else None)
    results, passed, score = check_criteria(criteria, metrics)

    assessment, usage, rubric_error = await _assess(config, state, results, passed)
    verdict, run_outcome = build_verdict(state, assessment)

    logger.info(
        "Verdict for run %s: %s (passed=%s, score=%.2f, rubric mean %s)",
        state.get("run_id"),
        verdict.decision.value,
        verdict.passed,
        verdict.score,
        f"{verdict.rubric_mean:.1f}" if verdict.rubric_mean is not None else "n/a",
    )

    await _persist_evaluation(config, state, verdict)

    update = _verdict_update(state, verdict, run_outcome)
    metadata = {
        **(state.get("metadata") or {}),
        "prompt_version_evaluator": load_prompt("evaluator").version,
    }
    if rubric_error is not None:
        metadata["evaluator_rubric_error"] = rubric_error
    return {
        **update,
        "usage": usage,
        "messages": [
            AIMessage(content=f"Verdict {verdict.decision.value}: {verdict.summary}")
        ],
        "metadata": metadata,
    }


async def _assess(
    config: RunnableConfig,
    state: AgentState,
    results: list[CriterionResult],
    passed: bool,
) -> tuple[RubricAssessment | None, Usage, str | None]:
    """Stage 2, with its failure absorbed here rather than by the decorator.

    An unreachable model must not cost the run its verdict: stage 1 has already produced
    every number that matters, so the rubric is the optional half. Returns
    `(assessment or None, usage, error or None)`.
    """
    prompt = load_prompt("evaluator")
    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        goal_block=_goal_block(state),
        criteria_block=_criteria_block(results),
        passed_line=(
            "every required criterion was MET — this run passed"
            if passed
            else "at least one required criterion was NOT met"
        ),
        gap_block=_gap_block(results, passed),
        history_block=_history_block(state),
        code_block=_code_block(state),
    )
    user = (
        "Score the five dimensions now and return the JSON object and nothing else. "
        "Ground every justification in the code or the numbers above."
    )

    try:
        # Client resolution is inside the guard: an Ollama that is not installed or not
        # reachable is exactly the outage this policy exists to absorb, and it fails here
        # rather than at the call.
        llm = get_chat_client(config, "evaluator")
        result = await call_structured(
            llm, output_model=RubricAssessment, system=system, user=user
        )
    except Exception as exc:  # noqa: BLE001 - the rubric is advisory; stage 1 already ran
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Rubric unavailable for run %s (%s); deterministic criteria decide alone",
            state.get("run_id"),
            detail,
        )
        return None, Usage(), detail
    return result.value, result.usage, None


# ------------------------------------------------------------------------------------
#  Prompt blocks
# ------------------------------------------------------------------------------------


def _goal_block(state: AgentState) -> str:
    plan: Plan | None = state.get("plan")
    lines = [wrap_untrusted("user_prompt", state.get("prompt") or "(empty)")]
    if plan is not None:
        lines += [
            "",
            f"Task kind `{plan.task_kind}`, primary metric `{plan.primary_metric}`, "
            f"plan revision {plan.revision}.",
        ]
        if plan.assumptions:
            lines += ["", "Assumptions the Planner recorded:"] + [
                f"- {a}" for a in plan.assumptions
            ]
    return "\n".join(lines)


def _criteria_block(results: list[CriterionResult]) -> str:
    """The stage-1 arithmetic as a table. Facts, presented as facts."""
    if not results:
        return "(the plan declared no success criteria)"
    lines = [
        "| criterion | metric | target | observed | required | result |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        symbol = COMPARATOR_SYMBOLS.get(r.comparator, r.comparator)
        observed = "—" if r.observed is None else f"{r.observed:.4g}"
        note = f" ({r.note})" if r.note else ""
        lines.append(
            f"| {r.criterion_id} | `{r.metric}` | {symbol} {r.threshold:g} | {observed} "
            f"| {'yes' if r.required else 'no'} | {'PASS' if r.passed else 'FAIL'}{note} |"
        )
    return "\n".join(lines)


def _gap_block(results: list[CriterionResult], passed: bool) -> str:
    if passed:
        return (
            "The run met its contract. Score the rubric and leave `proposed_decision` "
            "null — there is nothing to recommend."
        )
    return f"### The shortfall\n\n{gap_sentence(results)}"


def _history_block(state: AgentState) -> str:
    """Earlier attempts: what has already been tried, and whether it helped.

    This is the `compare_mlflow_runs` of §7.6, done deterministically from
    `mlflow_history` and `verdicts` rather than as a tool the model may or may not choose
    to call. The question it answers — "did the last refinement actually move the number?"
    — is the one that separates a refinement worth repeating from a loop worth ending, and
    it is too important to leave to whether an 8B model decides to look it up.
    """
    history: list[MLflowRef] = state.get("mlflow_history") or []
    verdicts = state.get("verdicts") or []
    if not history and not verdicts:
        return ""

    lines = ["## Earlier attempts in this run", ""]
    for index, ref in enumerate(history, start=1):
        metrics = ", ".join(
            f"{k}={v:.4g}" for k, v in sorted(ref.logged_metrics.items())
        )
        lines.append(f"- attempt {index}: {metrics or '(no metrics logged)'}")
    for index, verdict in enumerate(verdicts, start=1):
        directive = verdict.refine_directive or verdict.replan_directive or ""
        lines.append(
            f"- verdict {index}: {verdict.decision.value} (score {verdict.score:.2f}) "
            f"{directive}".rstrip()
        )
    lines += [
        "",
        "If an earlier directive was followed and the number did not move, say so and "
        "prefer `REPLAN`.",
    ]
    return "\n".join(lines)


def _code_block(state: AgentState) -> str:
    revision = state.get("current_revision")
    return revision.content.rstrip() if revision is not None else NO_CODE


# ------------------------------------------------------------------------------------
#  Persistence
# ------------------------------------------------------------------------------------


async def _persist_evaluation(
    config: RunnableConfig, state: AgentState, verdict: Verdict
) -> None:
    """Write the durable `evaluations` row (ARCHITECTURE.md §7.1).

    Best-effort, exactly like `mlops._persist_experiment`: this node's output is already
    in state and in the checkpoint, and a database hiccup must not cost a run the verdict
    that decides where it goes next.
    """
    task_id_raw = state.get("task_id") or state.get("run_id")
    try:
        task_id = uuid.UUID(str(task_id_raw))
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "evaluator could not persist an evaluations row: task_id %r is not a UUID",
            task_id_raw,
        )
        return

    revision = state.get("current_revision")
    row = Evaluation(
        task_id=task_id,
        run_id=str(state.get("run_id") or ""),
        revision=revision.revision if revision is not None else 0,
        decision=verdict.decision.value,
        passed=verdict.passed,
        score=round(verdict.score, 4),
        criteria_results=[r.model_dump(mode="json") for r in verdict.criteria_results],
        rubric_scores=(
            [s.model_dump(mode="json") for s in verdict.rubric]
            if verdict.rubric
            else None
        ),
        replan_directive=verdict.replan_directive,
        refine_directive=verdict.refine_directive,
        summary=verdict.summary,
    )

    session_factory = get_db_session_factory(config)
    try:
        async with session_factory() as session:
            session.add(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - persistence here is never load-bearing
        logger.warning(
            "evaluator could not persist the evaluations row for run %s: %s",
            state.get("run_id"),
            exc,
        )


__all__ = [
    "REFINE_GAP_CEILING",
    "RUBRIC_DIMENSIONS",
    "RubricAssessment",
    "build_verdict",
    "decide",
    "deterministic_verdict",
    "evaluator_node",
    "gap_sentence",
    "nearest_gap",
    "relative_gap",
]
