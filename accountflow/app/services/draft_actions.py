"""Approve / reject a draft.

Shared by BOTH entry points so they can never diverge:
  - app/api/routes/drafts.py  — the JWT API (dashboard, scripts)
  - app/api/routes/review.py  — the emailed one-click review page

Sending a customer-facing reply is the single most consequential thing this
product does, so the rules around it live in exactly one place.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.draft import Draft
from app.models.integration import Integration

log = get_logger(__name__)


class DraftActionError(Exception):
    """A draft could not be actioned. Carries an HTTP status for the API and a
    human-readable message safe to show a clinic owner."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _require_pending(draft: Draft) -> None:
    if draft.status != "pending_review":
        raise DraftActionError(
            f"This reply has already been {draft.status.replace('_', ' ')}.",
            status_code=409,
        )


async def approve_and_send(
    db: AsyncSession,
    draft: Draft,
    edited_body: Optional[str] = None,
) -> str:
    """Send a pending draft from the mailbox its thread arrived through, then commit.

    `draft.thread` must already be loaded (use selectinload) — lazy-loading a
    relationship inside an async session raises MissingGreenlet at runtime.

    Returns the provider's message id. Raises DraftActionError on any failure,
    leaving the draft pending so it can be retried.
    """
    _require_pending(draft)

    # The mailbox is part of the draft's data (migration 009, ADR-002): reply
    # from the integration the thread arrived through, never from "any active
    # one" — a Graph-sourced draft must not reach the Gmail sender. The tenant
    # predicate is belt-and-braces over row-level security.
    result = await db.execute(
        select(Integration).where(
            Integration.id == draft.integration_id,
            Integration.tenant_id == draft.tenant_id,
            Integration.is_active == True,  # noqa: E712
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise DraftActionError(
            "This mailbox is no longer connected, so the reply could not be "
            "sent. Reconnect it and try again.",
            status_code=422,
        )

    if edited_body:
        draft.edited_body = edited_body
    send_body = edited_body or draft.body

    # Imported here rather than at module scope: the provider modules pull in
    # heavy client stacks, and this module is imported by the API on every
    # request.
    from app.services.mail_provider import send_reply

    msg_id = await send_reply(
        db=db,
        integration=integration,
        thread_id=draft.thread.gmail_thread_id if draft.thread else "",
        message_id=draft.thread.gmail_message_id if draft.thread else None,
        to_email=draft.to_email,
        subject=draft.subject or "",
        body=send_body,
        in_reply_to=draft.thread.rfc_message_id if draft.thread else None,
        references=draft.thread.rfc_references if draft.thread else None,
    )
    if not msg_id:
        raise DraftActionError(
            "The mailbox provider refused the message, so nothing was sent. "
            "Please try again in a few minutes.",
            status_code=502,
        )

    now = datetime.now(timezone.utc)
    draft.status = "sent"
    draft.sent_message_id = msg_id
    draft.sent_at = now
    draft.reviewed_at = now
    await db.commit()

    log.info("draft_approved", draft_id=str(draft.id), sent_message_id=msg_id)
    return msg_id


async def reject(
    db: AsyncSession,
    draft: Draft,
    reason: Optional[str] = None,
) -> None:
    """Mark a pending draft rejected. Nothing is sent to the customer."""
    _require_pending(draft)

    draft.status = "rejected"
    draft.rejection_reason = reason
    draft.reviewed_at = datetime.now(timezone.utc)
    await db.commit()

    log.info("draft_rejected", draft_id=str(draft.id), has_reason=bool(reason))
