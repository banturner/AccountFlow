"""
Task management endpoints.

GET   /api/tasks          — list tasks (paginated, filterable by status)
GET   /api/tasks/{id}     — get one task
PATCH /api/tasks/{id}     — update status / assignee / due_date
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant
from app.database import get_db
from app.models.task import Task
from app.models.tenant import Tenant
from app.schemas.task import TaskListResponse, TaskOut
from app.schemas.task import TaskUpdate as TaskUpdateSchema

router = APIRouter()


@router.get("/tasks", response_model=TaskListResponse, tags=["Tasks"])
async def list_tasks(
    task_status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    q = select(Task).where(Task.tenant_id == tenant.id)
    if task_status:
        q = q.where(Task.status == task_status)

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar() or 0

    q = q.order_by(Task.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    tasks = result.scalars().all()

    return TaskListResponse(items=tasks, total=total, page=page, page_size=page_size)


@router.get("/tasks/{task_id}", response_model=TaskOut, tags=["Tasks"])
async def get_task(
    task_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task or task.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/tasks/{task_id}", response_model=TaskOut, tags=["Tasks"])
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdateSchema,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task or task.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Task not found")

    if body.status is not None:
        valid_statuses = {"open", "in_progress", "completed", "cancelled"}
        if body.status not in valid_statuses:
            raise HTTPException(status_code=422, detail=f"status must be one of {valid_statuses}")
        task.status = body.status
    if body.assigned_to is not None:
        task.assigned_to = body.assigned_to
    if body.due_date is not None:
        task.due_date = body.due_date

    await db.commit()
    await db.refresh(task)
    return task
