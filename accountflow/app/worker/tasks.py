"""
Core Celery tasks — the Gmail → Claude → Action pipeline.

Flow per tenant:
  poll_all_tenants
    └── process_tenant_inbox (per active integration)
         └── for each new message:
              process_single_email
                ├── classify_and_draft (Claude)
                ├── draft_reply → save Draft (pending_review or auto_sent if confidence > threshold)
                ├── create_appointment → save Task + Google Calendar event
                ├── create_task → save Task
                └── flag_human → save EmailThread with status=flagged
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

import sentry_sdk
from celery import Task

from app.worker.celery_app import celery_app
from app.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)


def run_async(coro):
    """Run an async coroutine from a sync Celery task.

    Each task gets a fresh event loop, so the shared async engine's pooled
    connections must be disposed before the loop closes — otherwise asyncpg
    connections leak across loops and raise 'attached to a different loop'.
    """
    async def _wrapped():
        from app.database import engine
        try:
            return await coro
        finally:
            await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


@celery_app.task(
    name="app.worker.tasks.poll_all_tenants",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def poll_all_tenants(self: Task):
    """
    Triggered every 120 seconds by Celery Beat.
    Fetches all active integrations and fans out one task per integration.
    """
    return run_async(_poll_all_tenants_async())


async def _poll_all_tenants_async():
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.integration import Integration
    from app.models.tenant import Tenant

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Integration)
            .join(Tenant, Integration.tenant_id == Tenant.id)
            .where(Integration.is_active == True)
            .where(Tenant.is_active == True)
        )
        integrations = result.scalars().all()

    log.info("polling_tenants", count=len(integrations))

    for integration in integrations:
        process_tenant_inbox.delay(str(integration.id))


@celery_app.task(
    name="app.worker.tasks.process_tenant_inbox",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def process_tenant_inbox(self: Task, integration_id: str):
    """Process all new emails for a single tenant integration."""
    try:
        return run_async(_process_tenant_inbox_async(integration_id))
    except Exception as exc:
        log.error("process_tenant_inbox_failed", integration_id=integration_id, error=str(exc))
        sentry_sdk.capture_exception(exc)
        raise self.retry(exc=exc)


async def _process_tenant_inbox_async(integration_id: str):
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.core.policy import monthly_period_needs_reset
    from app.database import AsyncSessionLocal
    from app.models.integration import Integration
    from app.models.tenant import Tenant
    from app.models.email_thread import EmailThread
    from app.services.mail_provider import fetch_new_messages
    from app.services.sendgrid import send_email

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Integration).where(Integration.id == uuid.UUID(integration_id))
        )
        integration = result.scalar_one_or_none()
        if not integration:
            log.warning("integration_not_found", integration_id=integration_id)
            return

        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == integration.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant or not tenant.is_active:
            return

        now = datetime.now(timezone.utc)

        # Lazy monthly reset: derive the period at poll time so a Beat outage
        # on the 1st cannot leave a tenant capped for the rest of the month.
        if tenant.monthly_reset_at is None:
            tenant.monthly_reset_at = now
        elif monthly_period_needs_reset(tenant.monthly_reset_at, now):
            log.info(
                "monthly_count_reset_lazy",
                tenant_id=str(tenant.id),
                previous=tenant.monthly_email_count,
            )
            tenant.monthly_email_count = 0
            tenant.monthly_reset_at = now

        # Stamped BEFORE the cap check and whether or not mail is found: a
        # capped tenant is healthy, and without this stamp the staleness alert
        # would tell them their inbox connection is broken.
        integration.last_poll_at = now

        cap = settings.plan_caps.get(tenant.plan, 500)
        if tenant.monthly_email_count >= cap:
            log.warning(
                "monthly_cap_reached",
                tenant_id=str(tenant.id),
                plan=tenant.plan,
                cap=cap,
                count=tenant.monthly_email_count,
            )
            await db.commit()
            return

        messages = await fetch_new_messages(db, integration)
        log.info("fetched_messages", count=len(messages), tenant_id=str(tenant.id))

        to_dispatch = []
        own_address = (integration.gmail_address or "").lower()

        for msg in messages:
            # Never process mail sent by the connected mailbox itself —
            # with message-level dedup our own replies would otherwise loop.
            if own_address and (msg["sender_email"] or "").lower() == own_address:
                continue

            # Message-level dedup (a follow-up in a known thread is NEW work —
            # the old thread-level skip silently ignored customer replies)
            existing = await db.execute(
                select(EmailThread.id).where(
                    EmailThread.gmail_message_id == msg["message_id"],
                    EmailThread.tenant_id == tenant.id,
                )
            )
            if existing.scalar_one_or_none():
                continue

            # Cap check per message
            if tenant.monthly_email_count >= cap:
                break

            # Save inside a savepoint: the DB unique constraint
            # (tenant_id, gmail_message_id) is the race-proof backstop.
            thread = EmailThread(
                tenant_id=tenant.id,
                gmail_thread_id=msg["thread_id"],
                gmail_message_id=msg["message_id"],
                rfc_message_id=msg.get("rfc_message_id"),
                rfc_references=msg.get("rfc_references"),
                sender_email=msg["sender_email"],
                sender_name=msg["sender_name"],
                subject=msg["subject"],
                body_text=msg["body_text"],
                received_at=msg["received_at"],
                status="processing",
            )
            try:
                async with db.begin_nested():
                    db.add(thread)
                    await db.flush()
            except IntegrityError:
                # A concurrent poller inserted the same message first
                continue

            tenant.monthly_email_count += 1
            to_dispatch.append(str(thread.id))

        # The cap was not reached when this poll began; if it is now, this
        # cycle crossed it. Capture plain values before commit (see the
        # MissingGreenlet note in _process_single_email_async).
        cap_notice = None
        if tenant.monthly_email_count >= cap:
            cap_notice = {
                "tenant_id": str(tenant.id),
                "to_email": tenant.email,
                "business_name": tenant.name,
                "plan": tenant.plan,
                "cap": cap,
            }

        await db.commit()

    # Dispatch AFTER commit — otherwise the worker can pick a task up
    # before the thread row is visible and fail with "not found".
    for thread_id in to_dispatch:
        process_single_email.delay(thread_id, integration_id)

    # Tell the owner ONCE, with the right message. Before this existed, the
    # only signal a capped tenant ever received was the staleness alert
    # claiming their inbox connection had died.
    if cap_notice:
        try:
            await asyncio.to_thread(
                send_email,
                to_email=cap_notice["to_email"],
                subject="AccountFlow: you've reached this month's email limit",
                body=(
                    f"Hi {cap_notice['business_name']},\n\n"
                    f"AccountFlow has processed {cap_notice['cap']} emails for you this "
                    f"month, which is the limit of your {cap_notice['plan'].title()} plan.\n\n"
                    f"Your mailbox connection is fine and nothing has been lost — but new "
                    f"customer emails will NOT be drafted or actioned until the 1st of next "
                    f"month, so please handle the inbox manually until then.\n\n"
                    f"To lift the limit now, reply to this email and we'll move you to a "
                    f"larger plan.\n\n"
                    f"— AccountFlow"
                ),
            )
            log.info("monthly_cap_email_sent", tenant_id=cap_notice["tenant_id"])
        except Exception as e:
            log.error("monthly_cap_email_failed", tenant_id=cap_notice["tenant_id"], error=str(e))
            sentry_sdk.capture_exception(e)


@celery_app.task(
    name="app.worker.tasks.process_single_email",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def process_single_email(self: Task, thread_id: str, integration_id: str):
    """Run Claude classification + action for one email thread."""
    try:
        return run_async(_process_single_email_async(thread_id, integration_id))
    except Exception as exc:
        log.error("process_single_email_failed", thread_id=thread_id, error=str(exc))
        sentry_sdk.capture_exception(exc)
        if self.request.retries >= self.max_retries:
            # Out of retries — record the failure so the dashboard and the
            # hourly error-rate alert can see it, then surface the error.
            run_async(_mark_thread_failed(thread_id, str(exc)))
            raise
        raise self.retry(exc=exc)


async def _mark_thread_failed(thread_id: str, error: str):
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.email_thread import EmailThread

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailThread).where(EmailThread.id == uuid.UUID(thread_id))
        )
        thread = result.scalar_one_or_none()
        if thread:
            thread.status = "failed"
            thread.error_message = error[:2000]
            await db.commit()


async def _process_single_email_async(thread_id: str, integration_id: str):
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.integration import Integration
    from app.models.email_thread import EmailThread
    from app.models.draft import Draft
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.core.policy import calendar_send_updates, should_auto_send
    from app.core.security import create_review_token
    from app.services.claude import classify_and_draft
    from app.services.mail_provider import (
        check_availability,
        create_appointment,
        send_reply,
    )
    from app.services.sendgrid import send_draft_review_email
    from dateutil.parser import parse as parse_dt

    # Populated when a draft needs owner approval; the notification is sent
    # AFTER commit so the review link can never point at an uncommitted row.
    notify: Optional[dict] = None

    async with AsyncSessionLocal() as db:
        thread_result = await db.execute(
            select(EmailThread).where(EmailThread.id == uuid.UUID(thread_id))
        )
        thread = thread_result.scalar_one_or_none()

        integration_result = await db.execute(
            select(Integration).where(Integration.id == uuid.UUID(integration_id))
        )
        integration = integration_result.scalar_one_or_none()

        if not thread or not integration:
            log.warning("thread_or_integration_missing", thread_id=thread_id)
            return

        if thread.status != "processing":
            # Already handled — a sweeper re-dispatch raced the original task,
            # or a retry arrived after the commit. Never classify twice.
            log.info("thread_already_handled", thread_id=thread_id, status=thread.status)
            return

        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == thread.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant:
            log.warning("tenant_missing", thread_id=thread_id)
            return

        # --- AI Classification ---
        ai_result = await classify_and_draft(
            sender_email=thread.sender_email,
            sender_name=thread.sender_name or "",
            subject=thread.subject or "",
            body=thread.body_text or "",
            business_name=tenant.name,
            custom_system_prompt=integration.system_prompt,
        )

        # Persist AI result on thread (incl. token usage for unit economics)
        thread.ai_action = ai_result["action"]
        thread.ai_confidence = ai_result["confidence"]
        thread.ai_model_used = ai_result["model_used"]
        thread.ai_input_tokens = ai_result.get("input_tokens")
        thread.ai_output_tokens = ai_result.get("output_tokens")
        thread.processed_at = datetime.now(timezone.utc)

        action = ai_result["action"]
        confidence = ai_result["confidence"]

        # ── Action: Draft Reply ────────────────────────────────────────────────
        if action == "draft_reply" and ai_result.get("draft_body"):
            # Auto-send is OPT-IN per tenant (pilot-safe: default off, so
            # every draft is human-reviewed until the owner enables it).
            auto_send = should_auto_send(
                tenant.auto_send_enabled, tenant.auto_send_threshold, confidence
            )
            draft_status = "auto_sent" if auto_send else "pending_review"

            draft = Draft(
                tenant_id=tenant.id,
                thread_id=thread.id,
                to_email=thread.sender_email,
                subject=f"Re: {thread.subject or ''}",
                body=ai_result["draft_body"],
                status=draft_status,
                ai_confidence=confidence,
            )
            db.add(draft)
            await db.flush()

            if auto_send:
                # send_reply fetches its own credentials — no need to pre-load
                msg_id = await send_reply(
                    db=db,
                    integration=integration,
                    thread_id=thread.gmail_thread_id,
                    # Graph replies are an operation on the original message,
                    # so the provider id is required; Gmail ignores it.
                    message_id=thread.gmail_message_id,
                    to_email=thread.sender_email,
                    subject=thread.subject or "",
                    body=ai_result["draft_body"],
                    in_reply_to=thread.rfc_message_id,
                    references=thread.rfc_references,
                )
                if msg_id:
                    draft.sendgrid_message_id = msg_id
                    draft.sent_at = datetime.now(timezone.utc)
                else:
                    draft.status = "pending_review"
                    log.warning("auto_send_failed_fallback_to_review", draft_id=str(draft.id))

            # Read the status AFTER the auto-send attempt: a failed send falls
            # back to pending_review and must still notify the owner.
            if draft.status == "pending_review":
                # Capture plain values now. Once the session commits, touching
                # an expired ORM attribute triggers a lazy refresh, which
                # raises MissingGreenlet in async context.
                notify = {
                    "to_email": tenant.email,
                    "business_name": tenant.name,
                    "customer_email": thread.sender_email or "",
                    "subject": thread.subject or "(no subject)",
                    "draft_body": ai_result["draft_body"],
                    "review_url": (
                        f"{settings.app_base_url.rstrip('/')}"
                        f"/api/review/{create_review_token(str(draft.id))}"
                    ),
                }

            thread.status = "actioned"

        # ── Action: Create Appointment ─────────────────────────────────────────
        elif action == "create_appointment":
            appt = ai_result.get("appointment_details") or {}
            task = Task(
                tenant_id=tenant.id,
                thread_id=thread.id,
                title=appt.get("title", f"Appointment — {thread.sender_name or thread.sender_email}"),
                description=appt.get("notes", ""),
                status="open",
            )

            # Try to parse a proposed datetime and create a calendar event
            proposed_dt_str = appt.get("proposed_datetime")
            slot_conflict = False
            if proposed_dt_str:
                try:
                    proposed_dt = parse_dt(proposed_dt_str)
                    duration = appt.get("duration_minutes") or 60

                    # Never book over an existing event. check_availability
                    # fails closed, so an API error also lands here and the
                    # owner books by hand rather than the AI double-booking.
                    slot_free = await check_availability(
                        db=db,
                        integration=integration,
                        start_datetime=proposed_dt,
                        end_datetime=proposed_dt + timedelta(minutes=duration),
                    )

                    if slot_free:
                        event_id = await create_appointment(
                            db=db,
                            integration=integration,
                            title=task.title,
                            start_datetime=proposed_dt,
                            duration_minutes=duration,
                            attendee_email=thread.sender_email,
                            notes=appt.get("notes"),
                            send_updates=calendar_send_updates(tenant.auto_send_enabled),
                        )
                        if event_id:
                            task.google_event_id = event_id
                    else:
                        slot_conflict = True
                        log.info(
                            "calendar_slot_unavailable",
                            thread_id=thread_id,
                            requested=proposed_dt_str,
                        )
                        task.title = f"Clash — {task.title}"
                        task.description = (
                            f"The customer asked for {proposed_dt_str}, but that slot "
                            f"is not free. Offer them another time.\n\n"
                            f"{task.description or ''}"
                        ).strip()
                except Exception as e:
                    log.warning("calendar_create_skipped", reason=str(e))

            db.add(task)
            # A clash needs a human, so don't mark the thread as handled.
            thread.status = "flagged" if slot_conflict else "actioned"

        # ── Action: Create Task ────────────────────────────────────────────────
        elif action == "create_task":
            task_details = ai_result.get("task_details") or {}
            task = Task(
                tenant_id=tenant.id,
                thread_id=thread.id,
                title=task_details.get("title", f"Follow-up: {thread.subject or 'Email'}"),
                description=task_details.get("description", ""),
                status="open",
            )
            if task_details.get("due_date"):
                try:
                    task.due_date = parse_dt(task_details["due_date"])
                except Exception:
                    pass
            db.add(task)
            thread.status = "actioned"

        # ── Action: Flag Human ─────────────────────────────────────────────────
        elif action == "flag_human":
            thread.status = "flagged"

        # ── Action: Ignore ─────────────────────────────────────────────────────
        elif action == "ignore":
            thread.status = "ignored"

        else:
            thread.status = "flagged"

        # Read before commit — afterwards this attribute may be expired, and
        # refreshing it would lazy-load inside an async session.
        final_status = thread.status
        await db.commit()
        log.info(
            "email_processed",
            thread_id=thread_id,
            action=action,
            confidence=confidence,
            status=final_status,
        )

    # Notify the owner outside the session, once the draft is durably stored.
    # A failure here must not lose the draft — it is safely in the database and
    # still reachable from the dashboard, so we log and move on.
    if notify:
        try:
            await asyncio.to_thread(send_draft_review_email, **notify)
            log.info("draft_review_email_sent", thread_id=thread_id)
        except Exception as e:
            log.error("draft_review_email_failed", thread_id=thread_id, error=str(e))
            sentry_sdk.capture_exception(e)


@celery_app.task(name="app.worker.tasks.reset_monthly_email_counts")
def reset_monthly_email_counts():
    """Reset all tenants' monthly_email_count on the 1st of each month."""
    return run_async(_reset_monthly_counts_async())


