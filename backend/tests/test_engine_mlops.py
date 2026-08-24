"""The `mlops` node in isolation (AGENTS.md §7.7).

The property this node exists to guarantee is in MLOPS.md §11: **MLflow being unreachable
must never fail a run.** These tests exercise that guarantee directly — a service whose
`log_attempt` raises, a session factory whose `commit` raises — and confirm the node still
returns a normal update rather than propagating, in addition to the ordinary happy path.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.engine.nodes.mlops import mlops_node
from app.engine.state import (
    CodeRevision,
    Plan,
    RunPhase,
    SandboxOutcome,
    SuccessCriterion,
    ValidationReport,
)
from app.services.mlflow_client import MLflowService, MLflowServiceError
from tests.fakes import FakeDbSessionFactory, FakeMlflowClient, FakeSandboxDriver, run

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
    },
    "params": {"estimator": "LogisticRegression"},
    "metrics": {"accuracy": 0.9737, "f1_macro": 0.9712},
}


def plan() -> Plan:
    return Plan(
        steps=[],
        success_criteria=[
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=0.9
            )
        ],
        task_kind="tabular-classification",
        primary_metric="accuracy",
    )


def outcome(*, metrics: dict | None = METRICS_PAYLOAD) -> SandboxOutcome:
    return SandboxOutcome(
        execution_id=uuid.uuid4(),
        profile="train",
        classification="CLEAN",
        exit_code=0,
        duration_ms=1500,
        metrics=metrics,
        artifacts=[],
        validation=ValidationReport(passed=True),
        revision=1,
    )


def base_state(**overrides) -> dict:
    state = {
        "run_id": RUN_ID,
        "task_id": RUN_ID,
        "task_kind": "tabular-classification",
        "plan": plan(),
        "model_routing": {},
        "metadata": {},
        "debug_iterations": 0,
        "replan_count": 0,
        "current_revision": CodeRevision(
            revision=1, content="print(1)\n", sha256="1" * 64
        ),
        "last_outcome": outcome(),
    }
    state.update(overrides)
    return state


class _RaisingMlflowService:
    """A service whose `log_attempt` always raises — an MLflow outage stand-in."""

    def log_attempt(self, *_args, **_kwargs):
        raise ConnectionError("mlflow is unreachable")


class _RaisingDbSessionFactory:
    """A `db_session_factory` whose session raises on commit."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    def add(self, _obj) -> None:
        return None

    async def commit(self) -> None:
        raise ConnectionError("postgres is unreachable")


def config(*, runs_root: Path, mlflow_service=None, db_session_factory=None) -> dict:
    return {
        "configurable": {
            "sandbox_driver": FakeSandboxDriver(runs_root),
            "mlflow_service": mlflow_service
            if mlflow_service is not None
            else MLflowService(client=FakeMlflowClient()),
            "db_session_factory": db_session_factory or FakeDbSessionFactory(),
        }
    }


class TestHappyPath:
    def test_the_attempt_is_logged_and_persisted(self, tmp_path):
        db_sessions = FakeDbSessionFactory()
        cfg = config(runs_root=tmp_path, db_session_factory=db_sessions)

        update = run(mlops_node(base_state(), cfg))

        assert update["phase"] is RunPhase.TRACK
        ref = update["mlflow"]
        assert ref.run_id
        assert update["mlflow_history"] == ref
        assert len(db_sessions.rows) == 1
        row = db_sessions.rows[0]
        assert row.run_id == RUN_ID
        assert row.mlflow_run_id == ref.run_id
        assert row.task_kind == "tabular-classification"
        assert row.metadata_json is None

    def test_reaching_mlops_with_nothing_to_log_degrades_without_writing_mlflow_state(
        self, tmp_path
    ):
        """`DEGRADE`: the fallback runs, `mlflow`/`mlflow_history` stay unwritten."""
        db_sessions = FakeDbSessionFactory()
        cfg = config(runs_root=tmp_path, db_session_factory=db_sessions)

        update = run(mlops_node(base_state(last_outcome=None), cfg))

        assert "mlflow" not in update
        assert db_sessions.rows == []


class TestMlflowOutage:
    def test_an_unreachable_mlflow_does_not_fail_the_run(self, tmp_path):
        db_sessions = FakeDbSessionFactory()
        cfg = config(
            runs_root=tmp_path,
            mlflow_service=_RaisingMlflowService(),
            db_session_factory=db_sessions,
        )

        update = run(mlops_node(base_state(), cfg))

        assert "mlflow" not in update
        assert "mlops_mlflow_error" in update["metadata"]

    def test_the_experiments_row_is_still_written_with_a_null_mlflow_run_id(
        self, tmp_path
    ):
        """MLOPS.md §11 — this row is what `mlflow_backfill` finds and retries."""
        db_sessions = FakeDbSessionFactory()
        cfg = config(
            runs_root=tmp_path,
            mlflow_service=_RaisingMlflowService(),
            db_session_factory=db_sessions,
        )

        run(mlops_node(base_state(), cfg))

        assert len(db_sessions.rows) == 1
        row = db_sessions.rows[0]
        assert row.mlflow_run_id is None
        assert "mlflow_error" in (row.metadata_json or {})


class TestPersistenceFailure:
    def test_a_postgres_outage_does_not_lose_the_mlflow_ref(self, tmp_path):
        """Persistence failing must not undo a successful MLflow logging call."""
        cfg = config(runs_root=tmp_path, db_session_factory=_RaisingDbSessionFactory())

        update = run(mlops_node(base_state(), cfg))

        assert update["mlflow"] is not None


def test_log_attempt_requires_a_clean_metrics_bearing_outcome():
    with pytest.raises(MLflowServiceError):
        MLflowService(client=FakeMlflowClient()).log_attempt(
            {"last_outcome": None, "current_revision": None}, workdir=Path(".")
        )
