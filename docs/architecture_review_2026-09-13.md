# AccountFlow — whole-system architecture assessment

**Date:** 2026-09-13 · **Produced by:** the `architect` subagent (read-only, full codebase + docs) · **Spot-checked by the main session:** Google scope set (`gmail.py:41-44`, `auth.py:47-50`), pagination (`drafts.py:34`, `tasks.py:28`, `settings.py:118`), pool sizing (`database.py:10-11`), subject in the Claude log line (`claude.py:198`), arbitrary-mailbox selection (`draft_actions.py:58-66`) — all confirmed. `STATUS.md` corrected the same day for the two doc-vs-code contradictions in §2.

Companion: `adr/001-row-level-security.md` (proposed) is §4 of this review, extracted so the RLS session can work from it directly.

---

## 1. Verdict

The architecture is sound for a 3-customer pilot on the shared box. Tenant isolation is real, not aspirational: every data route derives `tenant_id` from the verified JWT and `tests/test_route_security.py` fails CI if that regresses, and every worker write carries `tenant.id` from the per-tenant loop. The shape (poll → classify → draft → human-approve → send from the tenant's own mailbox) is the right shape, and the fail-closed choices (`check_availability`, `should_auto_send`, `calendar_send_updates`) are the ones I would have made.

The single biggest structural risk before customer #1 is not isolation — it is that **the pipeline's failure modes are silent**. An email accepted into `email_threads` with `status="processing"` has no owner: if its queue message is lost, nothing retries it, nothing marks it failed, and `check_tenant_error_rates` (`tasks.py:657`) counts only `failed`, so a patient's email is never answered and the product reports itself healthy. The same silence covers a tenant sitting at their monthly cap. Fix the observability of the un-processed state before you connect a real clinic's inbox.

---

## 2. Findings, most severe first

**[HIGH] worker/queue — an accepted email can be dropped with no trace, and Redis is configured to drop it.**
Evidence: `worker/tasks.py:184` (row committed as `processing`), `:196`, `:201` (dispatch after commit); `worker/celery_app.py:21-22` (same Redis is broker *and* result backend), `:39` (`result_expires=86400`); `docker-compose.prod.yml:45-47` (`--maxmemory 96mb --maxmemory-policy allkeys-lru`). A broker must never evict; `allkeys-lru` will discard queued task messages under memory pressure, and 24h of task results share the same 96 MB. Failure: a patient emails the clinic, the row sits `processing` forever, no draft, no task, no alert, no Sentry event — the clinic discovers it when the patient phones. Decision: `--maxmemory-policy noeviction`, `task_ignore_result=True` (drop the result backend entirely — nothing reads it), and a Beat/APScheduler sweeper that re-dispatches `processing` threads older than 15 minutes and marks them `failed` after an hour so the existing alert fires. Effort: 1h config + 3h sweeper + test. ADR: no — implementation, but name the sweeper in the APScheduler ADR.

**[HIGH] worker — a capped tenant is told their mailbox is broken.**
Evidence: `tasks.py:130-138` returns before `integration.last_poll_at` is stamped at `:143`; `core/policy.py:48-61` then reports the poll stale after an hour; `tasks.py:640` emails "**action needed — we can't read your inbox**". Failure: a Starter clinic crosses 500 emails, processing correctly stops, and the product sends a false alarm telling them their Google connection died. They reconnect, it doesn't help, they churn. Compounding: `reset_monthly_email_counts` (`tasks.py:498`) only zeroes the counter and never writes `monthly_reset_at` (`models/tenant.py:46`), so if Beat is down on the 1st nothing resets and nothing can tell. Decision: stamp `last_poll_at` before the cap check; send a distinct "you've reached your plan limit" email; make the reset lazy and idempotent (derive the period from `monthly_reset_at` at poll time) so Beat downtime cannot cause it. Effort: 4h. ADR: fold into the queued Celery Beat → APScheduler ADR.

*(The counter question, answered: increment and commit are atomic — `tasks.py:193` and `:196` are one transaction with the thread insert and the provider cursor flush at `gmail.py:210-213` / `outlook.py:241`, so a crash rolls all three back and the message is re-fetched. Retries can't double-count because `uq_tenant_gmail_message` (`models/email_thread.py:16`) blocks the re-insert. The counter is fine; Beat at month end is not.)*

**[HIGH] worker — `(thread_id, integration_id)` is an unchecked pairing, and it is the cross-tenant foot-gun.**
Evidence: `tasks.py:265-273` loads both rows by id with no tenant filter, then derives the tenant from `thread.tenant_id` at `:279` — but the *credentials* used to send at `:331` come from the independently-loaded integration. Nothing asserts `integration.tenant_id == thread.tenant_id`. Today the pairing is correct by construction (`tasks.py:201`), but `task_acks_late=True`, retries, and any queue that outlives a DB restore make a stale pairing reachable. Failure if it ever happens: clinic A's patient receives a reply sent from clinic B's mailbox, and clinic B's system prompt sees clinic A's email body. Decision: a three-line assert now; under RLS this becomes structural (see ADR-001 — the task sets the GUC once and the mispaired row simply returns zero rows). Effort: 30 min. ADR: covered by ADR-001.

**[HIGH] providers — `approve_and_send` picks an arbitrary mailbox; `Integration.gmail_address` is the identity for both providers.**
Evidence: `services/draft_actions.py:58-66` selects *any* active integration for the tenant ordered by `updated_at`; the thread has no `integration_id` (`models/email_thread.py`), so a Graph-sourced draft can be handed to `gmail.send_reply` with a Graph `conversationId` as `threadId` and an `internetMessageId` as `In-Reply-To`. The abstraction leaks: `gmail_address` is read as the Microsoft mailbox identity at `mail_provider.py:204` and as the self-send filter at `tasks.py:147`, and `outlook.py` writes it from Graph claims at `auth.py:320`. Failure: a dual-mailbox tenant gets "Gmail refused the message" on approve, or worse, a reply sent from the wrong address with broken threading. `UniqueConstraint(tenant_id, provider)` (`models/integration.py:19`) is currently substituting for a missing FK; the roadmap's multi-inbox item cannot be built until that FK exists. Decision: migration `009` adds `email_threads.integration_id` (FK, nullable → backfilled → NOT NULL) and `drafts.integration_id`; `approve_and_send` uses it; rename the column generically (`mailbox_address`) in the same migration; rename `Draft.sendgrid_message_id` → `sent_message_id` (`models/draft.py:40` — confirmed, it stores a Gmail/Graph id; already flagged in the July report §1) while the migration is open. Effort: 1 day. **ADR: yes** — this is the mailbox-ownership decision and it gates multi-inbox.

**[HIGH] pipeline — a retry after a successful auto-send re-sends.**
Evidence: the send happens at `tasks.py:331` inside the session; the commit is at `:465`. Any failure in between (commit error, the `parse_dt`/attribute paths) raises, `process_single_email` retries twice (`:208-222`), and each retry re-calls Claude and re-sends. Failure: a patient receives two or three replies. Mitigated only by auto-send defaulting off. Same shape on the human path: two concurrent POSTs to `/api/review/{token}` both pass `_require_pending` (`draft_actions.py:33-38`) before either commits — a double-tap on a phone can double-send. Decision: persist a `sent_message_id` and short-circuit on retry if set; take `SELECT … FOR UPDATE` on the draft in `approve_and_send`. Effort: 4h. ADR: no.

**[HIGH] logging/PDPA — the email subject reaches stdout.**
Evidence: `services/claude.py:198` logs `subject=subject[:80]`. Clinic subjects are health-revealing ("Re: my swelling after Tuesday's filler"). No email *body* reaches any log or any notification email — checked `sendgrid.py:94-108` (digest is counts only) and `tasks.py:640-681` (alerts carry the mailbox address and counts only), so both PDPA promises hold. But `gmail.py:286`, `sendgrid.py:44` and `outlook.py:274` log recipient addresses and Graph error bodies, and `docker-compose.prod.yml` sets no `logging:` driver limits, so these persist unbounded on a box shared with three other projects. Related and separate: **`configure_logging()` is never called in the worker** — `main.py:21` only; `worker/celery_app.py` has no call — so the worker's output is not the JSON structlog `STATUS.md` claims. Decision: drop the subject from that log line (keep `thread_id`), hash or drop addresses, add `logging: driver: json-file, max-size: 10m, max-file: 3`, call `configure_logging()` from a Celery `setup_logging` signal. Effort: 3h. ADR: no.

**[MEDIUM] PDPA — the data-flow one-pager given to every customer understates SendGrid.**
Evidence: `docs/PDPA_Baseline_Pack.md:19-20` says replies go from the clinic's own Gmail and "**Nothing is sent to any other party**", and that emails to the owner are "counts only, no email content" — but `services/sendgrid.py:66-77` mails the full AI draft, the patient's address and the subject through SendGrid on every pending draft. SendGrid is listed as a sub-processor, so this is a disclosure-accuracy gap, not an unlawful flow. Failure: the sentence handed to a clinic is untrue, in writing, about health data. Decision: amend §2 of the pack to describe the review notification explicitly; optionally trim the notification to subject + link. Effort: 1h (doc). ADR: no.

**[MEDIUM] review links — the token binds a draft, not a tenant, and lives 7 days.**
Evidence: `core/security.py:69-75` encodes `draft-review:<draft_id>` only; TTL from `config.py:77` applied at `review.py:108`; `review.py:124-127` is the one route in the app that loads a row with no tenant predicate. Replay after approval is **not** possible — `_require_pending` 409s and the GET renders "Already handled" without the patient text (`review.py:145-150`). What remains: while a draft is pending, the URL is a 7-day bearer credential to the patient's original email text (`review.py:169`), forwardable and unrevocable, sealed with the same global Fernet key as OAuth state (`security.py:14`) — so MultiFernet rotation will invalidate live review links too. Decision: put the tenant id inside the token (`draft-review:<tenant_id>:<draft_id>`) **before customer #1**, because ADR-001 needs it to set the tenant context on this route; shorten `REVIEW_LINK_DAYS` to 3. Effort: 2h. ADR: covered by ADR-001.

**[MEDIUM] KVM2 — the memory caps permit 2.4 GB, not the ~1 GB the runbook promises.**
Evidence: `docker-compose.prod.yml` limits sum to 2432 MB (512+128+640+768+256+128) against `DEPLOY_KVM2.md:15`'s "0.8–1.0 GB steady". Caps are not reservations, so the steady-state figure is honest, but the ceiling is not bounded to the budget: worst case 2.4 GB + hermes3's ~5.5 GB + the bots exceeds 7.8 GB and pushes Postgres into the 4 GB swap on a 2-vCPU box. Separately, `database.py:10-11` allows 30 connections per process (API + worker = 60) against `max_connections=50` (`prod.yml:36`). Decision: api 384m, worker 512m (concurrency is 1 — one Claude call at a time), total ceiling 1.4 GB; `pool_size=5, max_overflow=5`. Effort: 1h + a `docker stats` check after deploy. ADR: no.

**[LOW] CORS will break the dashboard on day one.** `main.py:47-49`: production allows exactly `app_base_url`, which is the API's own origin; the Next.js dashboard is on Vercel, a different origin. `allow_credentials=True` with `["*"]` in development is survivable (the refresh cookie is `SameSite=Strict`, `path=/api/auth`, `auth.py:91-99`) but should still be an explicit dev-origin list. Decision: a `cors_origins` list setting. 1h, do it with the dashboard.

**[LOW] `integration.system_prompt` silently replaces the injection defence.** `claude.py:173` — a custom prompt substitutes for `DEFAULT_SYSTEM_PROMPT` entirely, including the untrusted-data rule at `:96-99`. No route sets it today (it is DB/onboarding only), so this is latent. Decision: concatenate the security block rather than replace. 30 min. Do it before the auto-send keyword gate, which depends on the same prompt being intact.

**Doc-vs-code contradictions found (code wins; STATUS.md corrected 2026-09-13):** Google scopes are *already* narrowed to `gmail.readonly` / `gmail.send` / `calendar.events` / `calendar.events.freebusy` in `services/gmail.py:40-45` and `auth.py:46-53` — STATUS listed this as OPEN; it is done, and since no live tenant is connected there is no re-consent cost. Pagination is implemented on `/api/drafts`, `/api/tasks` and `/api/emails` (`drafts.py:34-35`, `tasks.py:28-29`, `settings.py:118-119`) as `page` / `page_size` — STATUS said OPEN.

---

## 3. Tenant-isolation walk — one inbound email, end to end

| # | Step | `tenant_id` |
|---|---|---|
| 1 | Beat fires `poll_all_tenants` (`celery_app.py:43`) | **absent** — cross-tenant by design |
| 2 | `_poll_all_tenants_async` selects all active integrations (`tasks.py:72-79`) | **absent**; fan-out message carries `integration_id` only (`:84`) — **assumption #1** |
| 3 | `_process_tenant_inbox_async` loads Integration by id, no tenant filter (`:113-116`); tenant derived from `integration.tenant_id` (`:122`) | **derived** — this is the authoritative derivation |
| 4 | `fetch_new_messages` (`mail_provider.py:71-81` → `gmail.py:65` / `outlook.py:117`) | **absent** — isolation is by decrypted OAuth credential; the Fernet key is global, so a wrong row decrypts cleanly — **assumption #2** |
| 5 | Self-send filter (`tasks.py:147-152`), dedup query (`:157-162`) | **carried** — explicit `EmailThread.tenant_id == tenant.id` |
| 6 | Thread insert (`:172-184`) + `uq_tenant_gmail_message` | **carried**, column + DB constraint |
| 7 | Counter (`:193`), commit (`:196`) | **carried** on the tenant row |
| 8 | Dispatch `process_single_email(thread_id, integration_id)` (`:201`) | **absent from the message** — **assumption #3** |
| 9 | `_process_single_email_async` loads thread and integration by id, no filter (`:265-273`); tenant from `thread.tenant_id` (`:279-281`) | **derived from the thread only**; the integration is trusted, unasserted — **assumption #4** |
| 10 | Claude call (`:288-295` → `claude.py:151-204`) | **absent** — carries `tenant.name` and `integration.system_prompt`; no tenant id in the call or in its log lines (`claude.py:196,206`), so a bad classification is not attributable. No cache, no shared state ⇒ no leak path today |
| 11 | Draft insert (`:317-325`), Task inserts (`:374-380`, `:436-442`) | **carried**, explicit |
| 12 | Calendar create / availability (`:392-410`) | **absent** — credential-scoped, as #4 |
| 13 | `create_review_token(draft.id)` (`:363-366` → `security.py:75`) | **absent** — draft id only, global key |
| 14 | Review email (`:479` → `sendgrid.py:51`) | **carried** as `tenant.email`, captured pre-commit at `:358` |
| 15 | `GET/POST /api/review/{token}` (`review.py:124-127`) | **absent** — the only route with no tenant predicate; token is the authority |
| 16 | `approve_and_send` (`draft_actions.py:58-66`) | **carried** (`draft.tenant_id`) but **mailbox is not** — arbitrary integration |
| 17 | `send_reply` (`mail_provider.py:102-125`) | **absent** — credential-scoped |
| — | JWT path, for contrast: `deps.py:60-67` → predicates at `drafts.py:41,62,83,106`; `tasks.py:33,54,66`; `settings.py:125` | **carried everywhere** |
| — | Cross-tenant sweeps with no context: `tasks.py:498` (unfiltered `UPDATE tenants`), `:523-526` (digest), `:602-606` (error rates); `tenants.py:70,84` (admin) | **absent by design** |
| — | Log lines: `tasks.py:136,144` carry `tenant_id`; `:466-472` and all of `claude.py` do not. No cache layer, no cache keys. Celery result keys are tenant-agnostic UUIDs in shared Redis db 0 | mixed |

Four assumption points (#1–#4) and two credential-scoped surfaces are what ADR-001 must convert into enforcement.

---

## 4. ADR-001 — row-level security

Extracted to `adr/001-row-level-security.md` (status: proposed).

---

## 5. Recommended order of work before customer #1

1. **Redis `noeviction` + drop the result backend** (1h). Everything below assumes the queue does not lose messages; this is the cheapest change on the list and the only one that can silently destroy a patient email.
2. **Stuck-`processing` sweeper + `last_poll_at` before the cap check + a real cap email** (1 day). Until an un-processed email becomes visible, no test result from steps 3–8 can be trusted — a failure will look like success.
3. **Log hygiene: drop the subject, call `configure_logging()` in the worker, cap the docker log files** (3h). Before anything runs against a real mailbox, because logs written before this are not retroactively cleanable.
4. **Migration `009`: `integration_id` on threads and drafts, column renames** (1 day). Must precede the RLS migration so the policy migration adds only policies, and precedes the Graph live test because that test is what will expose the wrong-mailbox path.
5. **Tenant id in the review token + `SELECT … FOR UPDATE` in `approve_and_send` + the auto-send re-send guard** (4h). ADR-001 depends on the token change, and there are no live links today — the window to do it for free closes the moment a clinic is connected.
6. **Worker E2E test with Postgres in CI** (1 day, already queued). RLS cannot be verified without it; landing RLS first would mean shipping an unverifiable isolation layer.
7. **ADR-001 implementation: role, GUC plumbing, migration `010`** (2 days). After 4, 5 and 6, so the schema is final and the test harness exists.
8. **Prod memory caps trimmed, pool sizes lowered, then deploy: domain, TLS, `backup_db.sh` in cron** (1 day, `DEPLOY_KVM2.md` §1–3, §8). Deploy last among the engineering items so the box only absorbs one step change, and confirm the kitchen bots stay healthy.
9. **`OAUTH_DECISION_TESTS.md` Test A and Test B against the deployed instance** (1 day). First time Graph code touches a live mailbox; needs a real HTTPS redirect URI, so it follows the deploy, and its outcome decides which provider customer #1 uses.
10. **PDPA pack §2 amended for the SendGrid notification; DPO appointed; Anthropic and SendGrid DPAs accepted** (founder, half a day). Before a clinic signs, not after — but blocks nothing technical, so it runs in parallel from step 1.
11. *Deferred past customer #1, correctly:* MultiFernet rotation, the auto-send keyword gate (`claude.py:173` prompt-merge fix first), the 12-month purge job (before the first renewal, not before the first customer), Beat → APScheduler, staging, Stripe, the Next.js dashboard.
