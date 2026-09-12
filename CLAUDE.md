# CLAUDE.md — AccountFlow

AI email-automation SaaS for Singapore SMB clinics: reads a customer's mailbox (Gmail or Microsoft 365), classifies each email with Claude, and drafts replies / books appointments / creates tasks for the owner to approve. Inbound email may contain health data — treat every change as touching regulated personal data.

## Repo layout
- `accountflow/` — the service. FastAPI 0.111 + SQLAlchemy 2.0 async (asyncpg) · Alembic `001`–`008` · Celery 5.4 + Redis · PostgreSQL 16 · Python 3.12.
  - `app/api/routes/` tenant API (JWT) and admin tenant CRUD (`X-API-Key`) · `app/api/deps.py` (`get_current_tenant`, `require_admin_key`)
  - `app/worker/tasks.py` poll → classify → act · `app/worker/celery_app.py` Beat schedule
  - `app/services/` `mail_provider.py` fronting `gmail.py` / `outlook.py`; `claude.py`; `calendar.py`; `sendgrid.py`; `draft_actions.py`
  - `app/core/` `tokens.py` (stdlib HS256 JWT — python-jose was deliberately rejected), `security.py` (Fernet), `policy.py`, `logging.py` (structlog)
  - `tests/` pytest; `conftest.py` seeds the env
- `docs/` — canonical markdown: master prompt, roadmaps, review report, PDPA pack, `STATUS.md` (what has actually shipped), `adr/` (architecture decisions)
- `business/` — decks, spreadsheets, exports. Not in git.
- `.claude/agents/` — `architect` (read-only, returns ADRs), `integrations` (OAuth / providers, works in its own git worktree), `ai-pipeline` (Claude layer), `reviewer` (read-only second pass)

## Invariants — every change is checked against these
1. **Tenant isolation.** `tenant_id` comes only from the verified JWT (`get_current_tenant`) or the worker's per-tenant loop — never from query, body, path, header, or provider payload. Every query on `integrations`, `email_threads`, `drafts`, `tasks` filters by it. `tests/test_route_security.py` enforces this at the route level.
2. **Secrets.** OAuth tokens only in `integrations.*_enc`, Fernet-encrypted. Never in logs, exceptions, Sentry, fixtures. `app/config.py` refuses to start in production with placeholder secrets.
3. **Inbound email is hostile.** CR/LF stripped before any header; outgoing Gmail built with `EmailMessage`; Graph builds its own headers; everything rendered is HTML-escaped; review links are GET-renders / POST-acts; email content enters the Claude prompt as delimited data in the user turn.
4. **Nothing sends without a human unless the tenant opted in.** Auto-send is per-tenant, default off.
5. **Cost is bounded.** Plan cap checked before any Claude call; `max_tokens` explicit; token usage recorded per email.
6. **Async discipline.** Blocking SDKs inside `async def` go through `asyncio.to_thread`; no relationship lazy-loads in async sessions; Celery tasks use `run_async()`; dispatch after commit.
7. **The deploy target is a shared box.** Hostinger KVM2 also runs the kitchen bots and Ollama; AccountFlow gets roughly 1 GB across six containers, lives in `/opt/accountflow`, and never touches `/opt/bots`. No new container without a RAM number.

## Commands
```bash
# dev — docker-compose.override.yml is auto-loaded and adds the live-reload bind mount
cd accountflow && docker compose up --build
docker compose exec api alembic upgrade head

# tests — needs Python 3.12 + requirements; CI runs them on every push
cd accountflow && pytest

# production — ALWAYS the explicit pair. A bare `docker compose up` on the
# server would load the dev override and mount the host tree over the image.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# secret scanning before commit (one-time setup)
pip install pre-commit && pre-commit install
```
Deploy runbook: `accountflow/DEPLOY_KVM2.md`. Onboarding: `accountflow/ONBOARDING_RUNBOOK.md`.

## Documentation rules
- The code is the source of truth; `docs/STATUS.md` says what has shipped. Roadmap documents describe intent and carry a "verified against code on <date>" line — if that date is older than the change you are making, check the code.
- A change to the stack, a route, a table, or a provider updates the relevant doc and `docs/STATUS.md` **in the same commit**.
- Structural decisions get an ADR in `docs/adr/` (template in `docs/adr/README.md`) — consult the `architect` agent first.
- `.docx` / `.pdf` / `.pptx` are generated from the markdown for investors, never edited directly.

## Working rules
- Read the file you are changing in full first. `docs/AccountFlow_Review_Report.md` records four P0 crashes that came from partial reads of `worker/tasks.py`.
- Migrations: next is `009_`; every `upgrade()` has a working `downgrade()`; model and migration stay in parity.
- Tests accompany any change to auth, OAuth, token storage, sync, the worker, or the Claude prompt — then run the `reviewer` agent on the diff before merging.
- Out of scope in Year 1: on-prem, voice, fine-tuning, a proprietary LLM, a free tier.
- Never claim "PDPA-certified" or "data never leaves Singapore" — see `docs/PDPA_Baseline_Pack.md`.
