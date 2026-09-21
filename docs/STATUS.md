# AccountFlow — Roadmap status

**Verified against code on 2026-09-21.** This table is the only place that says what has *shipped*; the roadmap documents describe intent. Update the relevant row in the same commit as the change.

Whole-system architecture review: `architecture_review_2026-09-13.md` — verdict, ranked findings, tenant-isolation walk, and the recommended order of work before customer #1. Its RLS design is `adr/001-row-level-security.md` (proposed).

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
| Pagination on list endpoints | DONE | `page` / `page_size` on `/api/drafts`, `/api/tasks`, `/api/emails` (`drafts.py:34`, `tasks.py:28`, `settings.py:118`). Wrongly listed OPEN on 2026-09-12 — corrected by the architecture review |
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
| Microsoft 365 / Graph provider | PARTIAL — written Aug 2026; the **OAuth path is proven against a live tenant** (2026-09-17), the provider code itself still is not | `services/outlook.py` header; decision procedure in `accountflow/OAUTH_DECISION_TESTS.md`; unit tests added 2026-09-12, extended 2026-09-21. Test B confirmed admin consent, the code-for-token exchange and a Graph inbox read by hand; `services/outlook.py` has still not polled a real mailbox. Three first-poll/delta bugs fixed 2026-09-21 (see below); `scripts/graph_live_smoke.py` runs the real provider against a live tenant as Test B step 9 |
| Emailed draft-review links (GET renders, POST acts) | DONE | `api/routes/review.py`, `core/security.py` |
| Token usage per email | DONE | `email_threads.ai_input_tokens` / `ai_output_tokens` |
| Message-level dedup + reply threading | DONE | `worker/tasks.py`, `services/gmail.py`, migration `006` |
| Provider abstraction | DONE | `services/mail_provider.py` |

## Open security items (from `docs/AccountFlow_Review_Report.md` §2)

| Item | Status | Notes |
|---|---|---|
| Narrow Google scopes | DONE (before 2026-09-12; wrongly listed OPEN) | `gmail.readonly` + `gmail.send` + `calendar.events` + `calendar.events.freebusy` in `services/gmail.py:40-45` and `api/routes/auth.py:46-51`. No live tenant was ever connected, so no re-consent cost. `gmail.readonly` is still a restricted scope (CASA) — see `OAUTH_DECISION_TESTS.md` |
| Fernet key rotation via `MultiFernet` | OPEN | — |
| Sentry `send_default_pii=False` explicit | DONE 2026-09-12 | `app/main.py`, `worker/celery_app.py` |
| Auto-send keyword gate (prices / refunds / medical advice) | OPEN | — |
| Refuse to boot in production with placeholder secrets | DONE 2026-09-12 | `app/config.py`; `tests/test_config_guard.py` |
| Route-level tenant-scoping audit as a test | DONE 2026-09-12 | `tests/test_route_security.py` |
| Secret scanning | PARTIAL | `gitleaks` in CI and `.pre-commit-config.yaml`; run `pre-commit install` locally |

