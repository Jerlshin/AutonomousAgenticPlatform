"""Task ORM model representing user-submitted AI agent research jobs.

The execution parents. manages the high-level lifecycle, input prompt, system status, and final JSON payload of an agent job.
Manages the lifecycle of a user-submitted AI research job.
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum as SQLEnum, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.artifact import Artifact
    from app.db.models.log import AgentLog


# execution status
class TaskStatus(
    enum.StrEnum
):  # `StrEnum`, matching every other enum in engine/state.py
    """Lifecycle status states for multi-agent task execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # A run whose worker died: ARCHITECTURE.md §5.3's `INTERRUPTED`. Distinct from FAILED
    # because it is the one non-terminal state — every node boundary is checkpointed, so
    # `POST /runs/{id}/resume` replays at most one node. Written only by
    # `reap_interrupted_runs` (`app/worker/cron.py`), which detects it as `RUNNING` with
    # no `lock:run:{id}` in Redis.
    INTERRUPTED = "INTERRUPTED"


# status - primary table schema
class Task(Base):
    """Represents an autonomous multi-agent task workflow execution."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = (
        mapped_column(  # Generates unique 128-bit UUID primary key to safely identify execution jobs across distributed workers.
            primary_key=True,
            default=uuid.uuid4,
            comment="Unique identifier for the task job.",
        )
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Concise descriptive title for the task.",
    )
    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Original user prompt or query submitted to the platform.",
    )
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus, name="task_status_enum"),
        default=TaskStatus.PENDING,
        nullable=False,
        index=True,
        comment="Current execution state of the task.",
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Final JSON result payload returned by the multi-agent graph.",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Captured stack trace or error message if execution fails.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="Timestamp when the task was initially submitted.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
        comment="Timestamp when the task record was last modified.",
    )

    # Cascade deletes logs and generated artifacts when a task is deleted
    logs: Mapped[list["AgentLog"]] = relationship(
        "AgentLog",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        "Artifact",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Task id={self.id} title='{self.title}' status={self.status}>"
