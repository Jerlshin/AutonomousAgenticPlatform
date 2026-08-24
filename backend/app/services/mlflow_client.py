"""MLflow tracking facade: run hierarchy, tag taxonomy, flavor logging, registry.

Specification: `docs/MLOPS.md` §4 (run hierarchy and tags), §5 (metric/parameter
vocabulary), §7 (model registry), §11 (failure handling and backfill).

Every MLflow call goes through the explicit `MlflowClient` object — never the fluent
`mlflow.start_run()` / `mlflow.log_metric()` global-state API. The worker may log several
runs concurrently (`WORKER_MAX_JOBS`), and an explicit `run_id` on every call is the form
with no shared mutable "active run" to race on. It also means this whole module is
injectable: `MLflowService(client=...)` accepts anything with the same method surface, so
the entire logging sequence is unit-tested against `tests.fakes.FakeMlflowClient` with no
MLflow server, and often not even the `mlflow` package, involved.

**Never unpickles sandbox output.** `mlflow.sklearn.log_model()` and its siblings need a
live Python object, which would mean loading agent-generated pickle output inside the
worker — precisely the boundary `ARCHITECTURE.md` §10.2 exists to protect. Model files are
therefore treated as opaque bytes: hashed and uploaded by `sandbox_exec` already, logged
here by path. The optional `model/signature.json` and `model/input_example.json` sidecars
the sandbox helper writes are what let a real `MLmodel` descriptor be built host-side
without ever touching the pickle itself (§7.1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.engine.criteria import check_criteria
from app.engine.state import (
    AgentState,
    CodeRevision,
    MLflowRef,
    Plan,
    SandboxOutcome,
    Usage,
    ValidationReport,
)

logger = logging.getLogger(__name__)

# MLflow's documented limit on a single tag/param value (MLOPS.md §4.3, §5.3).
MAX_TAG_VALUE_LENGTH = 500
MAX_PARAM_VALUE_LENGTH = 500

# The margin a challenger must clear before it displaces the incumbent @champion
# (MLOPS.md §7.3). Without it, run-to-run noise flips the champion on every attempt and
# the alias stops meaning anything.
CHAMPION_MARGIN = 0.005

# Metrics whose "better" direction is downward (MLOPS.md §5.1). Every other known metric
# is assumed higher-is-better.
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {"log_loss", "rmse", "mae", "mape", "median_ae", "smape", "mase", "davies_bouldin"}
)

_FLAVOR_LOADER_MODULES: dict[str, str] = {
    "sklearn": "mlflow.sklearn",
    "pytorch": "mlflow.pytorch",
    "lightgbm": "mlflow.lightgbm",
    "xgboost": "mlflow.xgboost",
    "pyfunc": "mlflow.pyfunc",
}


class MLflowServiceError(RuntimeError):
    """A programming error in this module — never raised for MLflow being unreachable.

    `log_attempt` requires a CLEAN outcome with metrics; anything else is a caller bug, not
    an MLflow outage, and the `mlops` node's `DEGRADE` handling is only meant to absorb the
    latter (MLOPS.md §11).
    """


def _truncate(value: str, limit: int = MAX_TAG_VALUE_LENGTH) -> str:
    """MLflow's hard value limit, with a hash suffix so truncation is still identifiable."""
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    keep = max(limit - len(digest) - 1, 0)
    return f"{value[:keep]}…{digest}"


