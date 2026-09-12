import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TaskOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    thread_id: Optional[uuid.UUID]
    title: str
    description: Optional[str]
    status: str
    due_date: Optional[datetime]
    assigned_to: Optional[str]
    google_event_id: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    due_date: Optional[datetime] = None


class TaskListResponse(BaseModel):
    items: list[TaskOut]
    total: int
    page: int
    page_size: int
