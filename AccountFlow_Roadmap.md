# AccountFlow.ai — Product Roadmap
**12-Month Plan · July 2026 → June 2027**

> **North star:** The AI inbox assistant every Singapore SMB can afford and trust — automates 80% of customer email without any IT setup.

---

## 📊 At a Glance

| Phase | Period | Theme | Key Milestone |
|---|---|---|---|
| 1 | Jul – Aug 2026 | **Harden & First Customer** | First paying customer live |
| 2 | Sep – Oct 2026 | **Dashboard & Self-serve** | Owner can manage without dev help |
| 3 | Nov – Dec 2026 | **Automation Depth** | Autonomous send + multi-inbox |
| 4 | Jan – Mar 2027 | **Scale Infrastructure** | 30+ customers, multi-tenant |
| 5 | Apr – Jun 2027 | **Expand & Fundraise** | New verticals + Series A prep |

---

## Phase 1 — Harden & First Customer
### Jul – Aug 2026

> **Goal:** Ship a production-stable v1 and sign 2–3 paying pilot customers.

### 🔴 Critical (must-ship)

- [ ] **OAuth token → PostgreSQL storage**
  Token.json on disk can corrupt on container restart. Move to a DB column with row-level lock on refresh.
  *Owner: Technical co-founder · Est: 3 days*

- [ ] **Email cap enforcement per plan**
  Add `EMAIL_MONTHLY_CAP` env var. Return graceful "plan limit reached" response beyond cap. Prevents runaway AI spend.
  *Est: 1 day*

- [ ] **Manual onboarding runbook**
  Step-by-step doc: create credentials.json, run OAuth flow, set .env, fire deploy.sh. Required until self-serve exists.
  *Est: 1 day*

- [ ] **Basic error alerting**
  Wire Sentry to send email/Slack on first occurrence of each new error type. Catch silent failures early.
  *Est: half day*

- [ ] **PDPA baseline (before first clinic customer)**
  Patient emails are personal data under PDPA. Minimum bar before go-live: consent language in the pilot agreement, data-processing summary for customers, DPA with Anthropic, defined retention/delete policy. Full legal review stays in Phase 4, but selling to medical clinics with zero paperwork is a liability.
  *Est: 2 days*

### 🟡 High priority

- [ ] **Confidence score tuning**
  Review first 100 processed emails. Calibrate `requires_owner_review` threshold. Target: <20% flagged for review.

- [ ] **Pilot customer #1 live**
  One dental/med-aesthetic clinic. Validate calendar booking + draft reply flows end-to-end.

- [ ] **Pricing page (simple static HTML)**
  Starter S$59 / Growth S$139 / Scale S$299 (matches Master Prompt pricing). Stripe Checkout link. No app login needed yet.

### 📈 Phase 1 Success Metrics
- 2+ paying pilots on the S$299 / 3-month pilot offer
- <5% of emails hitting error state
- Mean time to reply (AI-drafted): <2 min

---

## Phase 2 — Dashboard & Self-serve
### Sep – Oct 2026

> **Goal:** Owner can manage AccountFlow without messaging the developer. Reduce support burden to near-zero.

### 🔴 Critical

- [ ] **Web dashboard MVP**
  React SPA (or Next.js). Screens: pending drafts, open tasks, processed email log, stats. Auth via DASHBOARD_API_KEY.
  *Est: 2 weeks*

- [ ] **Draft approval UI**
  Replace manual API call with one-click Approve / Edit / Reject in the dashboard. Email body editable before send.
  *Est: 3 days*

- [ ] **Customer self-onboarding flow**
  Guided setup: connect Gmail (OAuth), set business profile, verify SendGrid sender domain. Reduces manual setup to <30 min.
  *Est: 1 week*

- [ ] **Stripe billing integration**
  Subscription create/cancel. Webhooks update customer status in DB. Pause processing if payment lapses.
  *Est: 3 days*

### 🟡 High priority

- [ ] **Replace Celery Beat → APScheduler**
  Beat is a separate process and single point of failure. Embed scheduler in the worker process.
  *Est: 1 day*

- [ ] **Mobile-responsive dashboard**
  Owners check email on phone. Dashboard must work on iOS Safari without zooming.

- [ ] **Weekly email digest**
  Automated summary: emails processed, drafts sent, tasks created. Sent every Monday 9am SGT.

### 📈 Phase 2 Success Metrics
- 10+ customers onboarded without dev involvement
- NPS from pilot customers: >50
- Dashboard DAU / MAU ratio: >40%

---

## Phase 3 — Automation Depth
### Nov – Dec 2026

> **Goal:** Increase the share of emails handled fully automatically. Less human review = more value delivered.

### 🔴 Critical

- [ ] **Autonomous send mode**
  Skip draft approval when `ai_confidence > 0.95` (configurable per customer). Already tracked in DB — just unlock the flag.
  *Est: 1 day*

- [ ] **Multi-inbox support**
  One customer, multiple Gmail addresses (e.g. appointments@ + enquiries@). Route to separate action sets per inbox.
  *Est: 1 week*

- [ ] **Claude Sonnet escalation**
  Auto-escalate to claude-sonnet-4-6 when Haiku confidence < 0.6. Log model used. Add cost tracking per escalation.
  *Est: 2 days*

### 🟡 High priority

- [ ] **Custom system prompt per customer**
  Each customer defines their own tone, services, FAQs. Stored in DB, editable in dashboard.
  *Est: 2 days*

- [ ] **Rejection reason capture**
  When owner rejects a draft, capture the reason. Feed into monthly AI tuning review.

