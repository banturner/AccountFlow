# Two tests that decide the product's shape

Run both. Each is roughly a morning. Together they answer the only question
that can still invalidate the plan: **can we legally and practically read a
customer's inbox, without a three-month compliance project first?**

Do not build the pilot around either provider until one of these passes.

| | Google | Microsoft |
|---|---|---|
| Reading mail is | a **restricted** scope | a standard permission |
| Third-party security audit | **CASA — mandatory, annual** | none (certification is optional) |
| Unverified-app trap | tokens die after **7 days** | no equivalent |
| Escape hatch | app is **Internal** to one Workspace org | admin consent in the customer's tenant |

Both tests need a **business** tenant — a paid Google Workspace or Microsoft
365 account on a real domain. Neither works properly on a free `@gmail.com`
or `@outlook.com`, which is itself a finding: your customers must be clinics
that pay for business email.

---

## Test A — Google Workspace "Internal" app

**Question:** does an Internal app escape verification *and* the 7-day token
expiry when it uses a restricted scope?

Google's public docs do not state this clearly, which is exactly why it has
to be tested rather than assumed.

### Steps

1. Sign in to <https://console.cloud.google.com> with a **Google Workspace
   admin account on your own domain** (not a personal Gmail).
2. **Create a new project** — top bar → project picker → **New Project**.
   Name it `accountflow-internal-test`. Confirm it is created **inside your
   organisation**, not "No organisation". If the org picker is greyed out,
   this account is not a Workspace org account and the test cannot proceed.
3. **APIs & Services → Library** → enable **Gmail API**.
4. **APIs & Services → OAuth consent screen** (newer consoles: **Google Auth
   Platform → Audience**).
   - Choose **Internal**. *If "Internal" is greyed out, stop — the project is
     not owned by a Workspace organisation. That alone fails the test.*
   - Fill in app name and support email, save.
5. **Scopes** → add `https://www.googleapis.com/auth/gmail.readonly`.
   Save. **Note whether the console demands verification or a security
   assessment at this point.** Record exactly what it says.
6. **Credentials → Create Credentials → OAuth client ID → Web application**.
   Add redirect URI `http://127.0.0.1:8000/api/auth/google/callback`.
   Copy the client ID and secret.
7. Run AccountFlow locally (`setup.bat` / `./setup.sh`), put those two values
   in `.env`, and connect the mailbox through
   `POST /api/auth/google/start` as in `INSTALL_GUIDE.md` Step 5.

### What to record

- [ ] Was **Internal** selectable at all?
- [ ] Did adding the restricted scope trigger a verification or security
      assessment demand? Paste the wording.
- [ ] At consent, did you see an **"unverified app"** / "Google hasn't
      verified this app" warning, or did it consent cleanly?
- [ ] **Day 8:** does polling still work?

That last box is the whole test. Put a calendar reminder in for eight days'
time and check `GET /api/emails` still returns fresh threads. If the token
died, Internal did **not** escape the Testing-mode guillotine, and Google
means a full CASA project before any real pilot.

### Verdict

| Outcome | What it means |
|---|---|
| Internal available, no warning, alive on day 8 | ✅ Google pilots can run per-clinic with no audit. Still start CASA in parallel for self-serve later. |
| Works but dies on day 8 | ⚠️ Pilot only with weekly manual reconnects. Not sellable. |
| Internal unavailable, or verification demanded | ❌ Google is gated behind CASA. Go Microsoft-first. |

---

## Test B — Microsoft Entra app registration

**Question:** can a clinic's Microsoft 365 admin consent to mail access
without any audit, and does the connection survive a week?

### Steps

1. Sign in to <https://entra.microsoft.com> with a **Microsoft 365 work
   account** that is a tenant admin (a trial tenant is fine).
2. **Applications → App registrations → New registration**.
   - Name: `AccountFlow`
   - Supported account types: **Accounts in any organizational directory
     (multitenant)** — this is what lets other clinics consent later.
   - Redirect URI: **Web** → `http://localhost:8000/api/auth/microsoft/callback`
   - Register. Copy the **Application (client) ID** and **Directory (tenant) ID**.
3. **Certificates & secrets → New client secret**. Copy the **Value**
   immediately — it is never shown again.
4. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions**. Add:
   - `Mail.Read`
   - `Mail.Send`
   - `Calendars.ReadWrite`
   - `offline_access`
   - `User.Read`
5. Click **Grant admin consent for \<your tenant\>**. **Record whether this
   button is required, and whether anything mentions certification or an
   audit.** It should not.
6. Build the consent URL and open it in a browser (one line, replace
   `{client_id}`):

   ```
   https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id={client_id}&response_type=code&redirect_uri=http://localhost:8000/api/auth/microsoft/callback&response_mode=query&scope=offline_access%20https://graph.microsoft.com/Mail.Read%20https://graph.microsoft.com/Mail.Send%20https://graph.microsoft.com/Calendars.ReadWrite
   ```

   Sign in as the mailbox owner and consent. You will be redirected to
   `localhost` with `?code=...` in the URL — the page failing to load is
   fine, the code in the address bar is what matters.
7. Exchange the code for tokens (any HTTP client):

   ```bash
   curl -X POST https://login.microsoftonline.com/common/oauth2/v2.0/token \
     -d client_id=CLIENT_ID -d client_secret=CLIENT_SECRET \
     -d grant_type=authorization_code -d code=THE_CODE \
     -d redirect_uri=http://localhost:8000/api/auth/microsoft/callback
   ```

   Save the `refresh_token` from the response.
8. Confirm you can actually read mail:

   ```bash
   curl -H "Authorization: Bearer ACCESS_TOKEN" \
     "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?\$top=3&\$select=subject,from,receivedDateTime"
   ```

### What to record

- [ ] Was **admin consent** required, and was it one click?
- [ ] Any mention of certification, attestation or an audit? (Expected: none.)
- [ ] Did the consent screen show an **"unverified publisher"** warning?
      *(Cosmetic — fixed by free publisher verification — but note it, since
      a clinic owner will ask.)*
- [ ] Did the Graph call return real messages?
- [ ] **Day 8:** does the saved `refresh_token` still mint a new access token?

Re-run step 7 on day 8 with `grant_type=refresh_token` and
`refresh_token=THE_SAVED_TOKEN`. Microsoft has no 7-day guillotine, so this
should simply work — but the point of the test is to prove it, not trust it.

### Verdict

| Outcome | What it means |
|---|---|
| Admin consent one click, Graph reads mail, alive day 8 | ✅ Microsoft-first. No audit, no annual re-certification. |
| Works but blocked by tenant consent policy | ⚠️ Solvable — the clinic admin allowlists the app. Note the steps. |
| Certification demanded before mail access | ❌ Unexpected. Send me the wording. |

---

## Deciding

Whichever passes cleanly is where pilot #1 goes. If **both** pass, go
Microsoft-first anyway: no annual audit, no recurring fee, and your reseller
channel already sells Microsoft 365 to exactly these clinics.

If both fail, the product needs a different acquisition route (per-clinic
Cloud projects, or an IT-partner-installed deployment) — worth knowing in a
week rather than in month three.

Then update `AccountFlow_Master_Prompt.md`: it currently lists Outlook as
out-of-scope for Year 1, reasoned as "add after Gmail is proven." That
reasoning predates anyone costing the Google compliance wall, and these tests
may well invert it.
