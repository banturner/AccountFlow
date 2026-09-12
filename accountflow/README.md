# AccountFlow — AI Email Automation for Singapore SMBs

## Quick Start (local dev)

```bash
# 1. Clone and copy env
cp .env.example .env
# Fill in your keys in .env

# 2. Start all services
docker compose up --build

# 3. Run migrations
docker compose exec api alembic upgrade head

# 4. Create your first tenant (admin key; see ONBOARDING_RUNBOOK.md)
curl -X POST http://localhost:8000/api/tenants \
  -H "X-API-Key: $DASHBOARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"Test Clinic","email":"owner@test.sg","password":"a-strong-password-123","plan":"starter"}'
```

## Architecture

```
Nginx (SSL)
  └── FastAPI (port 8000, 2 workers)
       ├── Celery Worker  — AI pipeline
       ├── Celery Beat    — 120s Gmail poll
       ├── Redis          — broker + results
       └── PostgreSQL 16  — 5 tables
```

## Key environment variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Haiku/Sonnet |
| `GOOGLE_CLIENT_ID/SECRET` | Gmail + Calendar OAuth |
| `FERNET_KEY` | Encrypts OAuth tokens in DB |
| `DASHBOARD_API_KEY` | API auth (Phase 1) |
| `AI_CONFIDENCE_THRESHOLD` | Auto-send threshold (default 0.85) |

## Running migrations

```bash
# Apply all
docker compose exec api alembic upgrade head

# Rollback one
docker compose exec api alembic downgrade -1
```

## Authentication

Two separate mechanisms — never mix them:

- **Tenant auth (JWT):** `POST /api/auth/login` (email + password) returns a
  15-min access token; a 30-day refresh token is set as an HttpOnly cookie.
  All tenant data endpoints require `Authorization: Bearer <access_token>`
  and are automatically scoped to that tenant — the tenant id always comes
  from the verified token, never from the client.
- **Admin auth (X-API-Key = `DASHBOARD_API_KEY`):** internal ops only —
  tenant creation/listing. Never given to customers.

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | none | Liveness check |
| POST | `/api/auth/login` | none | Email + password → JWT |
| POST | `/api/auth/refresh` | refresh cookie | New access token |
| POST | `/api/auth/logout` | refresh cookie | Clear refresh cookie |
| POST | `/api/auth/google/start` | JWT | Returns Google OAuth URL |
| GET | `/api/auth/google/callback` | signed state | OAuth callback |
| GET | `/api/drafts` | JWT | List own pending drafts |
| GET | `/api/drafts/{id}` | JWT | Get own draft |
| PATCH | `/api/drafts/{id}/approve` | JWT | Approve + send (threads correctly) |
| PATCH | `/api/drafts/{id}/reject` | JWT | Reject draft |
| GET | `/api/tasks` | JWT | List own tasks |
| PATCH | `/api/tasks/{id}` | JWT | Update own task |
| GET | `/api/emails` | JWT | Own email thread log |
| GET/PUT | `/api/settings/profile` | JWT | Tenant profile |
| GET | `/api/settings/plan` | JWT | Plan usage |
| GET/PUT | `/api/settings/automation` | JWT | Auto-send opt-in + threshold |
| POST | `/api/tenants` | admin key | Create tenant (see ONBOARDING_RUNBOOK.md) |
| GET | `/api/tenants` | admin key | List tenants |
| GET | `/api/review/{token}` | emailed link | Render a draft for approval (read-only) |
| POST | `/api/review/{token}` | emailed link | Approve / edit / reject from that page |

## How the owner actually reviews drafts

Until the dashboard exists, approval happens by email. When a draft is created
needing review, the owner is emailed the full text plus a link to a one-page
Approve / Edit / Reject screen — no login, nothing to install.

The link carries a Fernet-encrypted, single-draft, expiring token
(`REVIEW_LINK_DAYS`, default 7). Two rules make this safe to put in an inbox:

- **`GET` only renders; `POST` sends.** Mail scanners and link prefetchers
  fetch every URL in an inbound email — if approving were a `GET`, a scanner
  would silently email a patient.
- **Everything rendered is HTML-escaped.** Draft text originates from an
  untrusted inbound email, so it is treated as hostile.

Approve/reject rules live in `app/services/draft_actions.py` and are shared
with the JWT API, so the two paths cannot drift apart.

## Dev vs production compose

`docker compose up` (bare) auto-loads `docker-compose.override.yml`, which adds
the live-reload bind mount. Production names its files explicitly —
`-f docker-compose.yml -f docker-compose.prod.yml` — which skips the override,
so the built image runs, not the host tree. Never copy the override file to a
server. CI renders both configurations and fails if the mount leaks into prod.

## Running tests

```bash
pip install -r requirements.txt
pytest
```

`tests/conftest.py` seeds the required environment; nothing needs Postgres or
network. `test_route_security.py` fails if a route ships without tenant/admin
auth or accepts `tenant_id` from the client; `test_config_guard.py` proves
production refuses `.env.example` placeholders. CI runs the same suite on every
push (`.github/workflows/ci.yml`).

## Onboarding a customer

See `ONBOARDING_RUNBOOK.md` (and `../docs/PDPA_Baseline_Pack.md` before any
clinic goes live). Auto-send is **off by default** for every new tenant.
