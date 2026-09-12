"""
Tenant management — INTERNAL admin endpoints (X-API-Key), never tenant-facing.

POST /api/tenants        — create a tenant (onboarding runbook step 1)
GET  /api/tenants        — list tenants (ops overview)
"""
import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin_key
from app.config import get_settings
from app.core.logging import get_logger
from app.core.passwords import PasswordPolicyError, hash_password
from app.database import get_db
from app.models.tenant import Tenant

router = APIRouter()
settings = get_settings()
log = get_logger(__name__)


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=12, max_length=72)
    plan: str = "starter"


class TenantAdminOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    plan: str
    is_active: bool
    monthly_email_count: int
    auto_send_enabled: bool
    model_config = {"from_attributes": True}


@router.post("/tenants", response_model=TenantAdminOut, status_code=201, tags=["Admin"])
async def create_tenant(
    body: TenantCreate,
    _: str = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
):
    """Create a tenant with a dashboard login. Auto-send starts OFF."""
    if body.plan not in settings.plan_caps:
        raise HTTPException(
            status_code=422,
            detail=f"plan must be one of {sorted(settings.plan_caps)}",
        )

    email = body.email.strip().lower()
    existing = await db.execute(select(Tenant).where(Tenant.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A tenant with this email already exists")

    try:
        # bcrypt is CPU-bound — keep it off the event loop
        pw_hash = await asyncio.to_thread(hash_password, body.password)
    except PasswordPolicyError as e:
        raise HTTPException(status_code=422, detail=str(e))

    tenant = Tenant(name=body.name.strip(), email=email, plan=body.plan, password_hash=pw_hash)
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    log.info("tenant_created", tenant_id=str(tenant.id), plan=tenant.plan)
    return tenant


@router.get("/tenants", response_model=list[TenantAdminOut], tags=["Admin"])
async def list_tenants(
    include_inactive: bool = False,
    _: str = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
):
    q = select(Tenant).order_by(Tenant.created_at.desc())
    if not include_inactive:
        q = q.where(Tenant.is_active == True)  # noqa: E712
    result = await db.execute(q)
    return result.scalars().all()
