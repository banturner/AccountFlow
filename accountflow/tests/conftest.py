"""Settings for every test, plus the Postgres-backed fixtures.

The environment block must run before any app module imports Settings().

Everything below it exists for tests/test_rls.py and tests/test_worker_e2e.py,
which need a real PostgreSQL: row-level security is a database behaviour and
cannot be faked in SQLite or a mock. Those tests SKIP when no database is
reachable — the normal state on a developer laptop — and are MANDATORY in CI,
where REQUIRE_POSTGRES=1 turns the skip into a failure so the suite can never
go green by quietly not running them.

Two roles, deliberately:
  owner  (MIGRATION_DATABASE_URL) — superuser, bypasses RLS. Used only to
         create the world a test needs and to tear it down afterwards.
  app    (DATABASE_URL) — the non-owner `accountflow_app` role the API and
         worker really use. Every assertion runs through it.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DASHBOARD_API_KEY", "test-dashboard-key")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "https://example.com/api/auth/google/callback")
os.environ.setdefault("SENDGRID_API_KEY", "test")
os.environ.setdefault("SENDGRID_FROM_EMAIL", "test@example.com")
os.environ.setdefault("ENVIRONMENT", "test")


# ── Postgres availability ──────────────────────────────────────────────────

RLS_TABLES = ("integrations", "email_threads", "drafts", "tasks")
_ALL_TABLES = RLS_TABLES + ("tenants",)


def _require_postgres() -> bool:
    return os.environ.get("REQUIRE_POSTGRES", "").strip().lower() in {"1", "true", "yes"}


def _unavailable(reason: str):
    """Skip on a laptop, fail in CI."""
    if _require_postgres():
        pytest.fail(
            f"REQUIRE_POSTGRES is set but the database is unusable: {reason}. "
            "These tests verify tenant isolation and must not be skipped in CI."
        )
    pytest.skip(f"No usable PostgreSQL for this test: {reason}")


# Probe result, cached for the whole run: None = not yet probed, "" = usable,
# anything else = the reason it is not. Without this every skipped test pays
# for its own doomed connection attempt, which turns a laptop run of the suite
# from seconds into minutes.
_DB_STATUS = None
_RLS_STATUS = None


def _cached_unavailable(status):
    if status:
        _unavailable(status)


@pytest_asyncio.fixture
async def owner_engine():
    """Superuser engine: seeds and truncates. Bypasses RLS by design.

    Function-scoped, and it disposes the application's shared engine around
    every test, because asyncpg connections belong to the event loop that
    opened them and pytest-asyncio gives each test a fresh loop. Reusing a
    pooled connection across loops raises "attached to a different loop" —
    the same hazard `run_async` handles in the worker.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    global _DB_STATUS

    from app import database as app_database
    from app.config import get_settings

    _cached_unavailable(_DB_STATUS)

    await app_database.engine.dispose()

    settings = get_settings()
    url = settings.migration_database_url or settings.database_url
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            missing = []
            for table in _ALL_TABLES:
                exists = await conn.scalar(text("SELECT to_regclass(:t)"), {"t": table})
                if exists is None:
                    missing.append(table)
            if missing:
                _DB_STATUS = (
                    f"tables not migrated: {', '.join(missing)} (run `alembic upgrade head`)"
                )
    except Exception as e:  # noqa: BLE001 — any connection failure is the same answer
        _DB_STATUS = f"{type(e).__name__}: {e}"

    if _DB_STATUS:
        await engine.dispose()
        _unavailable(_DB_STATUS)
    _DB_STATUS = ""

    yield engine

    await engine.dispose()
    await app_database.engine.dispose()


@pytest_asyncio.fixture
async def rls_enforced(owner_engine):
    """Guard: the app role must actually be subject to RLS.

    Without this a misconfigured run — app and owner as the same role, or the
    role granted BYPASSRLS — would make every isolation assertion below pass
    for the wrong reason, which is worse than not testing at all.
    """
    global _RLS_STATUS

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from app.config import get_settings

    _cached_unavailable(_RLS_STATUS)

    app_role = make_url(get_settings().database_url).username
    async with owner_engine.connect() as conn:
        owner_role = await conn.scalar(text("SELECT current_user"))
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"),
                {"r": app_role},
            )
        ).first()
        # ENABLE and FORCE are separate flags and a policy is a third thing:
        # a table can be FORCE-marked with RLS disabled, or enabled with no
        # policy at all. Check all three, or this guard reports "healthy" for
        # a database in which isolation is off.
        unprotected = []
        for table in RLS_TABLES:
            flags = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity"
                        " FROM pg_class WHERE relname = :t"
                    ),
                    {"t": table},
                )
            ).first()
            policies = await conn.scalar(
                text("SELECT count(*) FROM pg_policies WHERE tablename = :t"), {"t": table}
            )
            if flags is None:
                unprotected.append(f"{table} (missing)")
            elif not flags.relrowsecurity:
                unprotected.append(f"{table} (RLS disabled)")
            elif not flags.relforcerowsecurity:
                unprotected.append(f"{table} (not FORCEd)")
            elif not policies:
                unprotected.append(f"{table} (no policy)")

    if app_role == owner_role:
        _RLS_STATUS = (
            f"DATABASE_URL and MIGRATION_DATABASE_URL both use {app_role!r}; "
            "the table owner bypasses RLS so nothing would be enforced"
        )
    elif row is None:
        _RLS_STATUS = f"role {app_role!r} does not exist (run `alembic upgrade head`)"
    elif row.rolsuper or row.rolbypassrls:
        _RLS_STATUS = f"role {app_role!r} is superuser/BYPASSRLS, so RLS would not apply"
    elif unprotected:
        _RLS_STATUS = f"row-level security is not in force on: {', '.join(unprotected)}"

    if _RLS_STATUS:
        _unavailable(_RLS_STATUS)
    _RLS_STATUS = ""
    return True


