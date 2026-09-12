"""
Draft management endpoints.

GET  /api/drafts              — list pending drafts (paginated)
GET  /api/drafts/{id}         — get one draft
PATCH /api/drafts/{id}/approve — approve (optionally with edited body) → send
PATCH /api/drafts/{id}/reject  — reject with optional reason
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_tenant
from app.database import get_db
from app.models.draft import Draft
from app.models.tenant import Tenant
from app.schemas.draft import DraftApprove, DraftListResponse, DraftOut, DraftReject
from app.services.draft_actions import (
    DraftActionError,
    approve_and_send,
    reject as reject_draft_action,
)

router = APIRouter()


@router.get("/drafts", response_model=DraftListResponse, tags=["Drafts"])
async def list_drafts(
    draft_status: Optional[str] = Query(default="pending_review"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    # tenant_id comes from the verified JWT — never from the client
    q = select(Draft).where(Draft.tenant_id == tenant.id)
    if draft_status:
        q = q.where(Draft.status == draft_status)

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar() or 0

    q = q.order_by(Draft.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(q)
    drafts = result.scalars().all()

    return DraftListResponse(items=drafts, total=total, page=page, page_size=page_size)


@router.get("/drafts/{draft_id}", response_model=DraftOut, tags=["Drafts"])
async def get_draft(
    draft_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(Draft, draft_id)
    if not draft or draft.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.patch("/drafts/{draft_id}/approve", response_model=DraftOut, tags=["Drafts"])
async def approve_draft(
    draft_id: uuid.UUID,
    body: DraftApprove,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Approve a draft — optionally overriding the body — then send via Gmail.

    The send itself lives in app/services/draft_actions so this endpoint and
    the emailed review page apply identical rules.
    """
    # Eager-load the thread: lazy loading in an async session raises MissingGreenlet
    result = await db.execute(
        select(Draft)
        .options(selectinload(Draft.thread))
        .where(Draft.id == draft_id, Draft.tenant_id == tenant.id)
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    try:
        await approve_and_send(db, draft, edited_body=body.edited_body)
    except DraftActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    await db.refresh(draft)
    return draft


@router.patch("/drafts/{draft_id}/reject", response_model=DraftOut, tags=["Drafts"])
async def reject_draft(
    draft_id: uuid.UUID,
    body: DraftReject,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(Draft, draft_id)
    if not draft or draft.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Draft not found")

    try:
        await reject_draft_action(db, draft, reason=body.reason)
    except DraftActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    await db.refresh(draft)
    return draft
