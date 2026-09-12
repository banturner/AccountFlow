"""
Tenant settings and stats endpoints (all scoped to the authenticated tenant).

GET  /api/settings/profile     — get tenant profile
PUT  /api/settings/profile     — update name
GET  /api/settings/plan        — current plan + usage stats
GET  /api/settings/automation  — auto-send preferences
PUT  /api/settings/automation  — enable/disable auto-send, set threshold
GET  /api/emails               — processed email thread log (paginated)
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant
from app.config import get_settings
from app.database import get_db
from app.models.tenant import Tenant
from app.schemas.email_thread import EmailThreadListResponse, EmailThreadOut

router = APIRouter()
settings = get_settings()


class TenantProfileOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    plan: str
    is_active: bool
    model_config = {"from_attributes": True}


class TenantProfileUpdate(BaseModel):
    name: Optional[str] = None


class PlanUsageOut(BaseModel):
    plan: str
    monthly_email_count: int
    monthly_cap: int
    cap_remaining: int


class AutomationOut(BaseModel):
    auto_send_enabled: bool
    auto_send_threshold: float


class AutomationUpdate(BaseModel):
    auto_send_enabled: Optional[bool] = None
    auto_send_threshold: Optional[float] = Field(default=None, ge=0.5, le=1.0)


@router.get("/settings/profile", response_model=TenantProfileOut, tags=["Settings"])
async def get_profile(tenant: Tenant = Depends(get_current_tenant)):
    return tenant


@router.put("/settings/profile", response_model=TenantProfileOut, tags=["Settings"])
async def update_profile(
    body: TenantProfileUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    if body.name:
        tenant.name = body.name.strip()
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get("/settings/automation", response_model=AutomationOut, tags=["Settings"])
async def get_automation(tenant: Tenant = Depends(get_current_tenant)):
    return AutomationOut(
        auto_send_enabled=tenant.auto_send_enabled,
        auto_send_threshold=tenant.auto_send_threshold,
    )


@router.put("/settings/automation", response_model=AutomationOut, tags=["Settings"])
async def update_automation(
    body: AutomationUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Auto-send is opt-in (pilot-safe default: every draft is reviewed)."""
    if body.auto_send_enabled is not None:
        tenant.auto_send_enabled = body.auto_send_enabled
    if body.auto_send_threshold is not None:
        tenant.auto_send_threshold = body.auto_send_threshold
    await db.commit()
    await db.refresh(tenant)
    return AutomationOut(
        auto_send_enabled=tenant.auto_send_enabled,
        auto_send_threshold=tenant.auto_send_threshold,
    )


@router.get("/settings/plan", response_model=PlanUsageOut, tags=["Settings"])
async def get_plan_usage(tenant: Tenant = Depends(get_current_tenant)):
    cap = settings.plan_caps.get(tenant.plan, 500)
    return PlanUsageOut(
        plan=tenant.plan,
        monthly_email_count=tenant.monthly_email_count,
        monthly_cap=cap,
        cap_remaining=max(0, cap - tenant.monthly_email_count),
    )


@router.get("/emails", response_model=EmailThreadListResponse, tags=["Emails"])
async def list_email_threads(
    thread_status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.models.email_thread import EmailThread

    q = select(EmailThread).where(EmailThread.tenant_id == tenant.id)
    if thread_status:
        q = q.where(EmailThread.status == thread_status)

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar() or 0

    q = q.order_by(EmailThread.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    threads = result.scalars().all()

    return EmailThreadListResponse(items=threads, total=total, page=page, page_size=page_size)
