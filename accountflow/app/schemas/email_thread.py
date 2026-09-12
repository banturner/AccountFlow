import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EmailThreadOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    gmail_thread_id: str
    sender_email: str
    sender_name: Optional[str]
    subject: Optional[str]
    ai_action: Optional[str]
    ai_confidence: Optional[float]
    ai_model_used: Optional[str]
    status: str
    received_at: Optional[datetime]
    processed_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class EmailThreadListResponse(BaseModel):
    items: list[EmailThreadOut]
    total: int
    page: int
    page_size: int
