"""
Gmail service — polling, message parsing, reply sending.

Token refresh uses a SELECT ... FOR UPDATE row lock so concurrent workers
can't race on the same tenant's refresh token.
"""
import asyncio
import base64
import email.utils
import json
from email.message import EmailMessage as MimeMessage
from datetime import datetime, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_token, encrypt_token
from app.models.integration import Integration

settings = get_settings()
log = get_logger(__name__)

# Minimum necessary scopes. Google requires least-privilege for restricted-scope
# verification, and we never write to Gmail beyond sending: the poller only reads
# (history.list / messages.get / getProfile) and send_reply only calls messages.send.
#
# gmail.readonly is still a RESTRICTED scope, so this does not avoid the security
# assessment — reading a user's mail always lands in the restricted tier. It does
# drop the ability to modify or delete mail, which is both a smaller blast radius
# and a materially better PDPA story. gmail.send is only "sensitive".
# calendar.events covers creating appointments; freebusy.query is NOT authorised by
# it and needs its own scope, or the conflict check fails at runtime with a 403.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
]


async def get_credentials(db: AsyncSession, integration: Integration) -> Optional[Credentials]:
    """
    Load decrypted OAuth credentials for a tenant integration.
    If the access token is expired, refresh it and persist the new token.
    Uses row-level lock to prevent concurrent refresh races.
    """
    # Row-level lock — only one worker refreshes at a time
    stmt = (
        select(Integration)
        .where(Integration.id == integration.id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    locked_integration = result.scalar_one_or_none()
    if not locked_integration:
        return None

    access_token = decrypt_token(locked_integration.access_token_enc)
    refresh_token = (
        decrypt_token(locked_integration.refresh_token_enc)
        if locked_integration.refresh_token_enc
        else None
    )

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        log.info("refreshing_oauth_token", integration_id=str(integration.id))
        # Blocking network call — keep it off the event loop
        await asyncio.to_thread(creds.refresh, Request())
        # Persist the new tokens (still inside the FOR UPDATE lock)
        locked_integration.access_token_enc = encrypt_token(creds.token)
        if creds.refresh_token:
            locked_integration.refresh_token_enc = encrypt_token(creds.refresh_token)
        locked_integration.token_expiry = creds.expiry
        locked_integration.updated_at = datetime.now(timezone.utc)
        await db.flush()

    return creds


def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_text(payload: dict) -> str:
    """Recursively walk MIME parts for the first text/plain body.
    Handles nested structures like multipart/mixed > multipart/alternative."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_text(part)
        if text:
            return text
    return ""


def parse_message(raw_msg: dict) -> dict:
    """Extract relevant fields from a raw Gmail API message object."""
    headers = {h["name"].lower(): h["value"] for h in raw_msg.get("payload", {}).get("headers", [])}

    payload = raw_msg.get("payload", {})
    body = _extract_text(payload)

    # Truncate body to keep token count manageable
    body = body[:4000]

    # Parse sender
    from_raw = headers.get("from", "")
    sender_name, sender_email = email.utils.parseaddr(from_raw)

    return {
        "message_id": raw_msg.get("id"),
        "thread_id": raw_msg.get("threadId"),
        "sender_email": sender_email,
        "sender_name": sender_name,
        "subject": headers.get("subject", "(no subject)"),
        "body_text": body,
        # RFC 5322 headers so our reply threads in the sender's mail client
        "rfc_message_id": headers.get("message-id"),
        "rfc_references": headers.get("references"),
        "received_at": datetime.fromtimestamp(
            int(raw_msg.get("internalDate", 0)) / 1000, tz=timezone.utc
        ),
    }


async def fetch_new_messages(
    db: AsyncSession,
    integration: Integration,
    max_results: int = 50,
) -> list[dict]:
    """
    Fetch unread messages newer than the last processed historyId.
    Falls back to listing unread messages if no historyId is stored.
    """
    creds = await get_credentials(db, integration)
    if not creds:
        log.error("no_credentials", integration_id=str(integration.id))
        integration.last_poll_error = "No usable Google credentials for this mailbox."
        return []

    service = build_gmail_service(creds)
    messages = []

    try:
        if integration.gmail_history_id:
            # Incremental poll via History API — paginated: busy inboxes span
            # multiple pages and unpaged results silently drop messages.
            msg_ids = []
            page_token = None
            new_history_id = None
            while True:
                history_result = (
                    service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=integration.gmail_history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                        pageToken=page_token,
                    )
                    .execute()
                )
                for record in history_result.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg_ids.append(added["message"]["id"])
                new_history_id = history_result.get("historyId") or new_history_id
                page_token = history_result.get("nextPageToken")
                if not page_token or len(msg_ids) >= max_results:
                    break

            for msg_id in msg_ids[:max_results]:
                msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                messages.append(parse_message(msg))
        else:
            # First poll — fetch recent unread messages
            result = (
                service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=max_results)
                .execute()
            )
            for msg_stub in result.get("messages", []):
                msg = service.users().messages().get(userId="me", id=msg_stub["id"], format="full").execute()
                messages.append(parse_message(msg))

            # Get current history ID for next poll
            profile = service.users().getProfile(userId="me").execute()
            new_history_id = profile.get("historyId")

        # Persist the new history ID
        integration.gmail_history_id = new_history_id
        # This poll reached Gmail successfully — clear any previous failure.
        integration.last_poll_error = None
        await db.flush()

    except HttpError as e:
        if getattr(e.resp, "status", None) == 404 and integration.gmail_history_id:
            # historyId expired (Gmail keeps ~1 week) — reset and re-sync next
            # poll. Expected and self-healing, so not a poll failure.
            log.warning("gmail_history_expired", integration_id=str(integration.id))
            integration.gmail_history_id = None
            integration.last_poll_error = None
            await db.flush()
        else:
            log.error("gmail_api_error", error=str(e), integration_id=str(integration.id))
            # Recorded so the hourly health check can alert the owner. An
            # expired or revoked OAuth grant surfaces here and would otherwise
            # look exactly like an inbox with no new mail.
            integration.last_poll_error = str(e)[:500]
            await db.flush()

    return messages


async def send_reply(
    db: AsyncSession,
    integration: Integration,
    thread_id: str,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Optional[str]:
    """
    Send an email reply via the Gmail API (uses the tenant's own Gmail address).
    Returns the Gmail message ID on success.
    """
    creds = await get_credentials(db, integration)
    if not creds:
        return None

    service = build_gmail_service(creds)

    # Build the MIME message with the stdlib email API. Never interpolate
    # sender-controlled values (subject, addresses) into raw headers — a
    # crafted "Subject: x\r\nBcc: ..." would otherwise inject headers.
    clean_subject = " ".join((subject or "").splitlines()).strip()
    clean_to = " ".join((to_email or "").splitlines()).strip()

    mime = MimeMessage()
    mime["From"] = integration.mailbox_address
    mime["To"] = clean_to
    mime["Subject"] = clean_subject if clean_subject.lower().startswith("re:") else f"Re: {clean_subject}"
    if in_reply_to:
        # RFC threading headers — without these the customer's mail client
        # shows the reply as a brand-new email instead of a conversation.
        clean_msg_id = " ".join(in_reply_to.splitlines()).strip()
        mime["In-Reply-To"] = clean_msg_id
        prior_refs = " ".join((references or "").splitlines()).strip()
        mime["References"] = (prior_refs + " " + clean_msg_id).strip()
    mime.set_content(body)
    encoded = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")

    def _send():
        return (
            service.users()
            .messages()
            .send(userId="me", body={"raw": encoded, "threadId": thread_id})
            .execute()
        )

    try:
        # Blocking Google API call — run in a thread so it never blocks the
        # shared event loop (critical in the FastAPI approve endpoint).
        sent = await asyncio.to_thread(_send)
        log.info("email_sent", message_id=sent["id"])
        return sent["id"]
    except HttpError as e:
        log.error("gmail_send_error", error=str(e))
        return None
