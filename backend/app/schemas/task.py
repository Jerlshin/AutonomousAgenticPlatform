import uuid
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from app.db.models.task import TaskStatus

# validates user input when submitting POST
class TaskCreate(BaseModel):
    """Payload schema for submitting a new agent workflow task."""

    title: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="Concise descriptive title for the task.",
        example="Train PyTorch MNIST Model",
    )
    prompt: str = Field(
        ...,
        min_length=5,
        description="Detailed prompt outlining instructions for the agent network.",
        example="Research MNIST architectures, write training script, and log metrics to MLflow.",
    )

# Used internally by workers and route handlers when updating and existing task record
class TaskUpdate(BaseModel):
    """Schema for updating task status or final results."""

    status: Optional[TaskStatus] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class TaskRead(BaseModel):
    """Response schema returning full task details."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    prompt: str
    status: TaskStatus
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Paginated task list response."""

    total: int
    tasks: list[TaskRead]