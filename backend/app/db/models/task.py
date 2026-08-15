"""Task ORM model representing user-submitted AI agent research jobs.

The execution parents. manages the high-level lifecycle, input prompt, system status, and final JSON payload of an agent job.
Manages the lifecycle of a user-submitted AI research job.
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, Enum as SQLEnum, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.artifact import Artifact
    from app.db.models.log import AgentLog

# execution status
class TaskStatus(str, enum.Enum): # inherits from both str and enum.Enum
    """Lifecycle status states for multi-agent task execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

# status - primary table schema
class Task(Base):
    """Represents an autonomous multi-agent task workflow execution."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column( # Generates unique 128-bit UUID primary key to safely identify execution jobs across distributed workers.
        primary_key=True,
        default=uuid.uuid4,
        description="Unique identifier for the task job.",
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        description="Concise descriptive title for the task.",
    )
    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        description="Original user prompt or query submitted to the platform.",
    )
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus, name="task_status_enum"),
        default=TaskStatus.PENDING,
        nullable=False,
        index=True,
        description="Current execution state of the task.",
    )
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
        description="Final JSON result payload returned by the multi-agent graph.",
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        description="Captured stack trace or error message if execution fails.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        description="Timestamp when the task was initially submitted.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
        description="Timestamp when the task record was last modified.",
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
    