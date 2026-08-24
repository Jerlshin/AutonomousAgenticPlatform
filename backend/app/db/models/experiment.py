"""Experiment ORM model — the durable cross-reference to MLflow (MLOPS.md §11, ARCHITECTURE.md §7.1).

One row per logged attempt (one MLflow child run). MLflow is the queryable index over
experiment results, deliberately *not* the system of record (MLOPS.md §1); this table is
part of what makes that true: a run's `params`/`metrics`/`tags` are mirrored here at write
time, and `mlflow_run_id` starts `NULL` and is healed by `mlflow_backfill`
(`app/worker/cron.py`) whenever the `mlops` node logged them here but MLflow itself was
unreachable. The row is written unconditionally, before MLflow is even attempted, so it is
never lost to a tracking-server outage the way an MLflow-only record would be.

**Deviation from the normative schema.** ARCHITECTURE.md §7.1 keys `experiments` off
`runs.id`, but this codebase has not yet built the `runs` table (`ARCHITECTURE.md` §6 marks
`runs.py` and its model `⬜`) — today a Task is a Run. `task_id` is therefore the foreign
key, matching the convention every other table in this package already uses
(`agent_logs.task_id`, `artifacts.task_id`). `run_id` is kept alongside it as a plain
string: it is the engine's own identity (`AgentState.run_id`, the MLflow tag
`pluton.run_id`, the run-volume directory name) and currently equals `task_id` because
`init_node` defaults one from the other — but the two are conceptually distinct, and this
column is what lets a future `runs` table absorb the FK without a data migration.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.task import Task


class Experiment(Base):
    """One MLflow child-run attempt, mirrored durably in PostgreSQL."""

    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the experiment row.",
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Foreign key referencing the owning Task (stands in for the not-yet-built Run).",
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="The engine's run identity (AgentState.run_id); the join key MLflow tags use.",
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="The code revision this attempt logs — MLflow child run `attempt-{revision:03d}`.",
    )
    task_kind: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="The Plan's task_kind — resolves the MLflow experiment `pluton/{task_kind}` "
        "on backfill, when mlflow_experiment_id is not yet known.",
    )
    mlflow_experiment_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="MLflow experiment id, once resolved.",
    )
    mlflow_run_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        index=True,
        comment="MLflow child run id. NULL means MLflow was unreachable when this attempt "
        "ran; mlflow_backfill retries rows where this is NULL (MLOPS.md §11).",
    )
    mlflow_parent_run_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="MLflow parent run id (`run-{run_id[:8]}`).",
    )
    artifact_uri: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="The child run's MLflow artifact URI, once logged.",
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Flattened, string-valued params as logged to MLflow (MLOPS.md §5.3).",
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Scalar metrics as logged to MLflow, from metrics.json plus platform metrics.",
    )
    tags: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="The fixed-allowlist tag set actually written (MLOPS.md §4.3).",
    )
    registered_model_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="`pluton-{task_kind}`, when this attempt's model was registered (MLOPS.md §7.2).",
    )
    registered_model_version: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="The registry version created for this attempt's model, if any.",
    )
    unrecoverable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Set by mlflow_backfill when the run volume was pruned before it could heal.",
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Backfill/failure bookkeeping: mlflow_error, artifact_upload_failed, reason.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="UTC timestamp this attempt was logged.",
    )

    task: Mapped["Task"] = relationship("Task")

    def __repr__(self) -> str:
        return (
            f"<Experiment run_id={self.run_id} revision={self.revision} "
            f"mlflow_run_id={self.mlflow_run_id}>"
        )
