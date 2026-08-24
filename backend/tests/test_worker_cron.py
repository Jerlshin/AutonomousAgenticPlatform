"""`mlflow_backfill` — retry logging for attempts MLflow missed (MLOPS.md §8.4, §11).

Driven against fakes for both the database session and the `MLflowService`: this codebase
has no established pattern yet for testing code that opens a real `AsyncSession` (see
`tests/test_services_ingestion.py`'s docstring), so the session this job opens is faked the
same way the sandbox driver and vector store are faked elsewhere — enough of the real
`AsyncSession`/`MLflowService` surface to drive `mlflow_backfill`'s retry logic end to end.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from app.db.models.experiment import Experiment
from app.engine.state import MLflowRef
from app.worker.cron import BACKFILL_BATCH_LIMIT, _resolve_service, mlflow_backfill


def make_row(**overrides: Any) -> Experiment:
    base = dict(
        id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=str(uuid.uuid4()),
        revision=1,
        task_kind="tabular-classification",
        mlflow_run_id=None,
        params={},
        metrics={},
        tags={},
        unrecoverable=False,
        metadata_json=None,
    )
    base.update(overrides)
    return Experiment(**base)


class _FakeScalars:
    def __init__(self, rows: list[Experiment]) -> None:
        self._rows = rows

    def all(self) -> list[Experiment]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[Experiment]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows: list[Experiment]) -> None:
        self._rows = rows
        self.commits = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult(list(self._rows))

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc_info: Any) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, rows: list[Experiment]) -> None:
        self.rows = rows

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.rows)


class _FakeBackfillService:
    def __init__(
        self,
        *,
        refs: dict[str, MLflowRef] | None = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, Path, str, int]] = []
        self.refs = refs or {}
        self.raise_for = raise_for or set()

    def log_from_disk(
        self, run_id: str, run_dir: Path, *, task_kind: str, revision: int
    ) -> MLflowRef:
        self.calls.append((run_id, run_dir, task_kind, revision))
        if run_id in self.raise_for:
            raise ConnectionError("mlflow is still unreachable")
        return self.refs[run_id]


def make_ref(run_id: str) -> MLflowRef:
    return MLflowRef(
        experiment_id="exp-1",
        experiment_name="pluton/tabular-classification",
        run_id=run_id,
        parent_run_id="parent-1",
        artifact_uri="file:///fake/artifacts",
        ui_url="http://localhost:5001/#/experiments/exp-1/runs/" + run_id,
        logged_metrics={"accuracy": 0.97},
        logged_params={"dataset_id": "sklearn.breast_cancer"},
    )


@pytest.fixture(autouse=True)
def _runs_root(monkeypatch, tmp_path):
    monkeypatch.setattr("app.worker.cron.settings.RUNS_ROOT", str(tmp_path))
    return tmp_path


class TestResolveService:
    def test_an_injected_service_in_ctx_wins(self):
        sentinel = object()
        assert _resolve_service({"mlflow_service": sentinel}) is sentinel

    def test_no_ctx_falls_back_to_a_real_service(self):
        from app.services.mlflow_client import MLflowService

        assert isinstance(_resolve_service(None), MLflowService)


class TestMlflowBackfill:
    async def _run(self, rows, service, monkeypatch):
        monkeypatch.setattr(
            "app.worker.cron.AsyncSessionLocal", _FakeSessionFactory(rows)
        )
        return await mlflow_backfill({"mlflow_service": service})

    def test_a_recoverable_row_is_healed(self, monkeypatch, tmp_path):
        row = make_row()
        (tmp_path / row.run_id / "rev-001" / "artifacts").mkdir(parents=True)
        ref = make_ref("healed-run-1")
        service = _FakeBackfillService(refs={row.run_id: ref})

        healed = _await(self._run([row], service, monkeypatch))

        assert healed == 1
        assert row.mlflow_run_id == "healed-run-1"
        assert row.mlflow_experiment_id == ref.experiment_id
        assert row.artifact_uri == ref.artifact_uri
        assert row.metadata_json is None
        assert service.calls == [
            (row.run_id, tmp_path / row.run_id, row.task_kind, row.revision)
        ]

    def test_a_row_whose_run_directory_was_pruned_is_marked_unrecoverable(
        self, monkeypatch, tmp_path
    ):
        row = make_row()  # no directory created under tmp_path
        service = _FakeBackfillService()

        healed = _await(self._run([row], service, monkeypatch))

        assert healed == 0
        assert row.unrecoverable is True
        assert row.metadata_json["reason"] == "run directory pruned"
        assert service.calls == []

    def test_a_row_that_is_still_failing_is_left_for_the_next_tick(
        self, monkeypatch, tmp_path
    ):
        row = make_row()
        (tmp_path / row.run_id / "rev-001" / "artifacts").mkdir(parents=True)
        service = _FakeBackfillService(raise_for={row.run_id})

        healed = _await(self._run([row], service, monkeypatch))

        assert healed == 0
        assert row.mlflow_run_id is None
        assert row.unrecoverable is False

    def test_multiple_rows_in_one_tick_are_each_healed_independently(
        self, monkeypatch, tmp_path
    ):
        rows = [make_row() for _ in range(3)]
        for row in rows:
            (tmp_path / row.run_id / "rev-001" / "artifacts").mkdir(parents=True)
        service = _FakeBackfillService(
            refs={row.run_id: make_ref(f"healed-{i}") for i, row in enumerate(rows)}
        )

        healed = _await(self._run(rows, service, monkeypatch))

        assert healed == 3
        assert all(row.mlflow_run_id is not None for row in rows)

    def test_the_batch_limit_is_positive_and_bounded(self):
        """The limit passed to the DB query — asserted directly since the fake session
        used elsewhere in this module does not itself enforce SQL LIMIT clauses."""
        assert 0 < BACKFILL_BATCH_LIMIT <= 100


def _await(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
