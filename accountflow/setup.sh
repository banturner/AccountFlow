#!/usr/bin/env bash
# AccountFlow one-command installer for Mac / Linux.
# Run it twice: first run creates your settings file, second run starts everything.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\n== %s ==\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ── 1. Docker present and running? ─────────────────────────────────────
command -v docker >/dev/null 2>&1 || fail "Docker is not installed.
Install Docker Desktop from https://www.docker.com/products/docker-desktop/ then run this again."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running.
Open the Docker Desktop app, wait until it says 'running', then run this again."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is missing (it comes with Docker Desktop)."

gen_hex()    { openssl rand -hex 24; }
gen_b64url() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }

# ── 2. First run: create .env with fresh secrets ───────────────────────
if [ ! -f .env ]; then
  say "Creating your settings file (.env) with fresh secure secrets"
  PG_PW="$(gen_hex)"; REDIS_PW="$(gen_hex)"; FERNET="$(gen_b64url)"
  ADMIN_KEY="$(gen_hex)"; JWT_KEY="$(gen_hex)$(gen_hex)"
  cat > .env <<ENVEOF
# ── Database (auto-generated, do not change) ──────────────
POSTGRES_DB=accountflow
POSTGRES_USER=accountflow
POSTGRES_PASSWORD=${PG_PW}
DATABASE_URL=postgresql+asyncpg://accountflow:${PG_PW}@db:5432/accountflow

# ── Redis (auto-generated, do not change) ─────────────────
REDIS_PASSWORD=${REDIS_PW}
REDIS_URL=redis://:${REDIS_PW}@redis:6379/0

# ── Security (auto-generated, do not change) ──────────────
FERNET_KEY=${FERNET}
DASHBOARD_API_KEY=${ADMIN_KEY}
JWT_SECRET_KEY=${JWT_KEY}
ACCESS_TOKEN_MINUTES=15
REFRESH_TOKEN_DAYS=30

# ── FILL THIS IN (required): your Anthropic API key ───────
# Get it at https://console.anthropic.com → API Keys
ANTHROPIC_API_KEY=PASTE_YOUR_ANTHROPIC_KEY_HERE
AI_CONFIDENCE_THRESHOLD=0.85

# ── FILL THIS IN (required for Gmail): Google OAuth ───────
# See Step 2 of INSTALL_GUIDE.md for click-by-click instructions
GOOGLE_CLIENT_ID=PASTE_YOUR_GOOGLE_CLIENT_ID_HERE
GOOGLE_CLIENT_SECRET=PASTE_YOUR_GOOGLE_CLIENT_SECRET_HERE
GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/api/auth/google/callback

# ── Optional (leave as is for now): SendGrid digests ──────
SENDGRID_API_KEY=optional_not_configured
SENDGRID_FROM_EMAIL=noreply@example.com
SENDGRID_FROM_NAME=AccountFlow

# ── Optional: error monitoring ────────────────────────────
SENTRY_DSN=

# ── App ───────────────────────────────────────────────────
ENVIRONMENT=development
GMAIL_POLL_INTERVAL=120
GMAIL_MAX_RESULTS=50
APP_BASE_URL=http://127.0.0.1:8000
ENVEOF
  echo
  echo "DONE. A settings file called .env was created with fresh secrets."
  echo
  echo "NEXT STEP: open the file named  .env  in any text editor and paste in:"
  echo "  1. Your Anthropic API key   (line: ANTHROPIC_API_KEY=...)"
  echo "  2. Your Google Client ID and Secret  (see INSTALL_GUIDE.md, Step 2)"
  echo "Save the file, then run  ./setup.sh  again to start AccountFlow."
  exit 0
fi

# ── 3. Second run: refuse to start with placeholder keys ───────────────
grep -q "PASTE_YOUR_ANTHROPIC_KEY_HERE" .env && fail "Your .env file still has the placeholder Anthropic key.
Open .env, paste your real key after ANTHROPIC_API_KEY=, save, and run ./setup.sh again."
grep -q "PASTE_YOUR_GOOGLE_CLIENT_ID_HERE" .env && echo "NOTE: Google keys not set yet — AccountFlow will start, but you can't connect a Gmail inbox until you add them (INSTALL_GUIDE.md Step 2)."

# ── 4. Build and start (nginx is skipped locally — it needs TLS certs) ─
say "Building and starting AccountFlow (first time takes 5–10 minutes)"
docker compose up -d --build db redis api worker beat

say "Waiting for the API to come up (up to 2 minutes)"
ok=""
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ -z "$ok" ]; then
  echo; echo "--- last API logs ---"; docker compose logs --tail 40 api
  fail "The API did not start. The logs above usually say why. Common cause: a typo in .env."
fi

say "Setting up the database"
docker compose exec -T api alembic upgrade head

say "Final health check"
health="$(curl -fsS http://127.0.0.1:8000/api/health)"
echo "$health"
echo "$health" | grep -Eq '"database": *"ok"' || fail "API is up but the database check failed. Check: docker compose logs db"

say "SUCCESS — AccountFlow is running"
cat <<'DONEEOF'

  • Create your first account:   ./create-account.sh
  • Explore the control panel:   http://127.0.0.1:8000/api/docs
  • Stop everything:             docker compose down
  • Start again later:           docker compose up -d db redis api worker beat

Full walkthrough: INSTALL_GUIDE.md
DONEEOF