async def _reset_monthly_counts_async():
    """Scheduled reset. The poller also resets lazily (policy.
    monthly_period_needs_reset), so a missed Beat run no longer strands a
    capped tenant; this stays as the prompt path on the 1st."""
    from app.database import AsyncSessionLocal
    from app.models.tenant import Tenant
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Tenant).values(
                monthly_email_count=0,
                monthly_reset_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    log.info("monthly_email_counts_reset")


@celery_app.task(name="app.worker.tasks.sweep_stuck_threads")
def sweep_stuck_threads():
    """Every 10 minutes: re-dispatch or fail threads stuck in `processing`."""
    return run_async(_sweep_stuck_threads_async())


async def _sweep_stuck_threads_async():
    """A thread is committed as `processing` before its task is queued. If the
    queue message is lost — broker eviction, worker killed before ack — nothing
    else ever touches the row, and the error-rate alert only counts `failed`,
    so the patient's email vanishes while the product reports itself healthy.
    This is the only path that turns that silent drop into a visible one.
    """
    from sqlalchemy import select
    from app.core.policy import STUCK_REDISPATCH_WINDOW, stuck_thread_action
    from app.database import AsyncSessionLocal
    from app.models.email_thread import EmailThread
    from app.models.integration import Integration

    now = datetime.now(timezone.utc)
    to_redispatch: list[tuple[str, str]] = []
    failed = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailThread).where(
                EmailThread.status == "processing",
                EmailThread.created_at <= now - STUCK_REDISPATCH_WINDOW[0],
            )
        )
        stuck = result.scalars().all()

        for thread in stuck:
            action = stuck_thread_action(thread.created_at, now)
            if action == "fail":
                thread.status = "failed"
                thread.error_message = (
                    "Stuck in processing for over an hour; no worker completed it."
                )
                failed += 1
                log.warning(
                    "stuck_thread_failed",
                    thread_id=str(thread.id),
                    tenant_id=str(thread.tenant_id),
                )
                continue
            if action != "redispatch":
                continue

            # Threads do not yet record which mailbox they came from
            # (migration 009 adds integration_id). Until then, re-dispatch only
            # when the tenant has exactly one active mailbox — the pilot
            # reality — and otherwise fail loudly rather than guess.
            integration_ids = (
                await db.execute(
                    select(Integration.id).where(
                        Integration.tenant_id == thread.tenant_id,
                        Integration.is_active == True,  # noqa: E712
                    )
                )
            ).scalars().all()
            if len(integration_ids) == 1:
                to_redispatch.append((str(thread.id), str(integration_ids[0])))
                log.warning(
                    "stuck_thread_redispatched",
                    thread_id=str(thread.id),
                    tenant_id=str(thread.tenant_id),
                )
            else:
                thread.status = "failed"
                thread.error_message = (
                    f"Stuck in processing; cannot re-dispatch with "
                    f"{len(integration_ids)} active mailboxes."
                )
                failed += 1
                log.warning(
                    "stuck_thread_failed_ambiguous_mailbox",
                    thread_id=str(thread.id),
                    tenant_id=str(thread.tenant_id),
                    mailboxes=len(integration_ids),
                )

        await db.commit()

    for thread_id, integration_id in to_redispatch:
        process_single_email.delay(thread_id, integration_id)

    if stuck:
        log.info(
            "stuck_thread_sweep",
            found=len(stuck),
            redispatched=len(to_redispatch),
            failed=failed,
        )


