"""`sandbox_exec` — validate, launch, stream, classify. No LLM.

Specification: AGENTS.md §7.4, classification table ARCHITECTURE.md §10.9. This node is
deterministic on purpose, and it is the reason the graph's control flow is reproducible:
the outcome is decided by `exit_code`, `OOMKilled`, `timed_out` and the validity of
`metrics.json` — never by a model's reading of stderr. Routing that depends on model
judgement is routing that fails nondeterministically.

Its failure policy is `FAIL_RUN`. A Docker-level failure — daemon down, image missing,
mount refused — is infrastructural, not agentic, and masking it as a code error would send
the Debugger chasing a bug that does not exist for the rest of the iteration budget.
"""

from __future__ import annotations

import logging
from typing import Any, cast, get_args

from langchain_core.runnables import RunnableConfig

from app.engine.errors import (
    error_from_traceback,
    synthetic_error,
    validation_error,
)
from app.engine.nodes.base import FailurePolicy, get_sandbox, node
from app.engine.state import (
    AgentState,
    Classification,
    Deliverable,
    DeliverableType,
    ErrorKind,
    ErrorRecord,
    Plan,
    PlanStep,
    RunPhase,
    SandboxOutcome,
    StepKind,
    StepStatus,
    Usage,
)
from app.schemas.metrics import check_dataset_binding, check_required_metrics
from app.services.sandbox import ArtifactRef, SandboxResult

logger = logging.getLogger(__name__)


class SandboxExecError(RuntimeError):
    """The node was reached in a state it cannot execute from."""


@node(name="sandbox_exec", phase=RunPhase.EXECUTE, policy=FailurePolicy.FAIL_RUN)
async def sandbox_exec_node(
    state: AgentState, config: RunnableConfig
) -> dict[str, Any]:
    revision = state.get("current_revision")
    if revision is None:
        raise SandboxExecError("sandbox_exec was reached with no code revision to run.")

    plan: Plan | None = state.get("plan")
    step = plan.step(state.get("current_step_id")) if plan else None
    profile = "train" if step is not None and step.kind is StepKind.TRAIN else "exec"
    driver = get_sandbox(config)

    result: SandboxResult = await driver.execute(
        run_id=state["run_id"],
        revision=revision.revision,
        code=revision.content,
        profile=profile,
        step_id=step.id if step else None,
        seed=int((state.get("metadata") or {}).get("seed", 42)),
        requirements=revision.requirements,
    )

    contract_problems = _contract_problems(result, plan, step)
    classification = classify(
        result,
        is_train=profile.startswith("train"),
        contract_problems=contract_problems,
    )

    outcome = SandboxOutcome(
        execution_id=result.execution_id,
        profile=result.profile,
        classification=classification,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        max_rss_bytes=result.max_rss_bytes,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
        stdout_ref=result.stdout_ref,
        stderr_ref=result.stderr_ref,
        metrics=result.metrics if classification == "CLEAN" else None,
        artifacts=[ref.model_dump() for ref in result.artifacts],
        validation=result.validation,
        revision=revision.revision,
    )

    update: dict[str, Any] = {
        "last_outcome": outcome,
        "outcomes": outcome,
        "deliverables": _deliverables(result),
        "usage": Usage(sandbox_executions=1),
    }

    if step is not None:
        update["step_status"] = {
            step.id: StepStatus.SUCCEEDED
            if classification == "CLEAN"
            else StepStatus.FAILED
        }

    if classification != "CLEAN":
        error = build_error(result, classification, contract_problems, revision.content)
        update["last_error"] = error
        update["errors"] = error
        logger.warning(
            "Sandbox execution classified %s for run %s revision %d: %s",
            classification,
            state.get("run_id"),
            revision.revision,
            error.message,
        )

    return update


