# AccountFlow — Install Guide (no coding required)

This guide gets AccountFlow running on your own computer so you can try it end to end. It's written for someone who has never used a command line. Take your time — you can't break anything.

Total time: about 30–45 minutes, most of it waiting for downloads.

**The short version:** install one free app (Docker Desktop), collect two keys (Anthropic + Google), then run `setup` twice.

---

## What you're installing

AccountFlow is several small programs that work together (a database, a web service, a background worker). Rather than install each one by hand, we use a free tool called **Docker** that runs them all in neat self-contained boxes. Our installer script does the rest: it creates your secret keys, starts everything, and checks that it works.

You'll run the installer **twice**:
1. **First run** — it creates a settings file and asks you to paste in two keys.
2. **Second run** — it starts AccountFlow and confirms everything is healthy.

**What you need:** a computer with ~8 GB memory and ~10 GB free disk space; an Anthropic account with API billing; and a Google account for the business Gmail you want automated.

---

## Step 1 — Install Docker Desktop (once)

1. Go to **https://www.docker.com/products/docker-desktop/**
2. Download the version for your computer (Windows or Mac).
3. Run the installer, accept the defaults, and restart if it asks.
4. Open the **Docker Desktop** app. Wait until the status says **"Running"**. Leave it open.

> On Windows it may ask to install "WSL 2" — say yes; it's part of Docker.

That's the only thing you install manually. Everything else is automatic.

---

## Step 2 — Get your two keys

### Key A — Anthropic API key (the AI brain; required)
1. Go to **https://console.anthropic.com** and sign in.
2. Add a small amount of credit if prompted (a few dollars is plenty for testing).
3. Open **API Keys** → **Create Key**. Copy the key (it starts with `sk-ant-`).

> The AI usage is billed through this Anthropic **API key**, which is separate from a Claude Pro chat subscription. Testing costs a few cents.

### Key B — Google keys (lets AccountFlow read/send Gmail)
You can start AccountFlow without these and add them later, but to connect a real Gmail you'll need them:
1. Go to **https://console.cloud.google.com** and sign in.
2. Top bar → **Select a project** → **New Project** → name it "AccountFlow" → **Create**.
3. Search **"Gmail API"** → open → **Enable**. Do the same for **"Google Calendar API"**.
4. **APIs & Services** → **OAuth consent screen** → **External** → fill in app name + your email → save through the steps. Under **Test users**, add the Gmail address you'll connect.
5. **Credentials** → **Create Credentials** → **OAuth client ID** → Application type **Web application**.
6. Under **Authorised redirect URIs** → **Add URI** → paste **exactly**:
   `http://127.0.0.1:8000/api/auth/google/callback`
7. Click **Create**. Copy the **Client ID** and **Client secret**.

---

## Step 3 — Run the installer (first time)

Open the **accountflow** folder.

**On Windows:** double-click **`setup.bat`**.
**On Mac:** open the **Terminal** app, type `cd ` (with a space), drag the accountflow folder onto the window, press Enter, then type `./setup.sh` and press Enter.

The installer checks Docker, then creates a settings file called **`.env`** with fresh secret keys and asks you to fill in your keys:
- **Windows:** Notepad opens the file automatically.
- **Mac:** open the `.env` file in the accountflow folder with TextEdit.

Find these lines and paste your keys after the `=` (no spaces, no quotes):
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
GOOGLE_CLIENT_ID=your-client-id-here
GOOGLE_CLIENT_SECRET=your-client-secret-here
```
**Save** the file and close the editor.

---

## Step 4 — Run the installer (second time)

Run the same file again (`setup.bat` / `./setup.sh`). This time it will:
- Build and start AccountFlow (**first build takes 5–10 minutes** — it's downloading the boxes).
- Wait for the service, set up the database, and do a final health check.

When you see **"SUCCESS — AccountFlow is running"**, you're live. If it stops with an error, it prints the reason and recent logs — usually a typo in a key. Fix `.env`, save, run again.

---

## Step 5 — Create your account and connect Gmail

1. Create your login:
   - **Windows:** double-click **`create-account.bat`**.
   - **Mac:** type `./create-account.sh`.
   It asks for your business name, email, and a password (12+ characters).

2. Open the built-in control panel: **http://127.0.0.1:8000/api/docs**
   This is a clickable page for every action — no coding.
   - **POST /api/auth/login** → **Try it out** → enter email + password → **Execute**. Copy the `access_token`.
   - Click the green **Authorize** button (top-right), paste the token, confirm.
   - **POST /api/auth/google/start** → **Execute** → copy the `auth_url`, paste it into a new browser tab, sign in with the **business Gmail** to automate.

Done. AccountFlow checks that inbox every 2 minutes. New emails become draft replies under **GET /api/drafts**.

> **Safety default:** auto-send is **OFF**. Every AI reply waits for your approval until you deliberately turn auto-send on. Nothing goes to your customers without your say-so.

---

## Everyday commands

| I want to… | Windows | Mac |
|---|---|---|
| Start AccountFlow | run `setup.bat` | `./setup.sh` |
| Stop AccountFlow | `docker compose down` | `docker compose down` |
| See what's happening | `docker compose logs -f api` | `docker compose logs -f api` |
| Create another account | `create-account.bat` | `./create-account.sh` |

(To run `docker compose` on Windows: in the accountflow folder, click the address bar, type `cmd`, press Enter, then type the command.)

---

## If something goes wrong

| Message | What to do |
|---|---|
| "Docker is not installed / not running" | Open Docker Desktop, wait for "Running", try again. |
| "still has the placeholder Anthropic key" | You didn't paste your real key into `.env`. Fix, save, rerun. |
| "The API did not start" | Read the logs it prints — usually a typo in `.env`. |
| Google "redirect URI mismatch" | The URI in Google (Step 2.6) must be **exactly** `http://127.0.0.1:8000/api/auth/google/callback`. |
| Port 8000 already in use | Quit the other program using it, or restart, and rerun. |

---

## Important notes

- This local setup is for **testing and demos**. It runs without HTTPS/TLS — fine on your own machine but **not** for real customer data. For production, deploy on a server (e.g. a Hostinger VPS) with the included `nginx` service, a real domain, and a certificate.
- Before putting any real clinic/patient email through it, complete `PDPA_Baseline_Pack.md`.
- Your secret keys live in the `.env` file. Never share it or email it to anyone.
