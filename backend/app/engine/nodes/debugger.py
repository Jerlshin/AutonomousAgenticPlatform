"""`debugger` — diagnose one failure and issue a fix directive. It never writes code.

Specification: AGENTS.md §7.5, loop 1 in §6.1. The split between diagnosis and synthesis
is the point of this node: only the Coder writes `current_revision`, so however confidently
a `Diagnosis` phrases itself it can never become a program by itself. That is what keeps
"one node owns code generation" true structurally rather than by convention, and it lets
this prompt be narrow — a 7B model asked to both diagnose and rewrite does neither well.

Two things are injected deterministically rather than left to the model:

* **The error-kind hint** (`errors.error_kind_hint`). The wall clock the profile allows,
  the peak RSS the container reached, the modules that exist in the image, the dataset the
  plan bound — all facts the platform knows. A model that guesses at them proposes
  `pip install`, which cannot work and costs a whole iteration to disprove.
* **The stagnation warning.** When consecutive failures share a fingerprint the previous
  diagnosis demonstrably did not work, and saying so is what stops the Coder–Debugger pair
  from generating cosmetically different code that fails identically until the budget runs
  out.

Failure policy `DEGRADE`: if the model cannot be made to emit a valid `Diagnosis`, the
declared fallback writes a minimal one carrying the raw error at `confidence=0.1`. The
Coder still gets a retry, and `debug_iterations` still advances — a debugger that failed
silently without advancing the counter would spin the loop against the global visit budget
instead of its own.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.engine.errors import consecutive_repeats, error_kind_hint
from app.engine.nodes.base import FailurePolicy, get_chat_client, node
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.state import (
    AgentState,
    Budgets,
    DatasetBinding,
    Diagnosis,
    ErrorRecord,
    Plan,
    RunPhase,
    Usage,
)
from app.engine.structured import call_structured

logger = logging.getLogger(__name__)

# A diagnosis quoting nothing is a guess dressed as an analysis. The prompt asks the model
# to say so itself (rule 2); this is the arithmetic that holds it to it, because a 7B model
# reporting 0.9 confidence on an unevidenced hunch is the normal case, not the rare one.
UNEVIDENCED_CONFIDENCE_CEILING = 0.4

# Consecutive identical fingerprints before the prompt tells the model its previous
# diagnosis failed. Two is deliberately earlier than the router's stagnation threshold of
# three: warn on the second, escalate to a replan on the third.
REPEAT_WARNING_AT = 2

NO_STDOUT = "(the program produced no output on stdout)"


class DebuggerError(RuntimeError):
    """The node was reached in a state it cannot diagnose from."""


def minimal_diagnosis_update(
    state: AgentState, exc: Exception | None = None
) -> dict[str, Any]:
    """The `DEGRADE` fallback: a diagnosis carrying the raw error and nothing invented.

    `confidence=0.1` is the honest number — no analysis happened. The Coder reads it as
    "you are on your own with this traceback", which is a materially better position than
    the one it would be in if this node wrote nothing and the loop simply repeated.
    """
    error: ErrorRecord | None = state.get("last_error")
    iteration = (state.get("debug_iterations") or 0) + 1
    fingerprint = error.fingerprint if error else "unknown"
    message = error.message if error else "the run failed without a recorded error"

    diagnosis = Diagnosis(
        error_fingerprint=fingerprint,
        root_cause=(
            f"Automated diagnosis was unavailable ({_reason(exc)}). The raw failure was: "
            f"{message}"
        ),
        evidence=[message],
        fix_strategy=(
            "Read the traceback and the failing source region directly, and make the "
            "smallest change that addresses the exception."
        ),
        targeted_changes=[_raw_directive(error, message)],
        confidence=0.1,
    )
    return {
        "last_diagnosis": diagnosis,
        "diagnoses": diagnosis,
        "debug_iterations": iteration,
        "usage": Usage(),
    }


def _raw_directive(error: ErrorRecord | None, message: str) -> str:
    """The one instruction available when no analysis happened: fix what crashed, where."""
    if error is None:
        return f"Diagnose and fix the failure directly: {message}"
    location = f" line {error.line}" if error.line else ""
    return (
        f"Fix the {error.kind.value} failure at {error.file or 'main.py'}{location}: "
        f"{message}"
    )


@node(
    name="debugger",
    phase=RunPhase.DEBUG,
    policy=FailurePolicy.DEGRADE,
    fallback=minimal_diagnosis_update,
)
async def debugger_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    error = state.get("last_error")
    if error is None:
        raise DebuggerError(
            "the debugger was reached with no error record; sandbox_exec writes one on "
            "every non-CLEAN classification."
        )

    budgets: Budgets = state.get("budgets") or Budgets()
    iteration = (state.get("debug_iterations") or 0) + 1
    outcome = state.get("last_outcome")
    dataset = _bound_dataset(state)

    llm = get_chat_client(config, "debugger")
    prompt = load_prompt("debugger")

    # Episodic memory is queried before the call, not offered as a tool the model may or
    # may not choose to use (§7.5). Phase 3 supplies the searcher; until then the block is
    # empty and the Debugger reasons from the traceback alone.
    prior_art = await _search_run_memory(config, state, error)

    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        kind=error.kind.value,
        fingerprint=error.fingerprint,
        revision=error.revision,
        iteration=iteration,
        max_iterations=budgets.max_debug_iterations,
        traceback_block=_traceback_block(error),
        stdout_block=wrap_untrusted(
            "sandbox_stdout", (outcome.stdout_tail if outcome else "") or NO_STDOUT
        ),
        line=error.line if error.line is not None else "unknown",
        offending_source=error.offending_source or "(source region unavailable)",
        error_kind_hint=error_kind_hint(error, outcome=outcome, dataset=dataset),
        plan_block=_plan_block(state, dataset),
        prior_art_block=_prior_art_block(prior_art),
        repeat_warning_block=repeat_warning_block(state),
    )
    user = (
        "Diagnose this failure now. Return the Diagnosis JSON object and nothing else. "
        "Do not write the corrected program — the Coder does that from your directive."
    )

    result = await call_structured(
        llm, output_model=Diagnosis, system=system, user=user
    )
    diagnosis = _reconcile(result.value, error, prior_art)

    logger.info(
        "Diagnosis for %s (iteration %d): %s [confidence %.2f, replan=%s]",
        error.fingerprint,
        iteration,
        diagnosis.root_cause,
        diagnosis.confidence,
        diagnosis.requires_replan,
    )

    return {
        "last_diagnosis": diagnosis,
        "diagnoses": diagnosis,
        "debug_iterations": iteration,
        "usage": result.usage,
        "messages": [
            AIMessage(
                content=f"Diagnosis (iteration {iteration}): {diagnosis.root_cause}"
            )
        ],
        "metadata": {
            **(state.get("metadata") or {}),
            "prompt_version_debugger": prompt.version,
        },
    }


def repeat_warning_block(state: AgentState) -> str:
    """The anti-thrash warning, injected once the same fingerprint recurs (§7.5).

    Empty until it applies. An always-present warning is one a model learns to ignore.
    """
    errors = state.get("errors") or []
    repeats = consecutive_repeats(errors)
    if repeats < REPEAT_WARNING_AT:
        return ""

    fingerprint = errors[-1].fingerprint
    return (
        f"## WARNING\n\n"
        f"This is failure number {repeats} in a row with fingerprint `{fingerprint}`. "
        "Your previous diagnosis did not work. Do NOT repeat it. Either identify a "
        "different root cause, or set `requires_replan: true` — the approach itself may "
        "be unworkable."
    )


def _reconcile(
    diagnosis: Diagnosis, error: ErrorRecord, prior_art: list[str]
) -> Diagnosis:
    """Reconcile model output with the facts the platform already holds.

    Three corrections, all of them cases where a 7B model is reliably wrong and the
    platform is reliably right: the fingerprint is computed, not remembered; prior art is
    what was actually retrieved, not what the model says it used; and confidence claimed
    without quoted evidence is capped.
    """
    confidence = diagnosis.confidence
    if not diagnosis.evidence:
        confidence = min(confidence, UNEVIDENCED_CONFIDENCE_CEILING)

    changes = [change for change in diagnosis.targeted_changes if change.strip()]
    if not changes:
        # A directive with no instruction in it leaves the Coder to regenerate blind,
        # which is how a debug loop turns into a random walk.
        changes = [diagnosis.fix_strategy or diagnosis.root_cause]

    return diagnosis.model_copy(
        update={
            "error_fingerprint": error.fingerprint,
            "confidence": confidence,
            "targeted_changes": changes,
            "prior_art": prior_art,
        }
    )


def _traceback_block(error: ErrorRecord) -> str:
    """The traceback, fenced as untrusted — it is program output, not instruction."""
    body = error.traceback.strip() or f"{error.exception_type}: {error.message}".strip()
    return wrap_untrusted("sandbox_stderr", body, trust="untrusted")


def _plan_block(state: AgentState, dataset: DatasetBinding | None) -> str:
    """What the run was trying to do, so the fix is judged against the actual goal."""
    plan: Plan | None = state.get("plan")
    if plan is None:
        return ""

    step = plan.step(state.get("current_step_id"))
    lines = [
        "### What the program was supposed to do",
        "",
        f"Task kind `{plan.task_kind}`, primary metric `{plan.primary_metric}`.",
    ]
    if step is not None:
        lines += ["", f"Current step — **{step.title}**: {step.description}"]
    if dataset is not None:
        lines += [
            "",
            f"Bound dataset `{dataset.dataset_id}` at `{dataset.path}`"
            + (
                f", target column `{dataset.target_column}`"
                if dataset.target_column
                else ""
            )
            + ".",
        ]
    criteria = ", ".join(
        f"{c.metric} {c.comparator} {c.threshold:g}" for c in plan.success_criteria
    )
    if criteria:
        lines += ["", f"Success criteria: {criteria}."]
    return "\n".join(lines)


def _prior_art_block(prior_art: list[str]) -> str:
    """Fixes that worked for this fingerprint in earlier runs (phase 3)."""
    if not prior_art:
        return ""
    body = "\n".join(f"- {item}" for item in prior_art)
    return (
        "### Prior art — fixes that worked for this error in earlier successful runs\n\n"
        + wrap_untrusted("run_memory", body, trust="verified")
    )


async def _search_run_memory(
    config: RunnableConfig, state: AgentState, error: ErrorRecord
) -> list[str]:
    """Episodic memory lookup, when a searcher is configured.

    Phase 3 injects one backed by the `run_memory` Qdrant collection. It is deliberately
    a config-supplied callable rather than an import: the Debugger must remain runnable —
    and testable — with no vector store, and a retrieval failure must never cost a
    diagnosis that the traceback alone could have produced.
    """
    configurable = (config or {}).get("configurable") or {}
    search = configurable.get("run_memory_search")
    if search is None:
        return []

    try:
        hits = search(
            fingerprint=error.fingerprint,
            message=error.message,
            task_kind=state.get("task_kind") or "",
        )
        if hasattr(hits, "__await__"):
            hits = await hits
        return [str(hit) for hit in (hits or [])]
    except Exception as exc:  # noqa: BLE001 - retrieval is an optimisation, never a gate
        logger.warning("run_memory lookup failed for %s: %s", error.fingerprint, exc)
        return []


def _bound_dataset(state: AgentState) -> DatasetBinding | None:
    plan: Plan | None = state.get("plan")
    step = plan.step(state.get("current_step_id")) if plan else None
    return step.dataset if step is not None else None


def _reason(exc: Exception | None) -> str:
    return f"{type(exc).__name__}: {exc}" if exc is not None else "unknown failure"


__all__ = [
    "DebuggerError",
    "debugger_node",
    "minimal_diagnosis_update",
    "repeat_warning_block",
]