@celery_app.task(name="app.worker.tasks.send_weekly_digest_all")
def send_weekly_digest_all():
    """Send the weekly digest to all active tenants."""
    return run_async(_send_weekly_digest_async())


async def _send_weekly_digest_async():
    from sqlalchemy import select, func
    from app.database import AsyncSessionLocal
    from app.models.tenant import Tenant
    from app.models.email_thread import EmailThread
    from app.models.draft import Draft
    from app.models.task import Task
    from app.services.sendgrid import send_weekly_digest

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    async with AsyncSessionLocal() as db:
        tenants_result = await db.execute(
            select(Tenant).where(Tenant.is_active == True)
        )
        tenants = tenants_result.scalars().all()

        for tenant in tenants:
            emails = await db.execute(
                select(func.count(EmailThread.id)).where(
                    EmailThread.tenant_id == tenant.id,
                    EmailThread.created_at >= week_ago,
                )
            )
            drafts_sent = await db.execute(
                select(func.count(Draft.id)).where(
                    Draft.tenant_id == tenant.id,
                    Draft.status.in_(["sent", "auto_sent"]),
                    Draft.created_at >= week_ago,
                )
            )
            tasks_created = await db.execute(
                select(func.count(Task.id)).where(
                    Task.tenant_id == tenant.id,
                    Task.created_at >= week_ago,
                )
            )

            appointments = await db.execute(
                select(func.count(Task.id)).where(
                    Task.tenant_id == tenant.id,
                    Task.google_event_id.is_not(None),
                    Task.created_at >= week_ago,
                )
            )

            await asyncio.to_thread(
                send_weekly_digest,
                to_email=tenant.email,
                business_name=tenant.name,
                emails_processed=emails.scalar() or 0,
                drafts_sent=drafts_sent.scalar() or 0,
                tasks_created=tasks_created.scalar() or 0,
                appointments_booked=appointments.scalar() or 0,
            )


