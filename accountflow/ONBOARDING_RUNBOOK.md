# AccountFlow — Manual Onboarding Runbook (Phase 1)

Step-by-step for onboarding a new customer until self-serve exists.
Time budget: ~30 minutes per customer.

## 0. Prerequisites (once per environment)
1. `.env` fully populated (see `.env.example`) — in particular:
   - `JWT_SECRET_KEY` — `python -c "import secrets; print(secrets.token_urlsafe(64))"`
   - `FERNET_KEY` — `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `DASHBOARD_API_KEY` — internal admin key; never share with customers
2. Google Cloud project with Gmail + Calendar APIs enabled and the OAuth
   consent screen published. Redirect URI must exactly match
   `GOOGLE_REDIRECT_URI`.
3. Stack running: `docker compose up -d` then `docker compose exec api alembic upgrade head`

## 1. Create the tenant (admin API key)
```bash
curl -X POST https://yourdomain.com/api/tenants \
  -H "X-API-Key: $DASHBOARD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Bright Smile Dental",
    "email": "owner@brightsmile.sg",
    "password": "<generate a strong 16+ char password>",
    "plan": "starter"
  }'
```
Record the returned tenant `id`. Send the password to the owner through a
secure channel (not email).

## 2. Owner logs in and connects Gmail
1. Owner logs in: `POST /api/auth/login` with email + password → access token.
2. With the access token: `POST /api/auth/google/start` → returns `auth_url`.
3. Owner opens `auth_url` in a browser, signs into THE CLINIC'S Gmail account,
   and grants access. The callback stores encrypted tokens automatically.
4. Verify: first poll runs within 2 minutes — check logs for
   `fetched_messages` with the tenant id.

## 3. Pilot-safe defaults (verify, don't skip)
- Auto-send is OFF by default — every AI draft waits for owner approval.
  Leave it off for at least the first 2 weeks / first 100 emails.
- When trust is earned: `PUT /api/settings/automation`
  `{"auto_send_enabled": true, "auto_send_threshold": 0.95}`

## 4. PDPA paperwork (required BEFORE go-live for clinics)
Complete the checklist in `PDPA_Baseline_Pack.md`:
- [ ] Signed pilot agreement incl. data-processing consent clause
- [ ] Customer given the data-flow one-pager
- [ ] Retention policy communicated (default: 12 months, delete on request)

## 5. Smoke test (do this with the owner watching)
1. Send a test appointment email from a personal address to the clinic inbox.
2. Within ~2 min: the OWNER receives a "Reply ready" email containing the
   drafted reply and a review link. This is the moment that sells the product —
   let them read it themselves rather than narrating it.
3. Have the owner click the link, edit a word, and press **Approve & send**.
   Confirm the reply arrives IN THE SAME THREAD in the test mailbox.
4. Confirm the draft also appears in `GET /api/drafts` (status `sent`).
5. Check `GET /api/settings/plan` shows `monthly_email_count: 1`.

> If the review email never arrives, check `SENDGRID_*` in `.env` and that
> `APP_BASE_URL` is the address the owner can actually reach — the review link
> is built from it, so a wrong value produces a dead link.

### Appointment smoke test
Send an email asking for a slot that is already busy in the clinic's calendar.
Expect: no calendar event, and a task titled "Clash — …" with the thread
flagged for a human. The bot must never book over an existing appointment.

## 6. Week-1 follow-up
- Day 2: check error rate (`GET /api/emails?thread_status=failed`).
- Day 7: review flagged/rejected drafts with the owner; tune the tenant
  system prompt if replies miss the clinic's tone.

## Troubleshooting
| Symptom | Fix |
|---|---|
| No emails fetched | Integration inactive? Token expired? Check `gmail_history_expired` / `refreshing_oauth_token` logs |
| 401 on login | Password typo, or tenant `is_active=false` |
| Replies not threading | `rfc_message_id` null on old threads — only new mail carries it |
| `monthly_cap_reached` warnings | Upgrade plan or wait for the 1st-of-month reset |
