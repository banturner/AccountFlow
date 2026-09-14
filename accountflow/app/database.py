"""Engine, session factory, and the per-request / per-task tenant context.

Tenant context (ADR-001). Postgres row-level security on integrations,
email_threads, drafts and tasks filters every statement by the session
variable `app.tenant_id`. The value travels in a ContextVar and is written to
each transaction by the `after_begin` listener below with

    SELECT set_config('app.tenant_id', :tid, true)

The trailing `true` makes it transaction-local: it dies with the transaction,
so it can never leak across pooled asyncpg connections, and because the
listener fires on every `after_begin` it survives the mid-request commits that
already happen (settings.py, draft_actions.py). With the variable unset the
policies match nothing — the default is zero rows, never another clinic's.

Three ways to set it, one per caller shape:

    bind_tenant(db, tenant_id)   API — from the verified JWT (api/deps.py) or
                                 a verified review / OAuth-state token. Also
                                 applies to the session's open transaction.
    tenant_scope(tenant_id)      worker — wraps a whole task body; reset on
                                 exit so the next task starts unbound.
    apply_tenant_context(db)     inside a cross-tenant sweep, after switching
                                 the ContextVar mid-transaction.

The same helpers bind `tenant_id` into structlog's context, so every log line
emitted under a tenant context — the Claude call included — is attributable.
"""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional, Union

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    # API + worker share Postgres max_connections=50 (docker-compose.prod.yml);
    # 5+5 per process is ample for a pilot polling three mailboxes.
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    echo=settings.environment == "development",
)


class _TenantSession(Session):
    """Sync Session behind AsyncSession, so the after_begin listener is scoped
    to this application's sessions and nothing else that imports SQLAlchemy."""


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    sync_session_class=_TenantSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


TenantId = Union[uuid.UUID, str]

_tenant_id_ctx: ContextVar[Optional[str]] = ContextVar("accountflow_tenant_id", default=None)
_SET_TENANT = text("SELECT set_config('app.tenant_id', :tid, true)")


def _normalise(tenant_id: TenantId) -> str:
    # Validate here so a malformed id fails loudly at the call site rather than
    # as a uuid cast error inside every subsequent policy evaluation.
    return str(uuid.UUID(str(tenant_id)))


def current_tenant_id() -> Optional[str]:
    """The tenant bound to the current task / request, or None."""
    return _tenant_id_ctx.get()


@contextmanager
def tenant_scope(tenant_id: TenantId) -> Iterator[str]:
    """Bind the tenant context for the duration of the block (worker tasks).

    Every transaction that BEGINS inside the block carries app.tenant_id. A
    transaction already open when the block starts does not — call
    apply_tenant_context(db) for that case.
    """
    tid = _normalise(tenant_id)
    token = _tenant_id_ctx.set(tid)
    log_tokens = structlog.contextvars.bind_contextvars(tenant_id=tid)
    try:
        yield tid
    finally:
        structlog.contextvars.reset_contextvars(**log_tokens)
        _tenant_id_ctx.reset(token)


async def apply_tenant_context(db: AsyncSession) -> None:
    """Write the current ContextVar value into the session's open transaction.

    after_begin only fires when a transaction starts, so code that changes
    tenant mid-transaction — the per-tenant loops in the worker sweeps — calls
    this after entering tenant_scope. Idempotent. With no tenant bound it
    writes '', which the policies treat as unset (zero rows).
    """
    await db.execute(_SET_TENANT, {"tid": _tenant_id_ctx.get() or ""})


async def bind_tenant(db: AsyncSession, tenant_id: TenantId) -> str:
    """API entry point: bind the request to a tenant and apply it to `db` now.

    Not reset on purpose. Each ASGI request runs in its own task with its own
    copy of the context, and the database side is transaction-local, so
    nothing outlives the request.
    """
    tid = _normalise(tenant_id)
    _tenant_id_ctx.set(tid)
    structlog.contextvars.bind_contextvars(tenant_id=tid)
    await db.execute(_SET_TENANT, {"tid": tid})
    return tid


@event.listens_for(_TenantSession, "after_begin")
def _apply_tenant_on_begin(session, transaction, connection):
    # Runs inside SQLAlchemy's greenlet bridge, so this sync execute is the
    # documented way to run SQL on the (async) connection from an ORM event.
    # Fires for savepoints too; re-applying the same value there is harmless,
    # and a rolled-back savepoint reverts to the outer transaction's value.
    tid = _tenant_id_ctx.get()
    if tid is not None:
        connection.execute(_SET_TENANT, {"tid": tid})


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