@pytest_asyncio.fixture
async def clean_db(owner_engine, rls_enforced):
    """Empty the world before and after each test, as the owner."""
    from sqlalchemy import text

    async def _truncate():
        async with owner_engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} CASCADE"))

    await _truncate()
    yield owner_engine
    await _truncate()


# ── Seeding ────────────────────────────────────────────────────────────────


class SeededTenant:
    """Plain ids for one tenant's seeded world — no ORM objects, so nothing
    here can lazy-load or carry a session between tests."""

    def __init__(self, tenant_id, integration_id, name, email, mailbox):
        self.id = tenant_id
        self.integration_id = integration_id
        self.name = name
        self.email = email
        self.mailbox = mailbox

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<SeededTenant {self.name} {self.id}>"


async def seed_tenant(
    owner_engine,
    *,
    name: str,
    mailbox: str,
    provider: str = "google",
    is_active: bool = True,
    auto_send_enabled: bool = False,
    plan: str = "starter",
) -> SeededTenant:
    """Create one tenant with one connected mailbox, as the owner."""
    from sqlalchemy import text

    from app.core.security import encrypt_token

    tenant_id = uuid.uuid4()
    integration_id = uuid.uuid4()
    email = f"{name.lower().replace(' ', '-')}@example.com"

    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, email, plan, is_active, auto_send_enabled,"
                " auto_send_threshold, monthly_email_count)"
                " VALUES (:id, :name, :email, :plan, :active, :auto, 0.95, 0)"
            ),
            {
                "id": tenant_id,
                "name": name,
                "email": email,
                "plan": plan,
                "active": is_active,
                "auto": auto_send_enabled,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO integrations (id, tenant_id, provider, mailbox_address,"
                " access_token_enc, refresh_token_enc, is_active)"
                " VALUES (:id, :tenant_id, :provider, :mailbox, :access, :refresh, true)"
            ),
            {
                "id": integration_id,
                "tenant_id": tenant_id,
                "provider": provider,
                "mailbox": mailbox,
                "access": encrypt_token("access-token"),
                "refresh": encrypt_token("refresh-token"),
            },
        )
    return SeededTenant(tenant_id, integration_id, name, email, mailbox)


async def seed_thread(
    owner_engine,
    tenant: SeededTenant,
    *,
    status: str = "processing",
    subject: str = "Appointment request",
    body: str = "Can I book Friday at 3pm?",
    sender: str = "patient@example.com",
    age: timedelta = timedelta(0),
    integration_id=None,
):
    """Create one inbound email thread, as the owner."""
    from sqlalchemy import text

    thread_id = uuid.uuid4()
    created = datetime.now(timezone.utc) - age
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO email_threads (id, tenant_id, integration_id, gmail_thread_id,"
                " gmail_message_id, rfc_message_id, sender_email, sender_name, subject,"
                " body_text, status, received_at, created_at)"
                " VALUES (:id, :tenant_id, :integration_id, :thread, :message, :rfc, :sender,"
                " 'A Patient', :subject, :body, :status, :created, :created)"
            ),
            {
                "id": thread_id,
                "tenant_id": tenant.id,
                "integration_id": integration_id or tenant.integration_id,
                "thread": f"t-{thread_id}",
                "message": f"m-{thread_id}",
                "rfc": f"<{thread_id}@example.com>",
                "sender": sender,
                "subject": subject,
                "body": body,
                "status": status,
                "created": created,
            },
        )
    return thread_id


async def seed_draft(
    owner_engine,
    tenant: SeededTenant,
    thread_id,
    *,
    status: str = "pending_review",
    body: str = "We can see you Friday at 3pm.",
    integration_id=None,
):
    """Create one pending draft, as the owner."""
    from sqlalchemy import text

    draft_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO drafts (id, tenant_id, thread_id, integration_id, to_email,"
                " subject, body, status, ai_confidence)"
                " VALUES (:id, :tenant_id, :thread_id, :integration_id, 'patient@example.com',"
                " 'Re: Appointment request', :body, :status, 0.9)"
            ),
            {
                "id": draft_id,
                "tenant_id": tenant.id,
                "thread_id": thread_id,
                "integration_id": integration_id or tenant.integration_id,
                "body": body,
                "status": status,
            },
        )
    return draft_id


async def seed_task(owner_engine, tenant: SeededTenant, thread_id, *, title: str = "Follow up"):
    from sqlalchemy import text

    task_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, tenant_id, thread_id, title, status)"
                " VALUES (:id, :tenant_id, :thread_id, :title, 'open')"
            ),
            {"id": task_id, "tenant_id": tenant.id, "thread_id": thread_id, "title": title},
        )
    return task_id


@pytest_asyncio.fixture
async def two_tenants(clean_db):
    """Two clinics, each with a mailbox, a thread, a draft and a task.

    The shape every isolation test needs: if A can see any of B's four rows,
    that is the PDPA breach ADR-001 exists to prevent.
    """
    owner_engine = clean_db
    a = await seed_tenant(owner_engine, name="Clinic A", mailbox="a@clinic-a.example")
    b = await seed_tenant(owner_engine, name="Clinic B", mailbox="b@clinic-b.example")
    for tenant in (a, b):
        thread_id = await seed_thread(
            owner_engine, tenant, status="actioned", subject=f"{tenant.name} subject"
        )
        await seed_draft(owner_engine, tenant, thread_id)
        await seed_task(owner_engine, tenant, thread_id)
        tenant.thread_id = thread_id
    return a, b
