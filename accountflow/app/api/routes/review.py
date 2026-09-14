"""One-click draft review, authorised by an emailed link.

Why this exists: approving a reply through the JWT API means logging in,
copying a bearer token and pasting a UUID. No clinic owner will do that forty
times a day, so in practice every draft would sit unreviewed. This gives the
owner an Approve / Edit / Reject page reachable straight from the notification
email, with nothing to install and nothing to log into.

Two deliberate design decisions:

1. GET only renders; POST mutates. Corporate mail scanners and link
   prefetchers routinely fetch every URL in an inbound email. If approving
   were a GET, a scanner would silently send an unreviewed reply to a
   patient. The GET is safe to fetch as many times as anything likes.

2. Everything rendered is HTML-escaped. The draft body is written by the AI
   from an untrusted customer email, so it is treated as hostile input.
"""
import html
import uuid

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.logging import get_logger
from app.core.security import verify_review_token
from app.database import bind_tenant, get_db
from app.models.draft import Draft
from app.services.draft_actions import DraftActionError, approve_and_send, reject

router = APIRouter()
settings = get_settings()
log = get_logger(__name__)

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; font:16px/1.55 -apple-system,BlinkMacSystemFont,
  "Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:#f4f6f8; color:#16212e; }
@media (prefers-color-scheme: dark) { body { background:#11161c; color:#e6edf3; }
  .card { background:#1a222c !important; border-color:#2b3642 !important; }
  .meta, textarea { background:#141b23 !important; color:#e6edf3 !important;
    border-color:#2b3642 !important; } }
.card { max-width:640px; margin:0 auto; background:#fff; border:1px solid #dfe5ec;
  border-radius:12px; padding:24px; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:#6b7a8d; font-size:14px; margin:0 0 20px; }
.meta { background:#f7f9fb; border:1px solid #e3e9f0; border-radius:8px;
  padding:12px 14px; font-size:14px; margin-bottom:18px; }
.meta div { margin:3px 0; }
label { display:block; font-weight:600; font-size:14px; margin:0 0 6px; }
textarea { width:100%; min-height:220px; padding:12px; font:15px/1.5 inherit;
  border:1px solid #cfd8e3; border-radius:8px; resize:vertical; background:#fff; }
.row { display:flex; gap:10px; margin-top:18px; flex-wrap:wrap; }
button { flex:1 1 160px; padding:13px 18px; font-size:15px; font-weight:600;
  border:0; border-radius:8px; cursor:pointer; }
.approve { background:#0f8a4a; color:#fff; }
.reject { background:#fff; color:#b3261e; border:1px solid #e4c4c1; }
@media (prefers-color-scheme: dark) { .reject { background:#241a1a; } }
.note { margin-top:16px; font-size:13px; color:#6b7a8d; }
.big { font-size:44px; line-height:1; margin-bottom:10px; }
"""


def _page(title: str, inner: str, status_code: int = 200) -> HTMLResponse:
    """Wrap content in the shared shell.

    no-store keeps the tokenised URL out of shared caches; no-referrer stops
    the token leaking through the Referer header; noindex keeps it out of
    search engines if a link is ever pasted somewhere public.
    """
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><div class='card'>{inner}</div></body></html>"
    )
    return HTMLResponse(
        content=doc,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


def _message(emoji: str, heading: str, detail: str, status_code: int = 200) -> HTMLResponse:
    return _page(
        heading,
        f"<div class='big'>{emoji}</div><h1>{html.escape(heading)}</h1>"
        f"<p class='sub'>{html.escape(detail)}</p>",
        status_code=status_code,
    )


async def _load_draft(token: str, db: AsyncSession):
    """Resolve a review token to a draft, eager-loading the thread.

    Returns (draft, None) or (None, error_response).
    """
    verified = verify_review_token(token, max_age_seconds=settings.review_link_days * 86400)
    if not verified:
        return None, _message(
            "⏱️",
            "This link has expired",
            "Review links are valid for a limited time. Open the most recent "
            "notification email, or sign in to your dashboard to see pending replies.",
            status_code=400,
        )
    tenant_str, draft_str = verified
    try:
        tenant_id = uuid.UUID(tenant_str)
        draft_id = uuid.UUID(draft_str)
    except ValueError:
        return None, _message(
            "⚠️", "Invalid link", "This review link could not be read.", status_code=400
        )

    # The token is the only credential on this route, so it is also the only
    # source of tenant context: bind it BEFORE the draft is read (ADR-001). The
    # explicit tenant predicate stays — RLS is the backstop, not the filter.
    await bind_tenant(db, tenant_id)
    result = await db.execute(
        select(Draft)
        .options(selectinload(Draft.thread))
        .where(Draft.id == draft_id, Draft.tenant_id == tenant_id)
    )
    draft = result.scalar_one_or_none()
    if not draft:
        return None, _message(
            "⚠️",
            "Reply not found",
            "This draft no longer exists.",
            status_code=404,
        )
    return draft, None


@router.get("/review/{token}", response_class=HTMLResponse, include_in_schema=False)
async def review_page(token: str, db: AsyncSession = Depends(get_db)):
    """Render the draft for review. Read-only — safe for link prefetchers."""
    draft, error = await _load_draft(token, db)
    if error:
        return error

    if draft.status != "pending_review":
        return _message(
            "✅",
            "Already handled",
            f"This reply was already {draft.status.replace('_', ' ')}. Nothing further to do.",
        )

    confidence = (
        f"{draft.ai_confidence * 100:.0f}%" if draft.ai_confidence is not None else "not scored"
    )
    thread = draft.thread
    original = (thread.body_text or "") if thread else ""

    inner = (
        "<h1>Reply ready for your approval</h1>"
        "<p class='sub'>Nothing has been sent to your customer yet.</p>"
        "<div class='meta'>"
        f"<div><strong>To:</strong> {html.escape(draft.to_email or '')}</div>"
        f"<div><strong>Subject:</strong> {html.escape(draft.subject or '')}</div>"
        f"<div><strong>AI confidence:</strong> {html.escape(confidence)}</div>"
        "</div>"
    )
    if original:
        inner += (
            "<div class='meta'><strong>They wrote:</strong><br>"
            f"{html.escape(original[:600])}{'…' if len(original) > 600 else ''}</div>"
        )
    inner += (
        f"<form method='post' action='/api/review/{html.escape(token)}'>"
        "<label for='edited_body'>Your reply (edit it if you like)</label>"
        f"<textarea id='edited_body' name='edited_body'>{html.escape(draft.body or '')}</textarea>"
        "<div class='row'>"
        "<button class='approve' type='submit' name='action' value='approve'>Approve &amp; send</button>"
        "<button class='reject' type='submit' name='action' value='reject'>Reject</button>"
        "</div></form>"
        "<p class='note'>Approving sends this from your own mailbox, as a reply in "
        "the original conversation.</p>"
    )
    return _page("Reply ready for your approval", inner)


@router.post("/review/{token}", response_class=HTMLResponse, include_in_schema=False)
async def review_submit(
    token: str,
    action: str = Form(...),
    edited_body: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Apply the owner's decision. This is the only path that can send."""
    draft, error = await _load_draft(token, db)
    if error:
        return error

    try:
        if action == "approve":
            # Only record an edit when the text actually changed, so the
            # rejection/edit rate stays a meaningful quality signal.
            body = edited_body.strip()
            changed = body and body != (draft.body or "").strip()
            await approve_and_send(db, draft, edited_body=body if changed else None)
            log.info("draft_approved_via_email_link", draft_id=str(draft.id), edited=bool(changed))
            return _message(
                "✅", "Sent", "Your reply is on its way, in the original email thread."
            )

        if action == "reject":
            await reject(db, draft, reason="Rejected from review email")
            log.info("draft_rejected_via_email_link", draft_id=str(draft.id))
            return _message(
                "\U0001f5d1️",
                "Rejected",
                "Nothing was sent. The customer's email stays in your inbox for you to handle.",
            )

        return _message("⚠️", "Unknown action", "Please use the buttons.", status_code=400)

    except DraftActionError as e:
        return _message("⚠️", "Could not complete that", e.message, status_code=e.status_code)
