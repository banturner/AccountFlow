import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DraftOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    thread_id: uuid.UUID
    to_email: str
    subject: Optional[str]
    body: str
    edited_body: Optional[str]
    status: str
    ai_confidence: Optional[float]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DraftApprove(BaseModel):
    edited_body: Optional[str] = None  # Override body before sending


class DraftReject(BaseModel):
    reason: Optional[str] = None


class DraftListResponse(BaseModel):
    items: list[DraftOut]
    total: int
    page: int
    page_size: int
