import uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.db.models.task import Task
from app.schemas.common import StandardResponse
from app.schemas.task import TaskCreate, TaskListResponse, TaskRead, TaskUpdate

router = APIRouter()


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED, summary="Submit Task")
async def create_task(
    payload: TaskCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Submit a new multi-agent research task."""
    task = Task(
        title=payload.title,
        prompt=payload.prompt,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@router.get("", response_model=TaskListResponse, summary="List Tasks")
async def list_tasks(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Fetch paginated list of submitted tasks."""
    total_query = await db.execute(select(func.count(Task.id)))
    total = total_query.scalar_one()

    query = select(Task).order_by(Task.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    tasks = result.scalars().all()

    return TaskListResponse(total=total, tasks=tasks)


@router.get("/{task_id}", response_model=TaskRead, summary="Get Task Details")
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Get status and result payload for a specific task."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )
    return task


@router.patch("/{task_id}", response_model=TaskRead, summary="Update Task Status")
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Update task lifecycle state, final result JSON, or error message."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(task, key, value)

    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/{task_id}", response_model=StandardResponse, summary="Delete Task")
async def delete_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Delete a task record along with all associated logs and artifacts."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found.",
        )

    await db.delete(task)
    await db.commit()
    return StandardResponse(message=f"Task {task_id} deleted successfully.")