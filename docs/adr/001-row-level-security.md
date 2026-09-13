# ADR-001: Row-level security as the tenant-isolation backstop
Status: proposed
Date: 2026-09-13
Source: `docs/architecture_review_2026-09-13.md` §4 (architect subagent); prerequisites in §5, items 4–6.

## Context
Isolation today is application-level only. It is well enforced — `tests/test_route_security.py` proves every route is JWT- or admin-scoped and that no route accepts `tenant_id` from a client, and every worker write passes `tenant_id=tenant.id` explicitly — but it is one forgotten `WHERE` from a PDPA breach, and the review report has carried "consider row-level security as a backstop" since July (`docs/AccountFlow_Review_Report.md:55`). The isolation walk in the architecture review finds four points where `tenant_id` is assumed rather than carried: the poll fan-out message (`worker/tasks.py:84`), the per-email dispatch (`:201`), the unasserted `(thread, integration)` pairing (`:265-273`), and the review route, which loads a draft with no tenant predicate (`api/routes/review.py:124-127`).

Two facts decide the role strategy. The app connects as `accountflow`, which the Postgres image creates as a **superuser** and which **owns every table** (`docker-compose.yml`, `.env.example:5`, `alembic/env.py:14-17`). Superusers ignore RLS unconditionally, and table owners ignore plain `ENABLE ROW LEVEL SECURITY`. Enabling RLS without changing roles is a no-op twice over.

A third fact decides the table scope: `POST /api/auth/login` queries `tenants` by email *before* any tenant context can exist (`api/routes/auth.py:110`). Putting `tenants` under RLS breaks every login.

## Decision
Enable RLS with `FORCE` on the four tables that hold customer data — `integrations`, `email_threads`, `drafts`, `tasks` — and **not** on `tenants`. Each gets one policy, `USING (tenant_id = current_setting('app.tenant_id', true)::uuid)` with an identical `WITH CHECK`; an unset GUC yields NULL and therefore zero rows, so the default is fail-closed. Create a second, non-superuser, non-owner role `accountflow_app` with DML grants only; the API and worker connect as it, while `accountflow` stays the owner/migration role and bypasses RLS by virtue of being superuser. The tenant context is carried in a `ContextVar` and re-applied to every transaction by a SQLAlchemy `after_begin` event listener issuing `SELECT set_config('app.tenant_id', :tid, true)` — `is_local=true` so it dies with the transaction and cannot leak across pooled asyncpg connections, and re-applied on `after_begin` so it survives the mid-request commits that already happen (`api/routes/settings.py:72`, `services/draft_actions.py:106`). The API sets the ContextVar in a dependency that wraps `get_current_tenant`; each Celery task sets it once at the top from a `tenant_id` that is now an explicit argument in every queue message.

## Tenant-isolation check
- **Fan-out** is inverted: `poll_all_tenants` iterates `tenants` (not under RLS), sets the GUC per tenant, then lists that tenant's integrations. Every queue message becomes `(tenant_id, integration_id[, thread_id])`, closing assumption points #1 and #3.
- **Worker tasks** set the GUC from the message's `tenant_id` before the first query. A mispaired `(thread, integration)` now returns zero rows instead of sending from the wrong mailbox — assumption #4 becomes structurally impossible rather than assert-dependent.
- **API** sets the GUC from the JWT-derived tenant. The existing explicit `WHERE tenant_id =` clauses stay; RLS is a backstop, not a replacement.
- **Review route** requires a change in `core/security.py:69-87`: the token becomes `draft-review:<tenant_id>:<draft_id>`, so the GUC is set from the token before the draft is queried. Do this now — no live links exist, so there is no compatibility window to manage.
- **Cross-tenant sweeps** all read `tenants` first and then work per tenant inside the loop with the GUC set: `_send_weekly_digest_async` (`tasks.py:523-565`), `_check_tenant_error_rates_async` (`:602-691`), and the future 12-month purge job. `_reset_monthly_counts_async` (`:498`) touches only `tenants` and is unaffected, as are the admin routes (`tenants.py:70,84`), which is precisely why `tenants` is excluded.
- **Not covered by RLS, unchanged:** provider API calls, which are isolated by the decrypted OAuth credential, and the Claude call, which receives no tenant identifier. Add `tenant_id` to the `claude.py` log lines so LLM behaviour is attributable.

## Resource check
No new container, no new process, no measurable RAM change — RLS is a planner predicate and the required `tenant_id` indexes already exist (migrations `002:34`, `003:37`, `004:36`, `005:32`). One migration creating the role, the grants, and eight policies; `downgrade()` drops policies and disables RLS. `.env` gains a second DSN (owner for Alembic, `accountflow_app` for runtime) — `alembic/env.py:14` reads `DATABASE_URL`, so it must read `MIGRATION_DATABASE_URL` with a fallback. **Backup consequence:** roles are cluster-level and `scripts/backup_db.sh` uses `pg_dump --no-owner`, so a restore into a fresh container will not recreate `accountflow_app` — the restore comment in that script must gain a `CREATE ROLE` step or the restored app cannot connect.

## Alternatives rejected
- Schema-per-tenant — N× migrations and a Phase 4 shape; not for three pilots.
- Database-per-tenant — multiplies Postgres memory on a ~1 GB budget.
- Application-level checks only (status quo) — one missing `WHERE` is a breach.
- `ENABLE` without `FORCE`, or keeping the superuser DSN — silently no-ops.
- Plain `SET` instead of `set_config(..., true)` — leaks to the next checkout of a pooled asyncpg connection.
- A second BYPASSRLS engine for the sweeps — doubles pool size against `max_connections=50` (`docker-compose.prod.yml`); the per-tenant loop is simpler and needs no second pool.
- RLS on `tenants` — breaks login (`auth.py:110`) and every admin route for no gain: `tenants` holds no patient data.

## Consequences
Easier: a forgotten `WHERE` returns nothing instead of another clinic's mail; the 12-month purge job and any future pgvector similarity query inherit the filter for free. Harder: every new cross-tenant query must be deliberate, and a missing GUC presents as "no data", which will be debugged as a bug at least once — so log a warning when a worker path runs with the GUC unset. Testing: this cannot be verified without a real Postgres, so the worker-E2E CI job (Postgres service + `alembic upgrade head`) is a **prerequisite**, not a parallel track. Revisit when a tenant needs a second mailbox of the same provider (the grain becomes `integration_id`) or when the dashboard adds multiple users per tenant (the GUC becomes tenant + user).

## Implementation notes
1. `integrations` — migration `009`: `email_threads.integration_id` and `drafts.integration_id` FKs, rename `sendgrid_message_id` → `sent_message_id`, rename `gmail_address` → `mailbox_address`. Land this *before* the RLS migration so `010` only adds policies.
2. `core/security.py` — tenant id inside the review token; update `tests/test_review_links.py`.
3. `database.py` — `ContextVar` + `after_begin` listener + `tenant_scope()` context manager; drop `pool_size` to 5 / overflow 5.
4. `api/deps.py` — set the ContextVar in `get_current_tenant`; a separate setter for the review route.
5. `worker/tasks.py` — `tenant_id` as an explicit argument on all three tasks; invert the fan-out; wrap the digest and error-rate loops.
6. Migration `010` — role, grants, `ENABLE`/`FORCE`, eight policies.
7. `DEPLOY_KVM2.md` §5 and `scripts/backup_db.sh` — second DSN, `CREATE ROLE` in the restore path.
