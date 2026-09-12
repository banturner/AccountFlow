# AccountFlow — Roadmap status

**Verified against code on 2026-09-12.** This table is the only place that says what has *shipped*; the roadmap documents describe intent. Update the relevant row in the same commit as the change.

Status words: DONE · PARTIAL · OPEN · VERIFY (believed true, not confirmed in code).

## Phase 1 — Harden & first customer (Jul–Aug 2026)

| Item | Status | Evidence |
|---|---|---|
| OAuth tokens → PostgreSQL, Fernet-encrypted, row lock on refresh | DONE | `app/models/integration.py`, `get_access_token` in `services/gmail.py` and `services/outlook.py`, migration `002` |
| Monthly email cap per plan | DONE | `app/config.py` `plan_caps`; enforced in `worker/tasks.py` `_process_tenant_inbox_async` |
| Manual onboarding runbook | DONE | `accountflow/ONBOARDING_RUNBOOK.md` |
| Sentry error alerting | PARTIAL | `sentry_sdk.init` in `app/main.py`; worker init added 2026-09-12 in `worker/celery_app.py` (before that, `capture_exception` in tasks was a no-op). Alert rules live in the Sentry UI — VERIFY |
| PDPA baseline | PARTIAL | Paperwork DONE (`docs/PDPA_Baseline_Pack.md`). OPEN: DPA acceptances, DPO appointment, breach-response note — founder actions |
| Celery Beat → APScheduler | OPEN | `beat` service still in `docker-compose.yml` |
| Structured logging | DONE | `structlog` via `app/core/logging.py` |
| Staging environment | OPEN | — |
| CI running pytest before merge | DONE 2026-09-12 | `.github/workflows/ci.yml` on every push to `banturner/AccountFlow`; suite is 117 tests, passing locally under Python 3.12 (`pytest.ini` sets `pythonpath = .`). Branch protection requiring the check is not yet enabled |

## Phase 2 — Dashboard & self-serve (Sep–Oct 2026)

| Item | Status | Evidence |
|---|---|---|
| JWT tenant auth, `tenant_id` from the verified token | DONE (early, Jul 2026) | `app/core/tokens.py`, `app/api/deps.py`; enforced by `tests/test_route_security.py` |
| Edit draft body before approving | PARTIAL | Via the emailed review page (`api/routes/review.py`); no `PATCH /api/drafts/{id}` on the JWT API |
| `/api/settings/profile`, `/plan`, `/automation` | DONE | `api/routes/settings.py` |
| Pagination on list endpoints | OPEN | No `page` / `per_page` parameters in any route |
| Weekly digest Monday 09:00 SGT | DONE | `worker/celery_app.py` `send-weekly-digest` |
| Next.js dashboard | OPEN | — |
| Customer self-onboarding | OPEN | OAuth connect exists (`/api/auth/{google,microsoft}/start`); no signup flow |
| Stripe billing | OPEN | — |

## Phase 3 — Automation depth (Nov–Dec 2026)

| Item | Status | Evidence |
|---|---|---|
| Autonomous send with per-tenant threshold | DONE (early) — opt-in, default OFF | `app/core/policy.py` `should_auto_send`; `PUT /api/settings/automation`. OPEN: 5-minute undo window |
| Haiku → Sonnet escalation | DONE | `services/claude.py` (per review report) |
| Custom system prompt per tenant | PARTIAL | `integration.system_prompt` per mailbox; no `tenant_profiles` table |
| Multi-inbox | PARTIAL | One Google + one Microsoft mailbox per tenant (`UniqueConstraint(tenant_id, provider)`); not several of the same provider |
| Calendar conflict detection | DONE | `check_availability` fails closed; clash → task titled "Clash — …", thread flagged |
| Rejection-reason capture | VERIFY | check `services/draft_actions.py` |
| WhatsApp channel | OPEN | — |

## Phase 4 — Multi-tenant & scale (Jan–Mar 2027)

| Item | Status | Evidence |
|---|---|---|
| `tenant_id` FK on every table (shared schema) | DONE since migration `001` | — |
| Postgres row-level security backstop | OPEN | Recommended in the review report; needs an ADR (session variable per request/task) |
| Kubernetes / managed Postgres & Redis | OPEN | Not before Phase 1 debt closes |
| Claude Batch API | OPEN | — |
| 12-month data purge job | OPEN | **Promised in the PDPA pack — build before the first renewal** |
| SLA monitoring | PARTIAL | Hourly per-tenant dead-connection and error-rate alert emails (`check_tenant_error_rates`); no ops-side alert |

## Shipped outside the roadmap

| Item | Status | Evidence |
|---|---|---|
| Microsoft 365 / Graph provider | PARTIAL — written Aug 2026, **not yet run against a live tenant** | `services/outlook.py` header; decision procedure in `accountflow/OAUTH_DECISION_TESTS.md`; unit tests added 2026-09-12 |
| Emailed draft-review links (GET renders, POST acts) | DONE | `api/routes/review.py`, `core/security.py` |
| Token usage per email | DONE | `email_threads.ai_input_tokens` / `ai_output_tokens` |
| Message-level dedup + reply threading | DONE | `worker/tasks.py`, `services/gmail.py`, migration `006` |
| Provider abstraction | DONE | `services/mail_provider.py` |

## Open security items (from `docs/AccountFlow_Review_Report.md` §2)

| Item | Status | Notes |
|---|---|---|
| Narrow Google scopes (`gmail.modify` + `calendar` → `gmail.readonly` + `gmail.send` + `calendar.events`) | OPEN — founder decision | Forces every connected Gmail to re-consent |
| Fernet key rotation via `MultiFernet` | OPEN | — |
| Sentry `send_default_pii=False` explicit | DONE 2026-09-12 | `app/main.py`, `worker/celery_app.py` |
| Auto-send keyword gate (prices / refunds / medical advice) | OPEN | — |
| Refuse to boot in production with placeholder secrets | DONE 2026-09-12 | `app/config.py`; `tests/test_config_guard.py` |
| Route-level tenant-scoping audit as a test | DONE 2026-09-12 | `tests/test_route_security.py` |
| Secret scanning | PARTIAL | `gitleaks` in CI and `.pre-commit-config.yaml`; run `pre-commit install` locally |

## Test coverage

Present: JWT sign/verify/tamper, bcrypt policy, Fernet state and review tokens, policy rules, Gmail MIME parsing, Graph parsing + delta sync (410 reset, deltaLink persistence, error recording), route auth audit, config guard.

Gaps, in order of risk: the worker path end to end (needs a Postgres service in CI plus `alembic upgrade head`); Gmail `send_reply` header sanitisation against a live-shaped payload; `services/calendar.py`; `services/claude.py` prompt fixtures (benign / injection / low-confidence / over-cap).

## Infrastructure prerequisites before first deploy (KVM2, checked 2026-09-12)

| Item | Status | Where |
|---|---|---|
| `/opt/accountflow` exists | OPEN — not deployed | `DEPLOY_KVM2.md` §4 |
| Swap (Ollama's 4.7 GB model + AccountFlow would OOM without it) | DONE 2026-09-12 | 4 GB `/swapfile`, in `/etc/fstab`; `swapon --show` confirms |
| `OLLAMA_KEEP_ALIVE=60s` | DONE 2026-09-12 | systemd drop-in `/etc/systemd/system/ollama.service.d/keepalive.conf`; kitchen bots verified healthy after restart |
| Domain + TLS | OPEN | `DEPLOY_KVM2.md` §1–3 |
| Nightly `pg_dump` (the box's `backup` container ignores this volume) | OPEN — script ready | `accountflow/scripts/backup_db.sh` |
