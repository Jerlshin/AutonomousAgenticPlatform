"""Central export point for all SQLAlchemy ORM models.

Makes importing models across the application clean and provides a unified
target for Alembic migration autogeneration.
"""

from app.db.base import Base
from app.db.models.artifact import Artifact
from app.db.models.log import AgentLog
from app.db.models.task import Task, TaskStatus

__all__ = [
    "Base",
    "Task",
    "TaskStatus",
    "AgentLog",
    "Artifact",
]

"""
We have done SQLAlchemy ORM models. The next step is turning those python classes into real postgreSQL tables and building the data access layer.

1. Database Migration with Alembic
2. Pydantic Schemas
3. Async Database CRUD Operations
4. Core FastAPI API routes
"""
