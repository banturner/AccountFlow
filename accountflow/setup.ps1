# AccountFlow one-click installer for Windows (run via setup.bat).
# Run it twice: first run creates your settings file, second run starts everything.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Say($m)  { Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Fail($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

function New-RandomBytes([int]$n) {
  $b = New-Object byte[] $n
  $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::Create()
  $rng.GetBytes($b); $rng.Dispose(); return $b
}
function New-HexSecret([int]$n = 24)  { (New-RandomBytes $n | ForEach-Object { $_.ToString("x2") }) -join "" }
function New-B64UrlSecret([int]$n = 32) {
  [Convert]::ToBase64String((New-RandomBytes $n)).Replace('+','-').Replace('/','_')
}

# -- 1. Docker present and running? --------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Fail "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ then run setup.bat again."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Fail "Docker is installed but not running. Open the Docker Desktop app, wait until it says 'running', then run setup.bat again."
}

# -- 2. First run: create .env with fresh secrets ------------------------
if (-not (Test-Path ".env")) {
  Say "Creating your settings file (.env) with fresh secure secrets"
  $pg = New-HexSecret; $redis = New-HexSecret
  $fernet = New-B64UrlSecret; $admin = New-HexSecret
  $jwt = (New-HexSecret) + (New-HexSecret)
  $env_content = @"
# -- Database (auto-generated, do not change) --------------
POSTGRES_DB=accountflow
POSTGRES_USER=accountflow
POSTGRES_PASSWORD=$pg
DATABASE_URL=postgresql+asyncpg://accountflow:$pg@db:5432/accountflow

# -- Redis (auto-generated, do not change) -----------------
REDIS_PASSWORD=$redis
REDIS_URL=redis://:$redis@redis:6379/0

# -- Security (auto-generated, do not change) --------------
FERNET_KEY=$fernet
DASHBOARD_API_KEY=$admin
JWT_SECRET_KEY=$jwt
ACCESS_TOKEN_MINUTES=15
REFRESH_TOKEN_DAYS=30

# -- FILL THIS IN (required): your Anthropic API key -------
# Get it at https://console.anthropic.com -> API Keys
ANTHROPIC_API_KEY=PASTE_YOUR_ANTHROPIC_KEY_HERE
AI_CONFIDENCE_THRESHOLD=0.85

# -- FILL THIS IN (required for Gmail): Google OAuth -------
# See Step 2 of INSTALL_GUIDE.md for click-by-click instructions
GOOGLE_CLIENT_ID=PASTE_YOUR_GOOGLE_CLIENT_ID_HERE
GOOGLE_CLIENT_SECRET=PASTE_YOUR_GOOGLE_CLIENT_SECRET_HERE
GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/api/auth/google/callback

# -- Optional (leave as is for now): SendGrid digests ------
SENDGRID_API_KEY=optional_not_configured
SENDGRID_FROM_EMAIL=noreply@example.com
SENDGRID_FROM_NAME=AccountFlow

# -- Optional: error monitoring ----------------------------
SENTRY_DSN=

# -- App ---------------------------------------------------
ENVIRONMENT=development
GMAIL_POLL_INTERVAL=120
GMAIL_MAX_RESULTS=50
APP_BASE_URL=http://127.0.0.1:8000
"@
  # ASCII, no BOM — docker compose cannot read UTF-8-BOM env files
  [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ".env"), $env_content, [System.Text.ASCIIEncoding]::new())
  Write-Host ""
  Write-Host "DONE. A settings file called .env was created with fresh secrets." -ForegroundColor Green
  Write-Host ""
  Write-Host "NEXT STEP: Notepad will now open the settings file. Paste in:"
  Write-Host "  1. Your Anthropic API key   (line: ANTHROPIC_API_KEY=...)"
  Write-Host "  2. Your Google Client ID and Secret (see INSTALL_GUIDE.md, Step 2)"
  Write-Host "Save the file (Ctrl+S), close Notepad, then run setup.bat again."
  Start-Process notepad ".env"
  exit 0
}

# -- 3. Second run: refuse to start with placeholder keys ----------------
$envText = Get-Content ".env" -Raw
if ($envText -match "PASTE_YOUR_ANTHROPIC_KEY_HERE") {
  Fail "Your .env file still has the placeholder Anthropic key. Open .env in Notepad, paste your real key after ANTHROPIC_API_KEY=, save, and run setup.bat again."
}
if ($envText -match "PASTE_YOUR_GOOGLE_CLIENT_ID_HERE") {
  Write-Host "NOTE: Google keys not set yet - AccountFlow will start, but you can't connect a Gmail inbox until you add them (INSTALL_GUIDE.md Step 2)." -ForegroundColor Yellow
}

# -- 4. Build and start (nginx is skipped locally - it needs TLS certs) --
Say "Building and starting AccountFlow (first time takes 5-10 minutes)"
docker compose up -d --build db redis api worker beat
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed - see the messages above." }

Say "Waiting for the API to come up (up to 2 minutes)"
$ok = $false
foreach ($i in 1..60) {
  try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 3 | Out-Null
    $ok = $true; break
  } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) {
  Write-Host "`n--- last API logs ---"
  docker compose logs --tail 40 api
  Fail "The API did not start. The logs above usually say why. Common cause: a typo in .env."
}

Say "Setting up the database"
docker compose exec -T api alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "Database setup failed - see the messages above." }

Say "Final health check"
$health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health"
$health | ConvertTo-Json
if ($health.database -ne "ok") { Fail "API is up but the database check failed." }

Say "SUCCESS - AccountFlow is running"
Write-Host ""
Write-Host "  * Create your first account:   double-click create-account.bat"
Write-Host "  * Explore the control panel:   http://127.0.0.1:8000/api/docs"
Write-Host "  * Stop everything:             docker compose down"
Write-Host "  * Start again later:           run setup.bat again"
Write-Host ""
Write-Host "Full walkthrough: INSTALL_GUIDE.md"