def flatten_params(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Nested params become dotted keys; MLflow params are `str -> str` (MLOPS.md §5.3)."""
    flat: dict[str, str] = {}
    for key, value in (params or {}).items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten_params(value, full_key))
        elif isinstance(value, list):
            flat[full_key] = _truncate(json.dumps(value), MAX_PARAM_VALUE_LENGTH)
        elif value is None:
            continue
        else:
            flat[full_key] = _truncate(str(value), MAX_PARAM_VALUE_LENGTH)
    return flat


def _finite_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    """Scalar, finite values only — the NaN/Inf check already rejected the rest upstream
    (MLOPS.md §3.4), this is just the type narrowing MLflow's API needs."""
    clean: dict[str, float] = {}
    for key, value in (metrics or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        as_float = float(value)
        if math.isfinite(as_float):
            clean[key] = as_float
    return clean


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sandbox_image(profile_name: str) -> str:
    """Best-effort `pluton.sandbox.image` tag value — never load-bearing."""
    try:
        from app.services.sandbox import profile_for

        return profile_for(profile_name).image
    except Exception:  # noqa: BLE001 - a missing/unknown profile just means no tag
        return ""


class MLflowService:
    """Experiment resolution, the parent/child run hierarchy, tags, metrics, registry.

    The MLflow SDK is synchronous; the `mlops` node runs every call through
    `asyncio.to_thread` rather than this class knowing anything about asyncio.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        tracking_uri: str | None = None,
        model_descriptor_writer: Any | None = None,
    ) -> None:
        self._client = client
        self.tracking_uri = tracking_uri or settings.MLFLOW_TRACKING_URI
        # Overridable the same way `client` is: the real writer needs `mlflow.models`,
        # which — like the tracking client itself — is only ever imported lazily, so a
        # test can substitute a lightweight fake and exercise the registration flow with
        # neither a live MLflow server nor the `mlflow` package installed.
        self._model_descriptor_writer = (
            model_descriptor_writer or self._write_mlmodel_descriptor
        )

    @property
    def client(self) -> Any:
        """The `MlflowClient`, created on first use.

        Lazy so importing this module — or driving it entirely against
        `tests.fakes.FakeMlflowClient` — never requires the `mlflow` package installed,
        matching `services/sandbox.py`'s lazy `docker` import and `services/vector_store.py`'s
        lazy `qdrant_client` import.
        """
        if self._client is None:
            from mlflow.tracking import MlflowClient

            self._client = MlflowClient(tracking_uri=self.tracking_uri)
        return self._client

    # ------------------------------------------------------------------------------
    #  Experiment resolution (MLOPS.md §4.2)
    # ------------------------------------------------------------------------------

    @staticmethod
    def experiment_name(task_kind: str) -> str:
        return f"{settings.MLFLOW_EXPERIMENT_PREFIX}/{task_kind}"

    def resolve_experiment(self, task_kind: str) -> str:
        name = self.experiment_name(task_kind)
        exp = self.client.get_experiment_by_name(name)
        if exp is None:
            return self.client.create_experiment(
                name=name,
                tags={
                    "pluton.managed": "true",
                    "pluton.task_kind": task_kind,
                    "pluton.created_by": "mlops-node",
                },
            )
        if getattr(exp, "lifecycle_stage", "active") == "deleted":
            # MLflow refuses to create an experiment whose name collides with a
            # soft-deleted one; restoring is the only way forward, not a corner case.
            self.client.restore_experiment(exp.experiment_id)
        return exp.experiment_id

    def ui_url(self, experiment_id: str, run_id: str) -> str:
        return (
            f"{settings.MLFLOW_PUBLIC_URL}/#/experiments/{experiment_id}/runs/{run_id}"
        )

    # ------------------------------------------------------------------------------
    #  Run hierarchy (MLOPS.md §4.1; idempotent lookup per AGENTS.md §10)
    # ------------------------------------------------------------------------------

    def _find_run(self, experiment_id: str, filter_string: str) -> Any | None:
        results = self.client.search_runs(
            [experiment_id], filter_string=filter_string, max_results=1
        )
        return results[0] if results else None

    def get_or_create_parent_run(
        self, experiment_id: str, run_id: str, tags: dict[str, str]
    ) -> Any:
        existing = self._find_run(
            experiment_id,
            f"tags.`pluton.run_id` = '{run_id}' and tags.`pluton.kind` = 'parent'",
        )
        if existing is not None:
            return existing
        return self.client.create_run(
            experiment_id,
            tags={
                **tags,
                "pluton.kind": "parent",
                "mlflow.runName": f"run-{run_id[:8]}",
            },
        )

    def get_or_create_child_run(
        self,
        experiment_id: str,
        parent_run_id: str,
        run_id: str,
        attempt: int,
        tags: dict[str, str],
    ) -> Any:
        existing = self._find_run(
            experiment_id,
            f"tags.`pluton.run_id` = '{run_id}' and tags.`pluton.attempt` = '{attempt}'",
        )
        if existing is not None:
            return existing
        return self.client.create_run(
            experiment_id,
            tags={
                **tags,
                "pluton.kind": "child",
                "mlflow.parentRunId": parent_run_id,
                "mlflow.runName": f"attempt-{attempt:03d}",
            },
        )

    # ------------------------------------------------------------------------------
    #  Tag taxonomy (MLOPS.md §4.3) — a fixed allowlist, nothing from model output
    # ------------------------------------------------------------------------------

    def parent_tags(self, state: AgentState, plan: Plan | None) -> dict[str, str]:
        tags: dict[str, str] = {
            "pluton.run_id": str(state.get("run_id") or ""),
            "pluton.task_id": str(state.get("task_id") or ""),
            "pluton.debug_iterations": str(state.get("debug_iterations") or 0),
            "pluton.replan_count": str(state.get("replan_count") or 0),
        }
        if plan is not None:
            tags["pluton.task_kind"] = plan.task_kind
            tags["pluton.primary_metric"] = plan.primary_metric
        return {k: _truncate(v) for k, v in tags.items() if v}

    def child_tags(
        self,
        state: AgentState,
        revision: CodeRevision,
        outcome: SandboxOutcome,
        payload: dict[str, Any],
        *,
        criteria_passed: bool,
        criteria_score: float,
    ) -> dict[str, str]:
        dataset = payload.get("dataset") or {}
        model_routing = state.get("model_routing") or {}
        metadata = state.get("metadata") or {}
        tags: dict[str, str] = {
            "pluton.run_id": str(state.get("run_id") or ""),
            "pluton.attempt": str(revision.revision),
            "pluton.code_sha256": revision.sha256,
            "pluton.sandbox.profile": outcome.profile,
            "pluton.sandbox.exit_code": str(outcome.exit_code),
            "pluton.sandbox.duration_ms": str(outcome.duration_ms),
            "pluton.dataset.id": str(dataset.get("id", "")),
            "pluton.dataset.sha256": str(dataset.get("sha256", "")),
            "pluton.model.coder": model_routing.get("coder", ""),
            "pluton.model.planner": model_routing.get("planner", ""),
            "pluton.prompt.coder": str(metadata.get("prompt_version_coder", "")),
            "pluton.prompt.planner": str(metadata.get("prompt_version_planner", "")),
            "pluton.seed": str(metadata.get("seed", "")),
            "pluton.criteria_passed": "true" if criteria_passed else "false",
            "pluton.criteria_score": f"{criteria_score:.4f}",
        }
        if revision.addresses_error:
            tags["pluton.addresses_error"] = revision.addresses_error
        image = _sandbox_image(outcome.profile)
        if image:
            tags["pluton.sandbox.image"] = image
        return {k: _truncate(v) for k, v in tags.items() if v}

    # ------------------------------------------------------------------------------
    #  Logging sequence (MLOPS.md §4.4): tags -> params -> metrics -> artifacts
    # ------------------------------------------------------------------------------

    def log_attempt(self, state: AgentState, *, workdir: Path) -> MLflowRef:
        """Log one attempt end to end and return its `MLflowRef`.

        Synchronous — the `mlops` node runs this through `asyncio.to_thread`. `workdir` is
        the revision's directory (`driver.revision_dir(run_id, revision)`): `main.py`,
        `stdout.log` and `stderr.log` live there directly, `artifacts/` beneath it.
        """
        outcome: SandboxOutcome | None = state.get("last_outcome")
        revision: CodeRevision | None = state.get("current_revision")
        if outcome is None or revision is None or outcome.metrics is None:
            raise MLflowServiceError(
                "log_attempt requires a CLEAN outcome with metrics and a code revision."
            )

        plan: Plan | None = state.get("plan")
        payload: dict[str, Any] = outcome.metrics
        task_kind = (
            (plan.task_kind if plan else None) or state.get("task_kind") or "general"
        )
        run_id = str(state.get("run_id") or "")

        results, all_required_passed, score = check_criteria(
            plan.success_criteria if plan else [], dict(payload.get("metrics") or {})
        )

        experiment_id = self.resolve_experiment(task_kind)
        parent = self.get_or_create_parent_run(
            experiment_id, run_id, self.parent_tags(state, plan)
        )
        child_tags = self.child_tags(
            state,
            revision,
            outcome,
            payload,
            criteria_passed=all_required_passed,
            criteria_score=score,
        )
        child = self.get_or_create_child_run(
            experiment_id, parent.info.run_id, run_id, revision.revision, child_tags
        )
        child_run_id = child.info.run_id

        # Tags first — if artifact upload fails midway, the run is still identifiable and
        # joinable (MLOPS.md §4.4's ordering rule). Re-applied even on a reused run, so a
        # retry's fresher values win.
        for key, value in child_tags.items():
            self.client.set_tag(child_run_id, key, value)

        params = self._log_params(child_run_id, payload)
        metrics = self._log_metrics(child_run_id, payload, state, outcome, score)
        self._log_code_and_logs(child_run_id, workdir)
        self._log_declared_artifacts(child_run_id, outcome)
        model_relpath = self._log_model_by_flavor(
            child_run_id, payload, workdir / "artifacts"
        )

        registered_model: str | None = None
        model_version: str | None = None
        if (
            model_relpath is not None
            and all_required_passed
            and settings.MLFLOW_REGISTRY_ENABLED
        ):
            registered = self._register_model(
                task_kind=task_kind,
                run_id=run_id,
                child_run_id=child_run_id,
                model_relpath=model_relpath,
                primary_metric=plan.primary_metric if plan else None,
                metrics=metrics,
                dataset_sha256=str((payload.get("dataset") or {}).get("sha256", "")),
            )
            if registered is not None:
                registered_model, model_version = registered

        self._promote_to_parent(parent.info.run_id, metrics, plan, all_required_passed)

        run_info = self.client.get_run(child_run_id)
        artifact_uri = getattr(run_info.info, "artifact_uri", "") or ""

        return MLflowRef(
            experiment_id=experiment_id,
            experiment_name=self.experiment_name(task_kind),
            run_id=child_run_id,
            parent_run_id=parent.info.run_id,
            artifact_uri=artifact_uri,
            ui_url=self.ui_url(experiment_id, child_run_id),
            logged_metrics=metrics,
            logged_params=params,
            registered_model=registered_model,
            model_version=model_version,
        )

    def _log_params(self, run_id: str, payload: dict[str, Any]) -> dict[str, str]:
        params = flatten_params(payload.get("params") or {})
        dataset = payload.get("dataset") or {}
        # The minimum reproducibility set, always logged (MLOPS.md §5.3).
        params.update(
            {
                "dataset_id": str(dataset.get("id", "")),
                "dataset_sha256": str(dataset.get("sha256", ""))[:16],
                "seed": str(dataset.get("seed", "")),
                "framework": str(payload.get("framework", "")),
                "split_strategy": str(
                    (dataset.get("split") or {}).get("strategy", "unknown")
                ),
            }
        )
        for key, value in params.items():
            if value == "":
                continue
            self.client.log_param(run_id, key, value)
        return params

    def _log_metrics(
        self,
        run_id: str,
        payload: dict[str, Any],
        state: AgentState,
        outcome: SandboxOutcome,
        score: float,
    ) -> dict[str, float]:
        for name, series in (payload.get("metric_series") or {}).items():
            for point in series:
                value = point.get("value")
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                ):
                    self.client.log_metric(
                        run_id, name, float(value), step=int(point.get("step", 0))
                    )

        metrics = _finite_metrics(payload.get("metrics") or {})
        baseline = _finite_metrics(payload.get("baseline") or {})
        metrics.update({f"baseline_{k}": v for k, v in baseline.items()})

        runtime = payload.get("runtime") or {}
        usage = state.get("usage")
        metrics.update(
            {
                "platform_train_seconds": float(
                    runtime.get("train_seconds", 0.0) or 0.0
                ),
                "platform_peak_rss_mb": float(runtime.get("peak_rss_mb", 0.0) or 0.0),
                "platform_sandbox_duration_ms": float(outcome.duration_ms),
                "platform_debug_iterations": float(state.get("debug_iterations") or 0),
                "platform_tokens_total": float(
                    (usage.tokens_in + usage.tokens_out) if usage else 0
                ),
                "platform_criteria_score": float(score),
            }
        )
        for key, value in metrics.items():
            self.client.log_metric(run_id, key, value)
        return metrics

    def _log_code_and_logs(self, run_id: str, workdir: Path) -> None:
        for relative, artifact_path in (
            ("main.py", "code"),
            ("stdout.log", "logs"),
            ("stderr.log", "logs"),
        ):
            path = workdir / relative
            if path.is_file():
                self.client.log_artifact(run_id, str(path), artifact_path=artifact_path)

    def _log_declared_artifacts(self, run_id: str, outcome: SandboxOutcome) -> None:
        """Upload every file `sandbox_exec` already discovered under `/artifacts`, at the
        same relative layout MLOPS.md §6 uses in both the run volume and the MLflow store."""
        for ref in outcome.artifacts:
            path = ref.get("abs_path")
            if not path or not Path(path).is_file():
                continue
            relative = Path(ref.get("path", ""))
            artifact_path = (
                str(relative.parent) if str(relative.parent) != "." else None
            )
            self.client.log_artifact(run_id, path, artifact_path=artifact_path)

    # ------------------------------------------------------------------------------
    #  Flavor-aware model logging (MLOPS.md §7.1)
    # ------------------------------------------------------------------------------

    def _log_model_by_flavor(
        self, run_id: str, payload: dict[str, Any], artifacts_dir: Path
    ) -> str | None:
        """Build and upload an `MLmodel` descriptor; return its artifact-relative path.

        Returns `None` when no flavor was declared, or a descriptor could not be built —
        the model bytes are still uploaded (by `_log_declared_artifacts`, from the same
        `metrics.json.artifacts` entry), just as a plain file, matching the `(absent)` row
        of MLOPS.md §7.1's table: "logged as a plain file, not registrable."
        """
        flavored = [
            a
            for a in (payload.get("artifacts") or [])
            if a.get("flavor") and a.get("path")
        ]
        if not flavored:
            return None
        entry = flavored[0]  # one model per attempt is the documented case
        entry_path = Path(entry["path"])
        if entry_path.parts[0] != "model":
            logger.info(
                "Model artifact '%s' for run %s is not under model/; logging as a plain "
                "artifact only.",
                entry["path"],
                run_id,
            )
            return None

        signature = _read_json(artifacts_dir / "model" / "signature.json")
        if signature is None:
            logger.info(
                "No model/signature.json for run %s; model logged as a plain artifact, "
                "not registrable (MLOPS.md §7.1).",
                run_id,
            )
            return None

        descriptor_dir = artifacts_dir / "model"
        try:
            self._model_descriptor_writer(
                descriptor_dir,
                flavor=entry["flavor"],
                model_file=entry_path.name,
                signature=signature,
            )
            self.client.log_artifact(
                run_id, str(descriptor_dir / "MLmodel"), artifact_path="model"
            )
        except Exception as exc:  # noqa: BLE001 - never unpickles, never fails the run
            logger.warning(
                "Could not build an MLmodel descriptor for run %s: %s", run_id, exc
            )
            return None
        return "model"

    @staticmethod
    def _write_mlmodel_descriptor(
        model_dir: Path, *, flavor: str, model_file: str, signature: dict[str, Any]
    ) -> None:
        """Construct the `MLmodel` YAML without ever loading the model object itself.

        `signature.json` (and `input_example.json`, referenced but not required here) are
        precomputed inside the sandbox by the `MetricsWriter.save_model()` helper — that is
        the whole point of those sidecars (MLOPS.md §7.1): signature inference works
        host-side with no unpickling.
        """
        from mlflow.models import Model
        from mlflow.models.signature import ModelSignature

        model = Model(artifact_path="model")
        model.add_flavor(flavor, model_file=model_file)
        model.add_flavor(
            "python_function",
            loader_module=_FLAVOR_LOADER_MODULES.get(flavor, "mlflow.pyfunc"),
            data=model_file,
        )
        model.signature = ModelSignature.from_dict(signature)
        model_dir.mkdir(parents=True, exist_ok=True)
        model.save(str(model_dir / "MLmodel"))

    # ------------------------------------------------------------------------------
    #  Model registry (MLOPS.md §7.2–7.4)
    # ------------------------------------------------------------------------------

    def _register_model(
        self,
        *,
        task_kind: str,
        run_id: str,
        child_run_id: str,
        model_relpath: str,
        primary_metric: str | None,
        metrics: dict[str, float],
        dataset_sha256: str,
    ) -> tuple[str, str] | None:
        name = f"pluton-{task_kind}"
        try:
            self.client.get_registered_model(name)
        except Exception:  # noqa: BLE001 - "not found" is the expected first-model case
            try:
                self.client.create_registered_model(name)
            except Exception as exc:  # noqa: BLE001 - registry write must never fail the run
                logger.warning("Could not create registered model '%s': %s", name, exc)
                return None

        model_uri = f"runs:/{child_run_id}/{model_relpath}"
        try:
            model_version = self.client.create_model_version(
                name=name, source=model_uri, run_id=child_run_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not register a model version for run %s: %s", run_id, exc
            )
            return None

        version = str(model_version.version)
        self.client.set_registered_model_alias(name, "candidate", version)
        promoted = False
        if primary_metric and primary_metric in metrics:
            promoted = self._maybe_promote_champion(
                name, version, primary_metric, metrics[primary_metric], dataset_sha256
            )

        description = (
            f"Produced by Pluton run {run_id} (attempt {child_run_id}).\n"
            f"Dataset sha256: {dataset_sha256[:16]}\n"
        )
        if primary_metric and primary_metric in metrics:
            description += f"{primary_metric}: {metrics[primary_metric]:.4f}\n"
        if promoted:
            description += "Promoted to @champion.\n"
        self.client.update_model_version(name, version, description=description)
        self.client.set_model_version_tag(name, version, "pluton.run_id", run_id)
        self.client.set_model_version_tag(
            name, version, "pluton.dataset.sha256", dataset_sha256[:16]
        )
        return name, version

    def _maybe_promote_champion(
        self, name: str, version: str, metric: str, value: float, dataset_sha256: str
    ) -> bool:
        """Promote to `@champion` only on a clear improvement over the incumbent.

        The `CHAMPION_MARGIN` is load-bearing (MLOPS.md §7.3): without it, run-to-run noise
        flips the champion on every attempt and the alias stops meaning anything.
        Promotion also requires the same dataset — comparing a metric across different data
        is meaningless, and skipping that comparison silently would be worse than not
        promoting at all.
        """
        try:
            champion = self.client.get_model_version_by_alias(name, "champion")
        except Exception:  # noqa: BLE001 - no champion yet is the expected first-model case
            self.client.set_registered_model_alias(name, "champion", version)
            return True

        champion_run = self.client.get_run(champion.run_id)
        champion_metrics = getattr(champion_run.data, "metrics", None) or {}
        champion_params = getattr(champion_run.data, "params", None) or {}
        champion_value = champion_metrics.get(metric)
        if champion_value is None:
            return False

        champion_dataset = champion_params.get("dataset_sha256")
        if (
            dataset_sha256
            and champion_dataset
            and champion_dataset != dataset_sha256[:16]
        ):
            return False

        higher_is_better = metric not in LOWER_IS_BETTER
        improved = (
            value > champion_value * (1 + CHAMPION_MARGIN)
            if higher_is_better
            else value < champion_value * (1 - CHAMPION_MARGIN)
        )
        if improved:
            self.client.set_registered_model_alias(name, "champion", version)
            self.client.set_model_version_tag(
                name, version, "pluton.promoted_from", str(champion.version)
            )
            return True
        return False

    def _promote_to_parent(
        self,
        parent_run_id: str,
        metrics: dict[str, float],
        plan: Plan | None,
        all_required_passed: bool,
    ) -> None:
        """Copy this attempt's metrics onto the parent with a `final_` prefix (MLOPS.md
        §4.1). Without it, the MLflow experiment table — which shows parent runs by
        default — has empty metric columns and every comparison requires expanding
        children.

        Phase 4 has no `evaluator` yet to pick "the accepted attempt" across a REFINE loop,
        so `all_required_passed` on this attempt is the proxy; the promotion is safe to
        re-run since it is simply the newest CLEAN, criteria-passing attempt overwriting
        the previous one.
        """
        if not all_required_passed:
            return
        for key, value in metrics.items():
            if key.startswith("platform_") or key.startswith("baseline_"):
                continue
            self.client.log_metric(parent_run_id, f"final_{key}", value)
        primary_metric = plan.primary_metric if plan else None
        if primary_metric and primary_metric in metrics:
            self.client.log_metric(
                parent_run_id, "primary_metric", metrics[primary_metric]
            )

    # ------------------------------------------------------------------------------
    #  Backfill (MLOPS.md §11) — reconstruct an attempt from the run volume
    # ------------------------------------------------------------------------------

    def log_from_disk(
        self, run_id: str, run_dir: Path, *, task_kind: str, revision: int
    ) -> MLflowRef:
        """Re-log an attempt whose `experiments` row has `mlflow_run_id IS NULL`.

        Reconstructs the same `AgentState`-shaped inputs `log_attempt` needs from what
        `sandbox_exec` already wrote under `run_dir`, so the backfill path and the live
        path share every line of logging logic instead of duplicating it. The original
        `Plan` is not persisted to disk in this phase, so criteria (and therefore registry
        promotion) cannot be recomputed here — the metrics, params, tags and artifacts,
        which are what backfill exists to recover, are unaffected by that gap.
        """
        from app.services.sandbox import enumerate_artifacts, sha256_text

        revision_dir = run_dir / f"rev-{revision:03d}"
        artifacts_dir = revision_dir / "artifacts"
        metrics_path = artifacts_dir / "metrics.json"
        if not metrics_path.is_file():
            raise MLflowServiceError(
                f"{metrics_path} does not exist; nothing to backfill."
            )

        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        code_path = revision_dir / "main.py"
        code_content = (
            code_path.read_text(encoding="utf-8") if code_path.is_file() else ""
        )

        reconstructed: AgentState = {
            "run_id": run_id,
            "task_id": run_id,
            "task_kind": task_kind,
            "plan": None,
            "model_routing": {},
            "metadata": {},
            "debug_iterations": 0,
            "replan_count": 0,
            # An empty `Usage`, not None: the channel is required and `merge_usage`
            # folds into it, so a None here would break the first reduction.
            "usage": Usage(),
            "current_revision": CodeRevision(
                revision=revision,
                content=code_content,
                sha256=sha256_text(code_content),
            ),
            "last_outcome": SandboxOutcome(
                execution_id=uuid.uuid4(),
                profile="train",
                classification="CLEAN",
                exit_code=0,
                duration_ms=0,
                metrics=payload,
                artifacts=[
                    ref.model_dump() for ref in enumerate_artifacts(artifacts_dir)
                ],
                validation=ValidationReport(passed=True),
                revision=revision,
            ),
        }
        return self.log_attempt(reconstructed, workdir=revision_dir)


__all__ = [
    "CHAMPION_MARGIN",
    "LOWER_IS_BETTER",
    "MLflowService",
    "MLflowServiceError",
    "flatten_params",
]
