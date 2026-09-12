# AccountFlow.ai — Technical Roadmap
**12-Month Engineering Plan · July 2026 → June 2027**

> **Verified against code on 2026-09-12.** This document describes intent; `STATUS.md` in this folder records what has actually shipped and is updated in the same commit as the change. The code snippets below are illustrations from planning time, not the current implementation — read the file before assuming a column or function exists.

> **Engineering north star:** A multi-tenant, horizontally scalable AI email processing platform with <2-minute end-to-end latency, >99.5% uptime, and <1% unhandled error rate.

---

## Current Stack (Baseline)

```
Nginx (SSL, rate limit)
  └─ FastAPI (uvicorn, 2 workers)
       └─ Celery Worker (AI processing)
       └─ Celery Beat (120s poll scheduler)
            ├─ Redis (broker + result backend)
            ├─ PostgreSQL 16 (5 tables, Alembic migrations)
            └─ External: Gmail API, Claude Haiku, Google Calendar, SendGrid, Sentry
```

**Known technical debt entering Year 1:**
- `token.json` on disk (crash-unsafe OAuth storage)
- Celery Beat as separate process (single point of failure for scheduling)
- No web frontend (dashboard is raw API calls)
- Single VPS — no horizontal scaling
- No per-tenant data isolation (single-schema, all customers share tables)

---

## Phase 1 — Critical Fixes & Stability
### Jul – Aug 2026

### P0: OAuth Token → PostgreSQL

**Problem:** `token.json` mounted as Docker volume. Container crash mid-refresh = corrupted token = Gmail polling silently stops.

**Implementation:**
```python
# New DB column on a new `integrations` table
class Integration(Base):
    __tablename__ = "integrations"
    id            = Column(UUID, primary_key=True, default=uuid4)
    tenant_id     = Column(UUID, ForeignKey("tenants.id"))
    provider      = Column(String)          # "gmail", "gcal"
    token_data    = Column(LargeBinary)     # encrypted JSON blob
    expires_at    = Column(DateTime)
    refresh_lock  = Column(Boolean, default=False)  # row-level lock on refresh
    updated_at    = Column(DateTime)
```

- Encrypt token blob with `cryptography.fernet` using `APP_SECRET_KEY`
- Acquire `SELECT ... FOR UPDATE SKIP LOCKED` before refresh → prevents concurrent refresh race
- Alembic migration: `002_add_integrations_table.py`
- Remove `GOOGLE_TOKEN_PATH` env var and `secrets/` volume mount

**Effort:** 3 days | **Risk:** High (breaks Gmail auth if botched — test in staging first)

---

### P0: Email Monthly Cap Enforcement

**Problem:** No hard cap on emails processed per customer. A misconfigured inbox could send thousands of emails → unbounded Claude API spend.

**Implementation:**
```python
# In worker.py poll task, before processing loop:
monthly_count = db.query(func.count(EmailLog.id)).filter(
    EmailLog.tenant_id == tenant.id,
    EmailLog.created_at >= first_of_month()
).scalar()

if monthly_count >= tenant.plan.email_cap:
    logger.warning("Tenant %s hit monthly cap (%d)", tenant.id, tenant.plan.email_cap)
    sentry_sdk.capture_message("Monthly email cap hit", level="warning")
    return {"status": "cap_reached"}
```

- Plan caps (must match pricing page): Starter=500, Growth=3,000, Scale=unlimited
- Alembic migration: `003_add_plan_caps.py`

**Effort:** 1 day

---

### P1: Replace Celery Beat → APScheduler

**Problem:** Celery Beat runs as a separate Docker service. If it dies, polling stops silently. Adds operational complexity.

**Implementation:**
```python
# In worker.py — embed scheduler directly
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="Asia/Singapore")
scheduler.add_job(
    poll_and_process_emails,
    "interval",
    seconds=int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "120")),
    id="email_poll",
    replace_existing=True,
    max_instances=1,          # prevents overlap if a poll runs long
)
scheduler.start()
```

- Remove `beat` service from `docker-compose.yml`
- Add `apscheduler==3.10.4` to `requirements.txt`
- Keep Celery for async task dispatch (approve_draft, manual trigger) — only Beat goes away

**Effort:** 1 day

---

### P1: Structured Logging

**Problem:** Current logging is `logger.info(...)` freeform strings. Hard to query in production.

**Implementation:**
```python
# Replace all log calls with structured dicts via structlog
import structlog
log = structlog.get_logger()

log.info("email.processed",
    tenant_id=str(tenant_id),
    gmail_message_id=email.message_id,
    action=action,
    confidence=confidence,
    input_tokens=usage.input_tokens,
    latency_ms=int((time.time() - start) * 1000),
)
```