- [ ] **Calendar conflict detection**
  Before creating an appointment, check Google Calendar for existing bookings. Suggest alternative slots.
  *Est: 3 days*

- [ ] **WhatsApp (optional add-on)**
  Integrate Twilio / WhatsApp Business API as a second channel. Same AI routing logic.
  *Est: 1 week*

### 📈 Phase 3 Success Metrics
- Autonomous send rate: >60% of drafted replies sent without human approval
- 20+ active customers
- MRR: S$2,100+

---

## Phase 4 — Scale Infrastructure
### Jan – Mar 2027

> **Goal:** Architecture supports 100+ customers without proportional ops overhead. Move from single-VPS to scalable infra.

### 🔴 Critical

- [ ] **Multi-tenant architecture**
  Tenants table. Each customer isolated by `tenant_id` FK on all tables. Row-level security on queries.
  *Est: 1 week*

- [ ] **Kubernetes / managed hosting migration**
  Move from single Hostinger VPS to DigitalOcean App Platform or Railway. Auto-scaling worker pods.
  *Est: 1 week*

- [ ] **Claude Batch API**
  Use Anthropic's async batch endpoint for non-urgent emails. ~50% cost reduction at volume.
  *Est: 3 days*

- [ ] **Managed PostgreSQL**
  Move from self-hosted Postgres to DigitalOcean Managed DB or Supabase. Automated backups, point-in-time recovery.
  *Est: 1 day*

### 🟡 High priority

- [ ] **Admin portal**
  Internal tool: view all tenants, usage stats, billing status, error rates. Required for ops at 50+ customers.

- [ ] **Usage-based billing tier**
  For high-volume customers: base fee + per-email charge above plan cap. Protects margin at scale.

- [ ] **SLA monitoring**
  Uptime dashboard. Alert if poll hasn't run in >10 min. Alert if error rate >5% over 1h window.

- [ ] **PDPA compliance review**
  Engage Singapore lawyer. Document data flows, retention policy, DPA with Anthropic. Required for enterprise deals.

### 📈 Phase 4 Success Metrics
- 50+ customers across 3+ verticals
- MRR: S$5,400+
- Infra cost as % of revenue: <3%
- P99 email processing latency: <5 min

---

## Phase 5 — Expand & Fundraise
### Apr – Jun 2027

> **Goal:** Prove the model beyond dental clinics. Build fundraising materials for Seed/Series A.

### 🔴 Critical

- [ ] **Vertical #2: Legal / accounting firms**
  New system prompt templates. New action: `create_client_matter`. Validate with 3 pilot firms.

- [ ] **Vertical #3: F&B / hospitality**
  Reservation management via calendar. New action: `manage_reservation`.

- [ ] **Partner / reseller program**
  Simple referral dashboard. Revenue share for business consultants who bring in SMB clients.

- [ ] **Investor deck + data room**
  Update architecture deck with live metrics. Build data room: MRR growth, churn, NPS, unit economics.

### 🟡 High priority

- [ ] **API for third-party integrations**
  Public REST API: push email to AccountFlow, get action result. Enables Zapier/Make integrations.

- [ ] **iOS / Android app**
  Draft approval on mobile. Push notification when a draft is waiting. React Native or PWA.

- [ ] **AI fine-tuning exploration**
  Evaluate fine-tuning Haiku on customer-approved drafts. Potential to increase autonomous send rate to >85%.

### 📈 Phase 5 Success Metrics
- 3 verticals with 5+ customers each
- MRR: S$9,000+ (path to S$110K ARR; S$13K+ if mix shifts up-market)
- Gross margin: >80%
- Seed/Series A term sheet in hand

---

## 🚫 Explicitly Not in Scope (Year 1)

| Item | Reason |
|---|---|
| Microsoft 365 / Outlook support | Gmail covers 80%+ of SG SMBs. Add later. |
| On-premise deployment | Too much ops overhead for Year 1. |
| Voice / phone calls | Different product. |
| Fine-tuning model | Not enough data yet. Revisit at 10K+ emails processed. |
| Building own LLM | Never. Use Anthropic API. |

---

## 🔑 Key Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Claude API pricing increase | Low | High | Batch API + model routing already planned |
| Gmail OAuth policy change | Medium | High | Abstract email reader; add IMAP fallback |
| Low SMB willingness to pay | Medium | High | Pilot pricing validation before full build |
| AI sends wrong reply | Low | High | Confidence threshold + human approval gate |
| Single-VPS outage | Medium | Medium | Phase 4 migration to managed infra |
| Competitor launches | Medium | Medium | Speed + Singapore-specific GTM moat |

---

## 💰 Revenue Targets

| Month | Customers (est.) | MRR (S$) | ARR Run Rate |
|---|---|---|---|
| Aug 2026 | 3 | S$320 | S$3,840 |
| Oct 2026 | 12 | S$1,290 | S$15,480 |
| Dec 2026 | 22 | S$2,360 | S$28,320 |
| Mar 2027 | 50 | S$5,360 | S$64,320 |
| Jun 2027 | 85 | S$9,100 | S$109,200 |

> Assumes ~60% Starter, 30% Growth, 10% Scale mix (blended ARPU ~S$107). Monthly churn <3%.
> Upside lever: shifting the mix up-market (e.g. 30/45/25) lifts blended ARPU to ~S$155 and Jun 2027 MRR to ~S$13K on the same customer count. Treat that as the stretch case, not the base case.

---

*Last updated: June 2026 · Owner: AccountFlow.ai Technical Co-Founder*
