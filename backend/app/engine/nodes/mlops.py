"""`mlops` — ingest `metrics.json`, log to MLflow, register models. No LLM.

Specification: AGENTS.md §7.7, MLOPS.md §4 (run hierarchy), §7 (registry), §11 (failure
handling). Reached only from `route_after_exec` when a `TRAIN` step just executed
`CLEAN`ly with metrics present (`routing.py`) — the JSON-Schema and semantic validation
`sandbox_exec` already ran (MLOPS.md §3.4) is what makes it safe to hand this node's fixed
tag/param/metric mapping a payload with no hallucinated names or NaN values in it.

Handing an LLM a validated `metrics.json` and asking it to call `mlflow.log_metric` would
introduce transcription errors into the one part of the system that must be exact
(AGENTS.md §1.3); this node is a mechanical mapping instead, all of it delegated to
`services.mlflow_client.MLflowService`.

**`DEGRADE`, but the actual absorption happens inside the node body.** MLflow being
unreachable must never fail a run (MLOPS.md §11), and the required behaviour is specific:
log a warning, still persist the `experiments` row with `mlflow_run_id = NULL`, and return
successfully so the run proceeds. That is a partial, data-carrying recovery — not "return an
empty update" — so it is handled by an inner `try/except` around the one call that can
reach an unreachable server, rather than by the `@node` decorator's binary success/failure
model. The decorator's `DEGRADE` policy is the outer safety net for anything that inner
guard did not anticipate, e.g. a bug in this module itself.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.db.models.experiment import Experiment
from app.engine.nodes.base import (
    FailurePolicy,
    get_db_session_factory,
    get_mlflow_service,
    get_sandbox,
    node,
)
from app.engine.state import AgentState, CodeRevision, RunPhase, SandboxOutcome

logger = logging.getLogger(__name__)


class MLOpsError(RuntimeError):
    """The node was reached in a state it cannot log from."""


def minimal_mlflow_update(
    state: AgentState, exc: Exception | None = None
) -> dict[str, Any]:
    """The `DEGRADE` fallback for a failure the inner MLflow guard did not already absorb.

    An MLflow outage never reaches here — it is caught inside `mlops_node` and returns a
    normal update. This path is for something else going wrong in this node (a missing
    `driver`, a programming error), and it leaves `mlflow`/`mlflow_history` unwritten,
    exactly like every other `DEGRADE` fallback in this package (compare
    `debugger.minimal_diagnosis_update`).
    """
    detail = f"{type(exc).__name__}: {exc}" if exc else "unknown failure"
    logger.error("mlops node failed outside its MLflow guard: %s", detail)
    return {}


@node(
    name="mlops",
    phase=RunPhase.TRACK,
    policy=FailurePolicy.DEGRADE,
    fallback=minimal_mlflow_update,
)
async def mlops_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    outcome = state.get("last_outcome")
    revision = state.get("current_revision")
    if outcome is None or revision is None or outcome.metrics is None:
        raise MLOpsError(
            "mlops was reached with no CLEAN, metrics-bearing sandbox outcome to log; "
            "route_after_exec should never send it anything else."
        )

    driver = get_sandbox(config)
    workdir = Path(driver.revision_dir(state["run_id"], revision.revision))
    service = get_mlflow_service(config)

    try:
        ref = await asyncio.to_thread(service.log_attempt, state, workdir=workdir)
    except Exception as exc:  # noqa: BLE001 - MLflow outages must never fail a run
        logger.warning(
            "MLflow logging failed for run %s revision %d: %s",
            state.get("run_id"),
            revision.revision,
            exc,
        )
        await _persist_experiment(
            config,
            state,
            revision,
            outcome,
            ref=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {
            "metadata": {
                **(state.get("metadata") or {}),
                "mlops_mlflow_error": f"{type(exc).__name__}: {exc}",
            }
        }

    await _persist_experiment(config, state, revision, outcome, ref=ref, error=None)

    logger.info(
        "Logged run %s attempt %d to MLflow: %s",
        state.get("run_id"),
        revision.revision,
        ref.ui_url,
    )
    return {"mlflow": ref, "mlflow_history": ref}


async def _persist_experiment(
    config: RunnableConfig,
    state: AgentState,
    revision: CodeRevision,
    outcome: SandboxOutcome,
    *,
    ref: Any | None,
    error: str | None,
) -> None:
    """Write the durable `experiments` row (AGENTS.md §7.7 step 9).

    Written unconditionally — whether or not the MLflow call succeeded. This row, not the
    MLflow run, is what `mlflow_backfill` (`app/worker/cron.py`) reads to find attempts
    that were logged here while MLflow was down and retry them (MLOPS.md §11). Persistence
    failing is itself absorbed the same way: this node must never fail a run that otherwise
    completed its sandbox execution.
    """
    task_id_raw = state.get("task_id") or state.get("run_id")
    try:
        task_id = uuid.UUID(str(task_id_raw))
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "mlops could not persist an experiments row: task_id %r is not a UUID",
            task_id_raw,
        )
        return

    metrics_payload = outcome.metrics or {}
    plan = state.get("plan")
    task_kind = (
        (plan.task_kind if plan else None) or state.get("task_kind") or "general"
    )
    row = Experiment(
        task_id=task_id,
        run_id=str(state.get("run_id") or ""),
        revision=revision.revision,
        task_kind=task_kind,
        mlflow_experiment_id=ref.experiment_id if ref else None,
        mlflow_run_id=ref.run_id if ref else None,
        mlflow_parent_run_id=ref.parent_run_id if ref else None,
        artifact_uri=ref.artifact_uri if ref else None,
        params=ref.logged_params if ref else {},
        metrics=ref.logged_metrics
        if ref
        else dict(metrics_payload.get("metrics") or {}),
        tags={},
        registered_model_name=ref.registered_model if ref else None,
        registered_model_version=ref.model_version if ref else None,
        metadata_json={"mlflow_error": error} if error else None,
    )

    session_factory = get_db_session_factory(config)
    try:
        async with session_factory() as session:
            session.add(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - persistence here is best-effort, never load-bearing
        logger.warning(
            "mlops could not persist the experiments row for run %s: %s",
            state.get("run_id"),
            exc,
        )


__all__ = ["MLOpsError", "minimal_mlflow_update", "mlops_node"]