- Add `structlog==24.1.0` to requirements
- Configure JSON renderer for production, ConsoleRenderer for local dev
- Ingest logs to Sentry or Logtail (free tier)

**Effort:** 1 day

---

### P1: Staging Environment

**Problem:** All testing done on production VPS. One bad deploy = customer-facing outage.

**Implementation:**
- Second Hostinger VPS (KVM 1, ~$6/mo)
- Separate `.env.staging` with test Gmail account, test SendGrid key
- `deploy.sh` takes `--env staging|production` flag
- GitHub Actions CI: run `python -m pytest` before allowing merge to `main`

**Effort:** 2 days

---

## Phase 2 — Web Dashboard & Auth
### Sep – Oct 2026

### Frontend Stack Decision

**Recommendation:** Next.js 14 (App Router) + Tailwind CSS + shadcn/ui

**Rationale:**
- Server-side rendering for dashboard SEO + fast initial load
- shadcn/ui components match the existing teal/navy palette
- Deploys to Vercel free tier (separate from VPS, no Nginx change needed)
- TypeScript end-to-end for type safety against FastAPI response shapes

**Repo structure:**
```
accountflow-web/
  app/
    (auth)/login/page.tsx
    dashboard/
      drafts/page.tsx          # pending drafts list
      tasks/page.tsx           # open tasks
      log/page.tsx             # processed email history
      settings/page.tsx        # business profile, plan, API key
  components/
    DraftCard.tsx              # approve / edit / reject
    TaskCard.tsx
    StatsBar.tsx               # emails processed, drafts sent, open tasks
  lib/
    api.ts                     # typed wrappers around FastAPI endpoints
```

---

### API Changes Required for Dashboard

**New endpoints to add to `app/main.py`:**

```python
# Edit draft body before approving
PATCH /api/drafts/{id}
  body: { "body": "...", "subject": "..." }

# Business profile CRUD
GET  /api/settings/profile
PUT  /api/settings/profile
  body: { business_name, owner_name, reply_tone, ... }

# Plan + usage stats
GET  /api/settings/plan
  returns: { plan_name, email_cap, used_this_month, renews_at }

# Pagination on list endpoints
GET  /api/drafts?status=pending&page=1&per_page=20
GET  /api/tasks?status=open&page=1&per_page=20
GET  /api/log?page=1&per_page=50
```

**Auth upgrade:**
- Replace single `DASHBOARD_API_KEY` with JWT tokens
- `POST /auth/login` → returns `access_token` (15min) + `refresh_token` (30d)
- Store refresh token in HttpOnly cookie
- ~~Add `python-jose[cryptography]==3.3.0` to requirements~~ — superseded: shipped as a stdlib HS256 implementation in `app/core/tokens.py` (python-jose is unmaintained with a CVE history; see the review report)

---

### Stripe Integration

```python
# New file: app/services/billing.py
import stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

def create_subscription(customer_email: str, price_id: str) -> str:
    customer = stripe.Customer.create(email=customer_email)
    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{"price": price_id}],
        payment_behavior="default_incomplete",
        expand=["latest_invoice.payment_intent"],
    )
    return subscription.client_secret

# Webhook handler: POST /webhooks/stripe
# Events to handle:
#   customer.subscription.created  → activate tenant
#   customer.subscription.deleted  → suspend polling
#   invoice.payment_failed         → email owner warning
```

- New DB columns: `tenants.stripe_customer_id`, `tenants.stripe_subscription_id`, `tenants.status`
- Alembic migration: `004_add_stripe_fields.py`
- Add `stripe==8.x` to requirements

---

## Phase 3 — AI Depth & Reliability
### Nov – Dec 2026

### Autonomous Send Mode

```python
# In _handle_draft(), replace requires_review logic:
AUTO_SEND_CONFIDENCE = float(os.getenv("AUTO_SEND_CONFIDENCE_THRESHOLD", "0.95"))

async def _handle_draft(db, data, email, email_log_id, tenant):
    draft = Draft(
        ...
        requires_review=data["confidence"] < tenant.auto_send_threshold,
        # tenant.auto_send_threshold overrides default per customer
    )
    db.add(draft)
    db.flush()

    if not draft.requires_review:
        # Send immediately without owner approval
        result = send_approved_reply(
            to_email=draft.to_email,
            subject=draft.subject,
            body=draft.body,
            reply_to=tenant.owner_email,
        )
        if result.success:
            draft.status = "auto_approved"
            draft.sendgrid_message_id = result.message_id
```

- New `tenants.auto_send_threshold` column (default: 0.95, range 0.0–1.0)
- Configurable per customer in dashboard settings
- Auto-send log visible in dashboard with "Undo" window (5 min grace period)

---

### Model Routing: Haiku → Sonnet Escalation

