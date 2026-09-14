# ADR-002: A thread and a draft belong to exactly one mailbox
Status: accepted (implemented 2026-09-13, migration `009`)
Date: 2026-09-13
Source: `docs/architecture_review_2026-09-13.md` §2, "providers" finding; ADR-001 implementation note 1.

## Context
`email_threads` and `drafts` did not record which integration they came from. The mailbox was reconstructed at send time: `approve_and_send` selected *any* active integration for the tenant, ordered by `updated_at` (`services/draft_actions.py:58-66` before this change). With one mailbox per tenant that is correct by accident; with a Google and a Microsoft mailbox on the same tenant — which `UniqueConstraint(tenant_id, provider)` already permits — a Graph-sourced draft could be handed to `gmail.send_reply` with a Graph `conversationId` as `threadId`, and the reply would leave from the wrong address with broken threading. The same gap forced the stuck-thread sweeper to refuse re-dispatch for any tenant with more than one mailbox, and it is the reason the roadmap's multi-inbox item could not start.

Two column names lied about the same abstraction: `integrations.gmail_address` has held the Microsoft mailbox since the Graph provider landed (`api/routes/auth.py`, `mail_provider.py`), and `drafts.sendgrid_message_id` has only ever stored a Gmail or Graph message id (flagged in the July review report §1).

## Decision
The mailbox becomes part of the data. Migration `009` adds `email_threads.integration_id` and `drafts.integration_id` — `NOT NULL` foreign keys to `integrations.id`, indexed, `ON DELETE NO ACTION` — and renames `gmail_address` → `mailbox_address` and `sendgrid_message_id` → `sent_message_id`. The poller writes `integration_id` on every thread it creates; the worker copies it onto the draft; `approve_and_send` loads exactly that integration (with the tenant predicate) and refuses with 422 if it is inactive. Every queue message and every re-dispatch carries the `(tenant_id, integration_id, thread_id)` triple the thread was born with.

## Tenant-isolation check
`integration_id` is written only from an `Integration` row already loaded under the tenant's own context, never from a message or a client. `approve_and_send` still filters by `draft.tenant_id`, and under ADR-001 a mispaired `(thread, integration)` — the wrong tenant's integration id arriving in a stale queue message — resolves to zero rows, so nothing sends (`tests/test_worker_e2e.py`). Nothing crosses tenants here; the change narrows *which mailbox within a tenant* can send.

## Resource check
Two UUID columns and two indexes; no new container, no RAM change. The backfill for pre-existing rows uses the same "most recently updated active integration" rule the old code applied at send time, so it is no worse than the status quo — and no live tenant has been connected, so in practice there is nothing to backfill. A thread whose tenant has *no* integration cannot be backfilled and `SET NOT NULL` fails loudly rather than guessing.

## Alternatives rejected
- Keep deriving the mailbox at send time — that *is* the bug.
- Store the provider name on the thread instead of the FK — breaks the moment a tenant has two mailboxes of the same provider (multi-inbox), and cannot be joined.
- `ON DELETE CASCADE` — deleting a mailbox row would silently destroy its email history and drafts. Disconnect is `is_active = false`, not a `DELETE`, so nothing needs the cascade. The cost of `NO ACTION` is that the future 12-month purge job must delete children first (drafts and tasks, then `email_threads`, then `integrations`); that is written into migration `009`'s comment where the job's author will find it.
- `ON DELETE SET NULL` — incompatible with `NOT NULL`, and a null mailbox on a pending draft is exactly the "any active one" ambiguity this removes.
- Doing the renames in a separate migration — pointless churn; the tables are open anyway.

## Consequences
Easier: multi-inbox (several mailboxes of one provider) is now a matter of dropping the `(tenant_id, provider)` unique constraint; the stuck-thread sweeper re-dispatches from `thread.integration_id` instead of failing multi-mailbox tenants; RLS (ADR-001) can make a mispairing structurally harmless. Harder: `EmailThread` and `Draft` can no longer be created without an integration, so fixtures and future import paths must supply one. Revisit when a mailbox is *deleted* rather than deactivated (would need an archive of its threads first) or when a draft should be sendable from a different mailbox than the one it arrived on (a deliberate override column, not a lookup).

## Implementation notes
1. `alembic/versions/009_mailbox_ownership.py` — renames, columns, backfill, `NOT NULL`, indexes; `downgrade()` reverses all of it.
2. `models/integration.py`, `models/email_thread.py`, `models/draft.py` — model parity.
3. `worker/tasks.py` — `integration_id=integration.id` on the thread insert and the draft insert; sweeper re-dispatches from `thread.integration_id`.
4. `services/draft_actions.py` — load the draft's integration by id + tenant; `sent_message_id`.
5. `api/routes/auth.py`, `services/gmail.py`, `services/mail_provider.py` — `mailbox_address`.
6. `tests/test_worker_e2e.py` — the pipeline records the mailbox and `approve_and_send` sends from it.
