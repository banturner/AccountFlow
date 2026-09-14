"""The tenant-context plumbing in app/database.py, without a database.

What RLS enforces is proven in tests/test_rls.py against Postgres; this file
pins the Python side — the ContextVar, the SQL the helpers emit, the listener
registration, the structlog binding — so a refactor cannot quietly stop
setting app.tenant_id while the Postgres tests keep passing on stale state.
"""
import uuid

import pytest
import structlog
from sqlalchemy import event

from app import database
from app.database import (
    _TenantSession,
    apply_tenant_context,
    bind_tenant,
    current_tenant_id,
    engine,
    tenant_scope,
)


class _FakeDb:
    """Records execute() calls; stands in for AsyncSession."""

    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


def test_context_is_unset_by_default():
    assert current_tenant_id() is None


def test_tenant_scope_sets_and_resets():
    tid = uuid.uuid4()
    with tenant_scope(tid) as bound:
        assert bound == str(tid)
        assert current_tenant_id() == str(tid)
    assert current_tenant_id() is None


def test_tenant_scope_nests_and_restores_the_outer_tenant():
    outer, inner = uuid.uuid4(), uuid.uuid4()
    with tenant_scope(outer):
        with tenant_scope(inner):
            assert current_tenant_id() == str(inner)
        assert current_tenant_id() == str(outer)


def test_tenant_scope_accepts_uuid_or_string_and_rejects_garbage():
    tid = uuid.uuid4()
    with tenant_scope(str(tid)):
        assert current_tenant_id() == str(tid)
    with pytest.raises(ValueError):
        with tenant_scope("not-a-uuid"):
            pass  # pragma: no cover
    # A failed entry must not leave anything bound.
    assert current_tenant_id() is None


def test_tenant_scope_binds_tenant_id_into_log_context():
    tid = uuid.uuid4()
    with tenant_scope(tid):
        assert structlog.contextvars.get_contextvars().get("tenant_id") == str(tid)
    assert "tenant_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
async def test_apply_tenant_context_writes_a_transaction_local_set_config():
    db = _FakeDb()
    tid = uuid.uuid4()
    with tenant_scope(tid):
        await apply_tenant_context(db)
    (statement, params), = db.calls
    assert "set_config('app.tenant_id', :tid, true)" in statement
    assert params == {"tid": str(tid)}


@pytest.mark.asyncio
async def test_apply_tenant_context_with_no_tenant_clears_the_variable():
    db = _FakeDb()
    await apply_tenant_context(db)
    (_, params), = db.calls
    assert params == {"tid": ""}


@pytest.mark.asyncio
async def test_bind_tenant_sets_the_context_and_applies_it_now():
    db = _FakeDb()
    tid = uuid.uuid4()
    assert await bind_tenant(db, tid) == str(tid)
    assert current_tenant_id() == str(tid)
    assert structlog.contextvars.get_contextvars().get("tenant_id") == str(tid)
    (statement, params), = db.calls
    assert "set_config('app.tenant_id', :tid, true)" in statement
    assert params == {"tid": str(tid)}
    # Test isolation: bind_tenant deliberately does not reset (request-scoped).
    database._tenant_id_ctx.set(None)
    structlog.contextvars.clear_contextvars()


@pytest.mark.asyncio
async def test_bind_tenant_rejects_a_malformed_id_before_touching_the_db():
    db = _FakeDb()
    with pytest.raises(ValueError):
        await bind_tenant(db, "definitely-not-a-uuid")
    assert db.calls == []


def test_after_begin_listener_is_registered_on_the_app_session_class():
    # The listener is what re-applies the tenant after every mid-request
    # commit; if it is ever detached, every second query returns zero rows.
    assert event.contains(_TenantSession, "after_begin", database._apply_tenant_on_begin)


def test_sessionmaker_uses_the_listened_session_class():
    assert database.AsyncSessionLocal.kw.get("sync_session_class") is _TenantSession


def test_pool_is_sized_for_a_shared_box():
    # API + worker must stay under Postgres max_connections=50.
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 5


# ── run_async: the worker's only way into a tenant context ────────────────


def test_run_async_binds_the_tenant_inside_the_coroutine():
    """asyncio copies the current context when it wraps a coroutine in a Task.

    Every Celery task relies on that: run_async enters the scope in the sync
    caller, and the coroutine must see it. If this ever stopped working, every
    worker query would return zero rows and every task would report success
    having done nothing.
    """
    from app.worker.tasks import run_async

    tid = uuid.uuid4()
    seen = {}

    async def _probe():
        seen["inside"] = current_tenant_id()

    run_async(_probe(), tenant_id=tid)
    assert seen["inside"] == str(tid)
    # And released afterwards, so the next task on this worker starts unbound.
    assert current_tenant_id() is None


def test_run_async_without_a_tenant_leaves_the_context_unset():
    """Cross-tenant tasks (the fan-out and the sweeps) pass no tenant and set
    the context per tenant inside their own loops."""
    from app.worker.tasks import run_async

    seen = {}

    async def _probe():
        seen["inside"] = current_tenant_id()

    run_async(_probe())
    assert seen["inside"] is None