def classify(
    result: SandboxResult,
    *,
    is_train: bool,
    contract_problems: list[str],
) -> Classification:
    """The §10.9 table, in the order the conditions must be tested.

    Order matters: a timed-out or OOM-killed container also exits non-zero, and reporting
    it as a runtime error would send the Debugger looking for a bug in code that was
    simply too expensive to run.
    """
    if not result.validation.passed:
        return "VALIDATION_REJECTED"
    if result.timed_out:
        return "TIMEOUT"
    if result.oom_killed:
        return "OOM"
    if result.exit_code == 0:
        if is_train and (result.metrics is None or contract_problems):
            return "CONTRACT_VIOLATION"
        return "CLEAN"
    if _looks_like_traceback(result.stderr_tail):
        return "RUNTIME_ERROR"
    return "UNKNOWN_FAILURE"


def build_error(
    result: SandboxResult,
    classification: Classification,
    contract_problems: list[str],
    source: str,
) -> ErrorRecord:
    """Turn a failed execution into the structured record the Debugger reasons from."""
    if classification == "RUNTIME_ERROR":
        error = error_from_traceback(
            result.stderr_tail, revision=result.revision, source=source
        )
    elif classification == "VALIDATION_REJECTED":
        error = validation_error(result.validation.rejections, revision=result.revision)
    elif classification == "CONTRACT_VIOLATION":
        error = synthetic_error(
            ErrorKind.CONTRACT_VIOLATION,
            "; ".join(contract_problems) or "metrics.json was not written",
            revision=result.revision,
            traceback_text=result.stderr_tail,
        )
    elif classification == "TIMEOUT":
        error = synthetic_error(
            ErrorKind.TIMEOUT,
            f"execution exceeded the wall-clock limit after {result.duration_ms} ms",
            revision=result.revision,
            traceback_text=result.stderr_tail,
        )
    elif classification == "OOM":
        error = synthetic_error(
            ErrorKind.OOM,
            f"the container was OOM-killed (peak RSS {result.max_rss_bytes or 'unknown'} bytes)",
            revision=result.revision,
            traceback_text=result.stderr_tail,
        )
    else:
        error = synthetic_error(
            ErrorKind.UNKNOWN,
            f"the program exited {result.exit_code} without a recognisable traceback",
            revision=result.revision,
            traceback_text=result.stderr_tail,
        )

    return error


def _contract_problems(
    result: SandboxResult, plan: Plan | None, step: PlanStep | None
) -> list[str]:
    """The plan-aware half of the metrics.json semantic checks (MLOPS.md §3.4).

    The driver already checked what it can see on its own — schema validity, finite
    values, declared files existing. These two need the plan: they are what catches a
    program that produced perfectly good metrics for the wrong dataset, or that quietly
    omitted the one number the run is judged on.
    """
    problems = list(result.metrics_errors)
    if result.metrics is None or plan is None:
        return problems

    problems += check_required_metrics(result.metrics, plan.success_criteria)
    if step is not None:
        problems += check_dataset_binding(result.metrics, step.dataset)
    return problems


def _looks_like_traceback(stderr: str) -> bool:
    return "Traceback (most recent call last)" in stderr or bool(
        [line for line in stderr.splitlines() if line.startswith('  File "')]
    )


def _deliverables(result: SandboxResult) -> list[Deliverable]:
    """Register every file the execution produced under /artifacts."""
    return [
        Deliverable(
            name=ref.path,
            artifact_type=_deliverable_type(ref),
            path=ref.abs_path,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            mime_type=ref.mime_type,
        )
        for ref in result.artifacts
    ]


# `ArtifactRef.artifact_type` uses the richer MLflow artifact vocabulary (MLOPS.md §6);
# `Deliverable.artifact_type` is the narrower API-facing one. Anything without a
# counterpart lands in `log`, which is the least-wrong bucket rather than a good one.
_DELIVERABLE_TYPES: frozenset[str] = frozenset(get_args(DeliverableType))


def _deliverable_type(ref: ArtifactRef) -> DeliverableType:
    if ref.artifact_type in _DELIVERABLE_TYPES:
        return cast(DeliverableType, ref.artifact_type)
    return "log"