```python
# In app/ai_agent.py
ESCALATION_CONFIDENCE_THRESHOLD = 0.6

def process_email(self, email: EmailMessage) -> dict:
    result = self._call_claude(email, model="claude-haiku-4-5")

    if result["data"].get("confidence", 1.0) < ESCALATION_CONFIDENCE_THRESHOLD:
        logger.info("Low confidence (%.2f) — escalating to Sonnet", result["data"]["confidence"])
        result = self._call_claude(email, model="claude-sonnet-4-6")
        result["escalated"] = True

    return result
```

- Log `model_used` and `escalated` flag in `email_logs`
- Surface escalation rate in `/api/stats` and admin dashboard
- Alert if escalation rate > 20% over 24h (may indicate system prompt issue)

---

### Custom System Prompt per Tenant

```python
# New table: tenant_profiles
class TenantProfile(Base):
    __tablename__ = "tenant_profiles"
    tenant_id          = Column(UUID, ForeignKey("tenants.id"), primary_key=True)
    business_name      = Column(String)
    business_type      = Column(String)
    business_description = Column(Text)
    owner_name         = Column(String)
    reply_tone         = Column(String, default="professional and warm")
    appointment_duration_minutes = Column(Integer, default=60)
    calendar_id        = Column(String, default="primary")
    custom_instructions = Column(Text)   # freeform override

# In AIAgent.__init__(), load profile from DB instead of env vars
profile = db.query(TenantProfile).filter_by(tenant_id=self.tenant_id).first()
self.system_prompt = build_system_prompt(profile)
```

- Editable in dashboard Settings → Business Profile
- Alembic migration: `005_add_tenant_profiles.py`
- Remove `BUSINESS_*` and `OWNER_*` env vars in favour of DB-driven config

---

## Phase 4 — Multi-Tenant & Scale
### Jan – Mar 2027

### Multi-Tenant DB Schema

**Strategy:** Shared schema with `tenant_id` FK on every table (simpler than schema-per-tenant for this scale).

```sql
-- New top-level table
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR NOT NULL,
    owner_email     VARCHAR NOT NULL UNIQUE,
    status          VARCHAR NOT NULL DEFAULT 'active',   -- active|suspended|cancelled
    plan_id         UUID REFERENCES plans(id),
    stripe_customer_id VARCHAR,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Add tenant_id FK to all existing tables
ALTER TABLE email_logs      ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE tasks           ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE drafts          ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE calendar_events ADD COLUMN tenant_id UUID REFERENCES tenants(id);

-- Indexes
CREATE INDEX idx_email_logs_tenant      ON email_logs(tenant_id, created_at DESC);
CREATE INDEX idx_tasks_tenant_status    ON tasks(tenant_id, status);
CREATE INDEX idx_drafts_tenant_status   ON drafts(tenant_id, status);
```

- Alembic migration: `006_multi_tenant.py` (data migration: assign all existing rows to `tenant_id = SEED_TENANT_ID`)
- All DB queries in FastAPI and Worker must include `tenant_id` filter — add SQLAlchemy query helper

---

### Kubernetes Migration

**Target:** DigitalOcean Kubernetes (DOKS) — 2-node cluster, autoscale to 5.

```yaml
# k8s/deployments/worker.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: accountflow-worker
spec:
  replicas: 2
  selector:
    matchLabels:
      app: accountflow-worker
  template:
    spec:
      containers:
      - name: worker
        image: registry.digitalocean.com/accountflow/app:latest
        command: ["celery", "-A", "app.worker", "worker", "--loglevel=info"]
        envFrom:
        - secretRef:
            name: accountflow-secrets
        resources:
          requests: { cpu: "250m", memory: "512Mi" }
          limits:   { cpu: "1000m", memory: "1Gi" }
```

**Services:**
- `accountflow-api`: FastAPI, 2→4 replicas behind DO Load Balancer
- `accountflow-worker`: Celery Worker, 2→8 replicas (autoscale on Redis queue depth)
- `postgres`: DO Managed PostgreSQL (no longer self-hosted)
- `redis`: DO Managed Redis

**CI/CD:**
```
GitHub push → GitHub Actions →
  1. pytest (unit + integration)
  2. docker build + push to DO Container Registry
  3. kubectl rollout restart (zero-downtime rolling deploy)
```

---

### Claude Batch API Integration

```python
# For non-urgent email processing (overnight digest, bulk historical)
# app/services/batch_processor.py

import anthropic

def submit_batch(emails: list[EmailMessage], tenant_id: str) -> str:
    client = anthropic.Anthropic()
    requests = [
        {
            "custom_id": f"{tenant_id}:{email.message_id}",
            "params": {
                "model": "claude-haiku-4-5",
                "max_tokens": 1024,
                "tools": TOOLS,
                "tool_choice": {"type": "any"},
                "messages": [build_email_message(email)],
            }
        }
        for email in emails
    ]
    batch = client.messages.batches.create(requests=requests)
    return batch.id   # poll for completion, ~50% cost reduction

# New Celery task: process_batch_results(batch_id, tenant_id)
# Scheduled: midnight SGT daily for any emails queued as "batch_pending"
```

