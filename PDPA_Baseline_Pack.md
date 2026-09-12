# AccountFlow — PDPA Baseline Pack (Phase 1)

**Purpose:** the minimum data-protection paperwork required before onboarding the first Singapore clinic. This is a working baseline for the pilot phase, not legal advice — have a Singapore-qualified lawyer review it during the Phase 4 compliance review, and before any enterprise deal.

---

## 1. What personal data AccountFlow touches

Inbound customer emails to the clinic's connected Gmail inbox, which may include: names, email addresses, phone numbers, appointment requests, and — because customers volunteer it — health-related information (symptoms, treatments, medications). Health data is sensitive; treat every inbound email as potentially sensitive.

Data collected about the clinic itself: business name, owner email, login credentials (bcrypt-hashed), OAuth tokens (Fernet-encrypted at rest).

## 2. Data flow (give this one-pager to every customer)

1. The clinic authorises AccountFlow to read and send email via Google OAuth. Tokens are encrypted at rest; AccountFlow staff cannot see the clinic's Gmail password.
2. Every ~2 minutes AccountFlow fetches new inbound emails over TLS.
3. Each email's text (truncated to ~4,000 characters) is sent to Anthropic's Claude API for classification and reply drafting. Anthropic acts as a sub-processor.
4. The email, the AI's decision, and any draft reply are stored in AccountFlow' PostgreSQL database (single-region VPS).
5. Replies are sent from the clinic's own Gmail address. Nothing is sent to any other party.
6. Weekly summary statistics (counts only, no email content) are emailed to the owner.

**Sub-processors:** Anthropic (AI processing), Google (email/calendar), SendGrid (system notifications), Sentry (error monitoring — configured not to capture email bodies), hosting provider (VPS).

## 3. Consent clause for the pilot agreement

> The Client acknowledges and consents that inbound emails received at the connected mailbox, which may contain personal data of the Client's customers, will be processed by AccountFlow Pte. Ltd. for the purpose of automated classification, reply drafting, appointment booking, and task creation. Processing includes transmission to Anthropic PBC's Claude API as a data intermediary. The Client remains the organisation responsible for the personal data under the PDPA and confirms it has the right to process customer emails for business communication purposes. AccountFlow will not use the Client's customer data for any purpose other than providing the service, will not use it to train AI models, and will delete it in accordance with the retention policy in Schedule B.

## 4. Retention policy (Schedule B)

- Email content and AI drafts: retained 12 months from receipt, then hard-deleted.
- On contract termination: all tenant data hard-deleted within 30 days, deletion confirmed in writing.
- On individual request (customer of the clinic exercising PDPA rights via the clinic): relevant records deleted within 30 days.
- Encrypted OAuth tokens: revoked and deleted immediately on termination or disconnect.
- Backups: deleted data ages out of backups within 35 days.

*Engineering follow-up: implement the 12-month purge job before the first renewal (currently manual).*

## 5. DPA checklist

- [ ] Anthropic: accept the Commercial Terms / Data Processing Addendum (zero-retention option if available on the current plan); record the date and version.
- [ ] Google: Workspace / API Services User Data Policy compliance — verify the OAuth app's scopes are the minimum required and complete verification if user count triggers it.
- [ ] SendGrid (Twilio) DPA: accept and file.
- [ ] Hosting provider: confirm data-center region and file their DPA. Note in customer materials where data physically resides.
- [ ] Appoint a Data Protection Officer (PDPA s.11(3) — mandatory; can be the founder) and publish contact details.
- [ ] Draft a breach-response note: PDPC notification within 3 calendar days of assessing a notifiable breach.

## 6. Honest-answer scripts for sales conversations

**"Is my patients' data safe?"** — "Emails are encrypted in transit and OAuth credentials encrypted at rest. Email content is processed by Anthropic's Claude under a data-processing agreement and is not used to train models. We keep data 12 months and delete on request. During the pilot phase, each customer runs with human-review-only mode on, so nothing is sent without your approval."

**"Do you comply with PDPA?"** — "We follow a documented PDPA baseline: consent clauses, a named DPO, retention limits, sub-processor DPAs, and breach procedures. A full external legal review is scheduled as we scale."

**Do not claim** "PDPA-certified" (no such certification exists) or "data never leaves Singapore" (Anthropic processing is not Singapore-resident) — both claims appear in older marketing drafts and must not be used.
