"""SandboxExecution ORM model — one row per container launch (ARCHITECTURE.md §7.1).

Durable telemetry for a single `sandbox_exec` container run: which profile, which pinned
image, how it exited, how long it took, how much memory it used. `SandboxOutcome`
(`engine/state.py`) is the checkpointed, in-graph twin of this row — this table is where
that telemetry becomes queryable across runs once persistence is wired into `sandbox_exec`.

**Deviation from the normative schema**, for the same reason as `Experiment`
(see `app/db/models/experiment.py`): ARCHITECTURE.md §7.1 keys this table off `runs.id` and
`run_steps.id`, neither of which exists in this codebase yet. `task_id` is the foreign key,
matching every other table in this package; `run_id` carries the engine's own run identity,
and `step_id` carries the Plan step's string id (`PlanStep.id`, e.g. `"s2"`) rather than a
foreign key to the not-yet-built `run_steps`.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.task import Task


class SandboxExecution(Base):
    """One sandboxed container execution, mirroring `SandboxResult`/`SandboxOutcome`."""

    __tablename__ = "sandbox_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the execution row.",
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
        comment="The engine's run identity (AgentState.run_id).",
    )
    step_id: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="The Plan step's string id this execution served (PlanStep.id, e.g. 's2').",
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="The code revision that was executed.",
    )
    profile: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Sandbox profile: exec, train, or train-tracked.",
    )
    image_digest: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="The resolved (pinned, if digests.json exists) image reference actually run.",
    )
    container_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Docker container id, for correlation with daemon logs.",
    )
    exit_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Process exit code; NULL only when the container never started (rejected).",
    )
    timed_out: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Killed for exceeding its wall clock.",
    )
    oom_killed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Killed for exceeding its memory limit.",
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Wall-clock execution time in milliseconds."
    )
    max_rss_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Peak resident memory sampled during execution.",
    )
    cpu_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="CPU time consumed, in milliseconds."
    )
    stdout_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Path to the captured stdout log on the run volume.",
    )
    stderr_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Path to the captured stderr log on the run volume.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="UTC timestamp this execution was recorded.",
    )

    task: Mapped["Task"] = relationship("Task")

    def __repr__(self) -> str:
        return (
            f"<SandboxExecution run_id={self.run_id} revision={self.revision} "
            f"profile={self.profile} exit_code={self.exit_code}>"
        )