- Add `processing_mode` column to `email_logs`: `realtime | batch`
- Customer can opt in to "batch mode" in settings (trades latency for cost savings)

---

## Phase 5 — Expand & API Platform
### Apr – Jun 2027

### Public REST API

```
POST /v1/process
  Authorization: Bearer {api_key}
  body: {
    "from_email": "patient@example.com",
    "subject": "Appointment request",
    "body": "...",
    "tenant_id": "..."
  }
  returns: {
    "action": "create_calendar_appointment",
    "data": { ... },
    "confidence": 0.94,
    "processed_at": "2027-04-15T10:30:00Z"
  }

GET /v1/drafts?status=pending
GET /v1/tasks?status=open
POST /v1/drafts/{id}/approve
```

- Versioned under `/v1/`
- Rate limited per API key (100 req/min default)
- API key management in dashboard
- OpenAPI spec auto-generated by FastAPI, published at `docs.accountflow.ai`

---

### Webhook Outbound Events

```python
# app/services/webhooks.py
# Fire-and-forget HTTP POST to customer-configured URL on key events

WEBHOOK_EVENTS = [
    "email.processed",
    "draft.created",
    "draft.approved",
    "task.created",
    "cap.reached",
]

async def fire_webhook(tenant_id: str, event: str, payload: dict):
    webhook_url = get_tenant_webhook_url(tenant_id)
    if not webhook_url:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            webhook_url,
            json={"event": event, "data": payload, "ts": datetime.utcnow().isoformat()},
            headers={"X-AccountFlow-Signature": hmac_sign(payload)},
            timeout=5.0,
        )
```

- Enables Zapier / Make integrations without them polling our API
- HMAC signature on each webhook payload for security verification

---

## Technical Debt Tracker

| Item | Priority | Phase | Notes |
|---|---|---|---|
| token.json → DB | 🔴 P0 | Phase 1 | Crash-unsafe |
| Email cap enforcement | 🔴 P0 | Phase 1 | Cost risk |
| Celery Beat → APScheduler | 🟡 P1 | Phase 1 | SPOF |
| Staging environment | 🟡 P1 | Phase 1 | No safe deploy path |
| JWT auth | 🟡 P1 | Phase 2 | Single shared key is fragile |
| Structured logging | 🟡 P1 | Phase 1 | Hard to debug in prod |
| Per-tenant system prompts | 🟢 P2 | Phase 3 | Currently env-var only |
| Multi-tenant schema | 🟢 P2 | Phase 4 | All rows share same tables |
| Self-hosted Postgres | 🟢 P2 | Phase 4 | No automated backups |
| Celery → APScheduler (full) | 🟢 P2 | Phase 4 | Or keep Celery for scale |
| PDPA compliance audit | 🟢 P2 | Phase 4 | Required for enterprise |

---

## Architecture Evolution

```
Phase 1 (now)          Phase 2–3              Phase 4–5
─────────────          ─────────────          ─────────────────────
Single VPS             Same VPS +             DOKS cluster
Nginx                  Next.js (Vercel)       DO Load Balancer
FastAPI (2 workers)    FastAPI (2 workers)    FastAPI (2–4 pods)
Celery Worker          Celery Worker          Celery Worker (2–8 pods)
Celery Beat            APScheduler            APScheduler (in worker)
Redis (self-hosted)    Redis (self-hosted)    DO Managed Redis
Postgres (self-hosted) Postgres (self-hosted) DO Managed Postgres
token.json (file)      token.json → DB        Multi-tenant DB (full)
```

---

## Dependencies & Versions to Track

| Package | Current | Watch for |
|---|---|---|
| `fastapi` | 0.111.x | v1.0 stable release |
| `sentry-sdk[fastapi]` | 2.8.0 | Breaking changes in 3.x |
| `anthropic` | latest | Batch API GA, pricing changes |
| `sqlalchemy` | 2.0.x | Minor version patches |
| `alembic` | 1.13.x | Auto-generate improvements |
| `celery` | 5.x | Phase out Beat in Phase 1 |
| `sendgrid` | 6.11.0 | v7.x migration if released |
| Python | 3.12 (Dockerfile: python:3.12-slim) | 3.13 when deps allow |
| PostgreSQL | 16 | PG 17 in Phase 4 (managed) |

---

*Last updated: June 2026 · Stack: FastAPI · PostgreSQL 16 · Celery · Claude Haiku 4.5 · Docker Compose → Kubernetes*
