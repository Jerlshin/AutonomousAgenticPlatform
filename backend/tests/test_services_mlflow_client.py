"""`MLflowService` — run hierarchy, tags, params, metrics, registry (MLOPS.md §4, §7).

Driven entirely against `FakeMlflowClient`: no MLflow server, and no `mlflow` package,
is required to exercise the logging sequence, the parent/child hierarchy, or model
registry promotion — matching how the sandbox driver and vector store are faked elsewhere
in this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.state import (
    CodeRevision,
    Plan,
    SandboxOutcome,
    SuccessCriterion,
    Usage,
    ValidationReport,
)
from app.services.mlflow_client import MLflowService, flatten_params
from tests.fakes import FakeMlflowClient

RUN_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"

METRICS_PAYLOAD = {
    "schema_version": "1.0",
    "task_kind": "tabular-classification",
    "framework": "scikit-learn",
    "dataset": {
        "id": "sklearn.breast_cancer",
        "sha256": "a" * 64,
        "n_samples": 569,
        "seed": 42,
        "split": {"strategy": "stratified-holdout-80-20"},
    },
    "params": {"estimator": "LogisticRegression", "C": 1.0},
    "metrics": {"accuracy": 0.97, "f1_macro": 0.95},
    "runtime": {"train_seconds": 1.8, "peak_rss_mb": 300.0},
}


def make_plan(*, threshold: float = 0.9) -> Plan:
    return Plan(
        steps=[],
        success_criteria=[
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=threshold
            )
        ],
        task_kind="tabular-classification",
        primary_metric="accuracy",
    )


def make_workdir(tmp_path: Path, *, metrics: dict | None = None) -> Path:
    workdir = tmp_path / "rev-001"
    artifacts_dir = workdir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (workdir / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (workdir / "stdout.log").write_text("training complete\n", encoding="utf-8")
    (workdir / "stderr.log").write_text("", encoding="utf-8")
    payload = metrics if metrics is not None else METRICS_PAYLOAD
    (artifacts_dir / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
    return workdir


def make_outcome(
    *, metrics: dict, artifacts: list[dict] | None = None
) -> SandboxOutcome:
    import uuid

    return SandboxOutcome(
        execution_id=uuid.uuid4(),
        profile="train",
        classification="CLEAN",
        exit_code=0,
        duration_ms=1234,
        metrics=metrics,
        artifacts=artifacts or [],
        validation=ValidationReport(passed=True),
        revision=1,
    )


def make_state(
    *,
    workdir: Path,
    metrics: dict | None = None,
    plan: Plan | None = None,
    revision: int = 1,
) -> dict:
    payload = metrics if metrics is not None else METRICS_PAYLOAD
    metrics_json_path = workdir / "artifacts" / "metrics.json"
    artifact_refs = [
        {
            "path": "metrics.json",
            "abs_path": str(metrics_json_path),
            "artifact_type": "metrics",
            "sha256": "0" * 64,
            "size_bytes": metrics_json_path.stat().st_size,
            "mime_type": "application/json",
        }
    ]
    return {
        "run_id": RUN_ID,
        "task_id": RUN_ID,
        "task_kind": "tabular-classification",
        "plan": plan if plan is not None else make_plan(),
        "model_routing": {
            "coder": "qwen2.5-coder:7b",
            "planner": "qwen2.5:14b-instruct",
        },
        "metadata": {"prompt_version_coder": "1.0.0", "seed": 42},
        "debug_iterations": 0,
        "replan_count": 0,
        "usage": Usage(tokens_in=100, tokens_out=50),
        "current_revision": CodeRevision(
            revision=revision, content="x = 1\n", sha256="1" * 64
        ),
        "last_outcome": make_outcome(metrics=payload, artifacts=artifact_refs),
    }


@pytest.fixture
def client() -> FakeMlflowClient:
    return FakeMlflowClient()


def fake_model_descriptor_writer(
    model_dir: Path, *, flavor: str, model_file: str, signature: dict
) -> None:
    """A `model_descriptor_writer` double: writes a placeholder `MLmodel` file without
    ever importing `mlflow.models`, so the registration flow is testable without the real
    `mlflow` package installed — mirroring `client` being a `FakeMlflowClient`."""
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "MLmodel").write_text(f"flavor: {flavor}\nmodel_file: {model_file}\n")


@pytest.fixture
def service(client: FakeMlflowClient) -> MLflowService:
    return MLflowService(
        client=client, model_descriptor_writer=fake_model_descriptor_writer
    )


class TestFlattenParams:
    def test_nested_dicts_become_dotted_keys(self):
        assert flatten_params({"model": {"hidden_dim": 64}}) == {
            "model.hidden_dim": "64"
        }

    def test_a_list_is_stored_as_its_json_string(self):
        assert flatten_params({"C_grid": [0.01, 0.1, 1, 10]}) == {
            "C_grid": "[0.01, 0.1, 1, 10]"
        }

    def test_none_values_are_dropped(self):
        assert flatten_params({"early_stopping": None}) == {}

    def test_long_values_are_truncated_with_a_hash_suffix(self):
        flat = flatten_params({"blob": "x" * 1000})
        assert len(flat["blob"]) <= 500
        assert "…" in flat["blob"]


class TestResolveExperiment:
    def test_a_new_experiment_is_created_once(self, service: MLflowService):
        first = service.resolve_experiment("tabular-classification")
        second = service.resolve_experiment("tabular-classification")
        assert first == second

    def test_different_task_kinds_get_different_experiments(
        self, service: MLflowService
    ):
        a = service.resolve_experiment("tabular-classification")
        b = service.resolve_experiment("tabular-regression")
        assert a != b

    def test_a_soft_deleted_experiment_is_restored_not_recreated(
        self, service: MLflowService, client: FakeMlflowClient
    ):
        exp_id = service.resolve_experiment("tabular-classification")
        client._experiments[exp_id].lifecycle_stage = "deleted"
        restored = service.resolve_experiment("tabular-classification")
        assert restored == exp_id
        assert client._experiments[exp_id].lifecycle_stage == "active"


class TestLogAttempt:
    def test_a_clean_attempt_is_logged_with_tags_params_and_metrics(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir = make_workdir(tmp_path)
        ref = service.log_attempt(make_state(workdir=workdir), workdir=workdir)

        child = client.runs[ref.run_id]
        assert child.data.tags["pluton.run_id"] == RUN_ID
        assert child.data.tags["pluton.attempt"] == "1"
        assert child.data.tags["pluton.criteria_passed"] == "true"
        assert child.data.params["dataset_id"] == "sklearn.breast_cancer"
        assert child.data.params["estimator"] == "LogisticRegression"
        assert child.data.metrics["accuracy"] == 0.97
        assert child.data.metrics["platform_sandbox_duration_ms"] == 1234.0
        assert ref.parent_run_id in client.runs
        assert ref.parent_run_id != ref.run_id

    def test_the_parent_run_is_named_from_the_run_id(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir = make_workdir(tmp_path)
        ref = service.log_attempt(make_state(workdir=workdir), workdir=workdir)
        parent = client.runs[ref.parent_run_id]
        assert parent.data.tags["mlflow.runName"] == f"run-{RUN_ID[:8]}"

    def test_passing_required_criteria_promotes_final_metrics_onto_the_parent(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir = make_workdir(tmp_path)
        ref = service.log_attempt(make_state(workdir=workdir), workdir=workdir)
        parent = client.runs[ref.parent_run_id]
        assert parent.data.metrics["final_accuracy"] == 0.97
        assert parent.data.metrics["primary_metric"] == 0.97

    def test_missing_a_required_criterion_does_not_promote_onto_the_parent(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir = make_workdir(tmp_path)
        state = make_state(workdir=workdir, plan=make_plan(threshold=0.999))
        ref = service.log_attempt(state, workdir=workdir)
        parent = client.runs[ref.parent_run_id]
        assert "final_accuracy" not in parent.data.metrics

    def test_a_second_attempt_for_the_same_run_reuses_the_parent_but_not_the_child(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir1 = make_workdir(tmp_path / "a")
        first = service.log_attempt(
            make_state(workdir=workdir1, revision=1), workdir=workdir1
        )

        workdir2 = make_workdir(tmp_path / "b")
        second = service.log_attempt(
            make_state(workdir=workdir2, revision=2), workdir=workdir2
        )

        assert second.parent_run_id == first.parent_run_id
        assert second.run_id != first.run_id

    def test_re_logging_the_same_attempt_reuses_the_same_child_run(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        """Idempotency on resume (AGENTS.md §10): looked up by run_id + attempt tags."""
        workdir = make_workdir(tmp_path)
        first = service.log_attempt(make_state(workdir=workdir), workdir=workdir)
        second = service.log_attempt(make_state(workdir=workdir), workdir=workdir)
        assert first.run_id == second.run_id
        assert len(client.runs) == 2  # one parent, one child — no duplicates

    def test_code_and_logs_are_uploaded_as_artifacts(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir = make_workdir(tmp_path)
        ref = service.log_attempt(make_state(workdir=workdir), workdir=workdir)
        uploaded = [
            (path, dest)
            for run_id, path, dest in client.artifacts
            if run_id == ref.run_id
        ]
        assert (str(workdir / "main.py"), "code") in uploaded
        assert (str(workdir / "stdout.log"), "logs") in uploaded

    def test_nan_metrics_are_never_forwarded_to_mlflow(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        payload = {**METRICS_PAYLOAD, "metrics": {"accuracy": float("nan")}}
        workdir = make_workdir(tmp_path, metrics=payload)
        ref = service.log_attempt(
            make_state(workdir=workdir, metrics=payload), workdir=workdir
        )
        assert "accuracy" not in client.runs[ref.run_id].data.metrics

    def test_raises_when_there_is_nothing_clean_to_log(
        self, service: MLflowService, tmp_path: Path
    ):
        from app.services.mlflow_client import MLflowServiceError

        with pytest.raises(MLflowServiceError):
            service.log_attempt(
                {"last_outcome": None, "current_revision": None}, workdir=tmp_path
            )


class TestModelRegistration:
    def _state_with_model(
        self, tmp_path: Path, *, accuracy: float
    ) -> tuple[dict, Path]:
        workdir = make_workdir(tmp_path)
        artifacts_dir = workdir / "artifacts"
        model_dir = artifacts_dir / "model"
        model_dir.mkdir()
        (model_dir / "model.joblib").write_bytes(b"not a real pickle, never loaded")
        (model_dir / "signature.json").write_text(
            json.dumps({"inputs": "[]", "outputs": "[]"}), encoding="utf-8"
        )
        payload = {**METRICS_PAYLOAD, "metrics": {"accuracy": accuracy}}
        payload["artifacts"] = [
            {"path": "model/model.joblib", "type": "model", "flavor": "sklearn"}
        ]
        metrics_json_path = artifacts_dir / "metrics.json"
        metrics_json_path.write_text(json.dumps(payload), encoding="utf-8")

        state = make_state(workdir=workdir, metrics=payload)
        state["last_outcome"] = make_outcome(
            metrics=payload,
            artifacts=[
                {
                    "path": "model/model.joblib",
                    "abs_path": str(model_dir / "model.joblib"),
                    "artifact_type": "model",
                    "sha256": "0" * 64,
                    "size_bytes": 10,
                    "mime_type": "application/octet-stream",
                },
                {
                    "path": "metrics.json",
                    "abs_path": str(metrics_json_path),
                    "artifact_type": "metrics",
                    "sha256": "0" * 64,
                    "size_bytes": metrics_json_path.stat().st_size,
                    "mime_type": "application/json",
                },
            ],
        )
        return state, workdir

    def test_a_model_without_a_signature_sidecar_is_not_registered(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        workdir = make_workdir(tmp_path)
        artifacts_dir = workdir / "artifacts"
        model_dir = artifacts_dir / "model"
        model_dir.mkdir()
        (model_dir / "model.joblib").write_bytes(b"opaque")
        payload = {**METRICS_PAYLOAD}
        payload["artifacts"] = [
            {"path": "model/model.joblib", "type": "model", "flavor": "sklearn"}
        ]
        state = make_state(workdir=workdir, metrics=payload)
        ref = service.log_attempt(state, workdir=workdir)
        assert ref.registered_model is None
        assert not client.registered_models

    def test_the_first_model_becomes_champion_and_candidate(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        state, workdir = self._state_with_model(tmp_path, accuracy=0.95)
        ref = service.log_attempt(state, workdir=workdir)

        assert ref.registered_model == "pluton-tabular-classification"
        assert ref.model_version == "1"
        entry = client.registered_models["pluton-tabular-classification"]
        assert entry["aliases"]["champion"] == "1"
        assert entry["aliases"]["candidate"] == "1"

    def test_a_clearly_better_challenger_is_promoted_to_champion(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        first_state, first_workdir = self._state_with_model(
            tmp_path / "a", accuracy=0.90
        )
        service.log_attempt(first_state, workdir=first_workdir)

        second_state, second_workdir = self._state_with_model(
            tmp_path / "b", accuracy=0.99
        )
        second_state["current_revision"] = CodeRevision(
            revision=2, content="x = 2\n", sha256="2" * 64
        )
        second_state["last_outcome"] = second_state["last_outcome"].model_copy(
            update={"revision": 2}
        )
        ref = service.log_attempt(second_state, workdir=second_workdir)

        entry = client.registered_models["pluton-tabular-classification"]
        assert entry["aliases"]["champion"] == ref.model_version
        assert entry["versions"][ref.model_version].tags["pluton.promoted_from"] == "1"

    def test_a_marginal_improvement_within_the_noise_margin_does_not_promote(
        self, service: MLflowService, client: FakeMlflowClient, tmp_path: Path
    ):
        first_state, first_workdir = self._state_with_model(
            tmp_path / "a", accuracy=0.900
        )
        service.log_attempt(first_state, workdir=first_workdir)

        second_state, second_workdir = self._state_with_model(
            tmp_path / "b", accuracy=0.901
        )
        second_state["current_revision"] = CodeRevision(
            revision=2, content="x = 2\n", sha256="2" * 64
        )
        second_state["last_outcome"] = second_state["last_outcome"].model_copy(
            update={"revision": 2}
        )
        service.log_attempt(second_state, workdir=second_workdir)

        entry = client.registered_models["pluton-tabular-classification"]
        assert entry["aliases"]["champion"] == "1"