@celery_app.task(name="app.worker.tasks.check_tenant_error_rates")
def check_tenant_error_rates():
    """Hourly: alert tenant owners whose inbox processing is failing.

    Alert condition: >= 3 failed threads AND failure rate >= 10% over the
    last 24h. Throttled to one alert email per tenant per 24h.
    """
    return run_async(_check_tenant_error_rates_async())


async def _check_tenant_error_rates_async():
    """Hourly health sweep across every active tenant.

    Two distinct failure modes, because they look nothing alike in the data:

      1. Emails arrive but processing fails  → failed/total ratio.
      2. The inbox connection itself is dead → no emails arrive at all, so
         mode 1 can never fire (0/0). This is the dangerous one: a revoked
         OAuth grant looks exactly like a quiet week, and historically the
         first thing anyone noticed was a customer asking why the AI had
         stopped replying.
    """
    from sqlalchemy import func, select

    from app.core.policy import poll_is_stale
    from app.database import AsyncSessionLocal
    from app.models.email_thread import EmailThread
    from app.models.integration import Integration
    from app.models.tenant import Tenant
    from app.services.sendgrid import send_email

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    async with AsyncSessionLocal() as db:
        tenants_result = await db.execute(select(Tenant).where(Tenant.is_active == True))  # noqa: E712
        tenants = tenants_result.scalars().all()

        for tenant in tenants:
            # Throttle: max one alert per tenant per 24h
            if tenant.last_error_alert_at and tenant.last_error_alert_at > day_ago:
                continue

            subject = None
            body = None

            # ── Mode 2: is the mailbox connection itself broken? ──────────
            integrations = (
                (
                    await db.execute(
                        select(Integration).where(
                            Integration.tenant_id == tenant.id,
                            Integration.is_active == True,  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
            )
            broken = [
                i
                for i in integrations
                if i.last_poll_error or poll_is_stale(i.last_poll_at, now)
            ]
            if broken:
                mailbox = broken[0].gmail_address or "your mailbox"
                log.warning(
                    "tenant_integration_unhealthy",
                    tenant_id=str(tenant.id),
                    gmail=mailbox,
                    error=broken[0].last_poll_error,
                )
                subject = "AccountFlow: action needed — we can't read your inbox"
                body = (
                    f"Hi {tenant.name},\n\n"
                    f"AccountFlow has stopped being able to check {mailbox}.\n\n"
                    f"New customer emails are NOT being answered right now. Your "
                    f"mail itself is untouched and nothing has been lost — but "
                    f"please treat that inbox manually until this is fixed.\n\n"
                    f"This usually means the Google connection expired or was "
                    f"revoked. Reconnecting it from your settings normally "
                    f"resolves it. We've been alerted too.\n\n"
                    f"— AccountFlow"
                )

            # ── Mode 1: mail arrives, but processing keeps failing ────────
            if subject is None:
                counts = await db.execute(
                    select(
                        func.count(EmailThread.id).filter(EmailThread.status == "failed"),
                        func.count(EmailThread.id),
                    ).where(
                        EmailThread.tenant_id == tenant.id,
                        EmailThread.created_at >= day_ago,
                    )
                )
                failed, total = counts.one()
                if total and failed >= 3 and (failed / total) >= 0.10:
                    log.warning(
                        "tenant_error_rate_alert",
                        tenant_id=str(tenant.id),
                        failed=failed,
                        total=total,
                    )
                    subject = "AccountFlow: some of your emails need attention"
                    body = (
                        f"Hi {tenant.name},\n\n"
                        f"In the last 24 hours, {failed} of {total} incoming emails "
                        f"could not be processed automatically.\n\n"
                        f"They are safe — nothing was lost — but the AI could not "
                        f"handle them, so please review your inbox for anything "
                        f"urgent. Our team has been notified and is investigating.\n\n"
                        f"— AccountFlow"
                    )

            if subject is None:
                continue

            await asyncio.to_thread(
                send_email, to_email=tenant.email, subject=subject, body=body
            )
            tenant.last_error_alert_at = now

        await db.commit()
