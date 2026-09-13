"""
SendGrid service — transactional email sending.
Used as fallback when Gmail API send is unavailable,
or for system emails (weekly digest, notifications).
"""
from typing import Optional

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, To, From, Content

from app.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)

_client = SendGridAPIClient(api_key=settings.sendgrid_api_key)


def send_email(
    to_email: str,
    subject: str,
    body: str,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
) -> Optional[str]:
    """
    Send a plain-text email via SendGrid.
    Returns the SendGrid X-Message-Id on success, None on failure.
    """
    message = Mail(
        from_email=From(
            from_email or settings.sendgrid_from_email,
            from_name or settings.sendgrid_from_name,
        ),
        to_emails=To(to_email),
        subject=subject,
        plain_text_content=Content("text/plain", body),
    )

    try:
        response = _client.send(message)
        message_id = response.headers.get("X-Message-Id", "unknown")
        log.info("sendgrid_sent", message_id=message_id, status=response.status_code)
        return message_id
    except Exception as e:
        log.error("sendgrid_error", error=str(e))
        return None


def send_draft_review_email(
    to_email: str,
    business_name: str,
    customer_email: str,
    subject: str,
    draft_body: str,
    review_url: str,
) -> Optional[str]:
    """Tell the owner a reply is waiting, with a one-click review link.

    This is the product's actual user interface until the dashboard exists —
    without it, drafts pile up unseen and the customer concludes the AI is
    broken. The full draft is included so the owner can judge it from the
    notification alone and only open the link to act.
    """
    body = (
        f"Hi {business_name},\n\n"
        f"A customer email needs a reply. Nothing has been sent yet.\n\n"
        f"From:    {customer_email}\n"
        f"Subject: {subject}\n\n"
        f"--- Suggested reply ---\n"
        f"{draft_body}\n"
        f"-----------------------\n\n"
        f"Approve, edit or reject it here:\n"
        f"{review_url}\n\n"
        f"— AccountFlow"
    )
    return send_email(
        to_email=to_email,
        subject=f"Reply ready: {subject}"[:200],
        body=body,
    )


def send_weekly_digest(
    to_email: str,
    business_name: str,
    emails_processed: int,
    drafts_sent: int,
    tasks_created: int,
    appointments_booked: int,
) -> None:
    """Send the Monday 9am SGT weekly digest email to the tenant owner."""
    body = (
        f"Hi {business_name},\n\n"
        f"Here's your AccountFlow weekly summary:\n\n"
        f"  Emails processed:      {emails_processed}\n"
        f"  Replies sent:          {drafts_sent}\n"
        f"  Tasks created:         {tasks_created}\n"
        f"  Appointments booked:   {appointments_booked}\n\n"
        f"Log in to your dashboard to review any pending drafts.\n\n"
        f"— AccountFlow"
    )
    send_email(
        to_email=to_email,
        subject=f"AccountFlow Weekly Summary — {business_name}",
        body=body,
    )