## Architecture review follow-ups (`architecture_review_2026-09-13.md` §5, in order)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Redis `noeviction`; result backend dropped | DONE 2026-09-13 | `docker-compose.prod.yml` redis command; `worker/celery_app.py` `task_ignore_result=True`, no `backend=` |
| 2 | Stuck-`processing` sweeper; `last_poll_at` stamped before the cap check; lazy monthly reset; real "plan limit reached" email | DONE 2026-09-13 (reviewer pass applied same day) | `worker/tasks.py` `sweep_stuck_threads` (every 10 min; columns-only, batch of 500, inactive tenants excluded, each dispatch individually guarded so a full broker cannot abort the run), `_process_single_email_async` (row lock via `FOR UPDATE` + status re-check, so a duplicate dispatch is harmless at any concurrency; tenant/integration mismatch and inactive tenant → `failed`), `_process_tenant_inbox_async`; rules in `core/policy.py` with tests in `tests/test_policy.py`. **Owed:** session-level sweeper tests (threads at 5/20/70 min → none/redispatch/fail) belong to item 6's Postgres CI job; a `redispatched_at` column for accounting belongs in migration 009. Re-dispatch is limited to tenants with a single integration row until 009 records `integration_id` on threads |
| 3 | Log hygiene: subject removed from the Claude escalation log, recipient addresses and Graph error bodies removed from provider/SendGrid logs, `configure_logging()` wired into the worker via the Celery `setup_logging` signal, docker log rotation (10 MB × 3) on every service | DONE 2026-09-13 | `services/claude.py`, `services/gmail.py`, `services/outlook.py`, `services/sendgrid.py`, `worker/celery_app.py`, `docker-compose.yml` `x-logging` |
| 4 | Migration 009: `integration_id` on threads and drafts; `gmail_address` → `mailbox_address`; `sendgrid_message_id` → `sent_message_id`; `approve_and_send` uses the thread's mailbox | OPEN | Prerequisite for 7 |
| 5 | Tenant id inside the review token; `SELECT … FOR UPDATE` in `approve_and_send`; auto-send retry guard | OPEN | `core/security.py`, `services/draft_actions.py`, `worker/tasks.py` |
| 6 | Worker end-to-end test with Postgres in CI | OPEN | Prerequisite for 7 |
| 7 | Row-level security per `adr/001-row-level-security.md` | OPEN — session started 2026-09-13 | Blocked on 4 and 6 |
| 8 | Prod memory caps trimmed (api 384m, worker 512m), DB pool 5/5; then deploy: domain, TLS, backup cron | OPEN | `docker-compose.prod.yml`, `database.py`, `DEPLOY_KVM2.md` |
| 9 | `OAUTH_DECISION_TESTS.md` A and B against the deployed instance | OPEN — **Test B steps 1–8 PASSED 2026-09-17**; day 8 refresh check due 2026-09-25, then new step 9 (poll through `services/outlook.py`). Test A not started, deliberately: its only job was Google vs Microsoft and Test B answered that | Decides customer #1's provider. Admin consent was one click, nothing demanded certification or an audit, and Graph returned real inbox messages. Runners: `accountflow/scripts/oauth_test_microsoft.ps1` (`-Refresh` for day 8), `accountflow/scripts/graph_live_smoke.py` (step 9) |
| 10 | PDPA pack §2 amended for the SendGrid review notification; DPO named; Anthropic + SendGrid DPAs | OPEN — founder | `docs/PDPA_Baseline_Pack.md` |
| 11 | Graph provider first-poll and delta-cursor bugs — `$filter`/`$orderby` 400 `ErrorInefficientFilter` on every first poll; dropped `@odata.nextLink` stalling the cursor on any round past `max_results`; delta `isRead` updates replaying pre-connection mail into the pipeline | DONE 2026-09-21 | `services/outlook.py` `fetch_new_messages` / `_horizon`; 7 new tests in `tests/test_outlook_provider.py`. Found by review, **not yet confirmed against a live tenant** — that is Test B step 9 |

## Test coverage

Present: JWT sign/verify/tamper, bcrypt policy, Fernet state and review tokens, policy rules, Gmail MIME parsing, Graph parsing + first poll (filter/orderby shape, cursor parked before the backlog read, no cursor on a failed read) + delta sync (410 reset, deltaLink and nextLink persistence, page cap, pre-connection mail ignored, error recording), route auth audit, config guard.

Gaps, in order of risk: the worker path end to end (needs a Postgres service in CI plus `alembic upgrade head`); Gmail `send_reply` header sanitisation against a live-shaped payload; `services/calendar.py`; `services/claude.py` prompt fixtures (benign / injection / low-confidence / over-cap).

## Infrastructure prerequisites before first deploy (KVM2, checked 2026-09-12)

| Item | Status | Where |
|---|---|---|
| `/opt/accountflow` exists | OPEN — not deployed | `DEPLOY_KVM2.md` §4 |
| Swap (Ollama's 4.7 GB model + AccountFlow would OOM without it) | DONE 2026-09-12 | 4 GB `/swapfile`, in `/etc/fstab`; `swapon --show` confirms |
| `OLLAMA_KEEP_ALIVE=60s` | DONE 2026-09-12 | systemd drop-in `/etc/systemd/system/ollama.service.d/keepalive.conf`; kitchen bots verified healthy after restart |
| Domain + TLS | OPEN | `DEPLOY_KVM2.md` §1–3 |
| Nightly `pg_dump` (the box's `backup` container ignores this volume) | OPEN — script ready | `accountflow/scripts/backup_db.sh` |
