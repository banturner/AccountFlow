# Create your first AccountFlow account (run via create-account.bat, after setup)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".env")) { Write-Host "ERROR: run setup.bat first." -ForegroundColor Red; exit 1 }
$adminLine = Get-Content ".env" | Where-Object { $_ -match "^DASHBOARD_API_KEY=" } | Select-Object -First 1
$adminKey = $adminLine -replace "^DASHBOARD_API_KEY=", ""
if (-not $adminKey) { Write-Host "ERROR: DASHBOARD_API_KEY missing from .env" -ForegroundColor Red; exit 1 }

Write-Host "Let's create your business account." -ForegroundColor Cyan
$name  = Read-Host "Business name (e.g. Bright Smile Dental)"
$email = Read-Host "Owner email (this becomes your login)"
do {
  $pw = Read-Host "Choose a password (at least 12 characters)" -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
  if ($plain.Length -lt 12) { Write-Host "Too short - needs at least 12 characters." -ForegroundColor Yellow }
} while ($plain.Length -lt 12)

$body = @{ name = $name; email = $email; password = $plain; plan = "starter" } | ConvertTo-Json
try {
  $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tenants" -Method Post `
    -Headers @{ "X-API-Key" = $adminKey } -ContentType "application/json" -Body $body
  Write-Host ""
  Write-Host "SUCCESS - account created!" -ForegroundColor Green
  Write-Host "Login email: $email"
  Write-Host ""
  Write-Host "Next: open http://127.0.0.1:8000/api/docs"
  Write-Host "  1. Find POST /api/auth/login -> 'Try it out' -> enter your email + password -> Execute"
  Write-Host "  2. Copy the access_token from the response"
  Write-Host "  3. Click the green 'Authorize' button (top right) and paste the token"
  Write-Host "  4. Run POST /api/auth/google/start -> open the returned auth_url in your browser"
  Write-Host "     and sign in with YOUR BUSINESS Gmail account. Done - polling starts automatically."
} catch {
  Write-Host ""
  Write-Host "Something went wrong:" -ForegroundColor Red
  if ($_.Exception.Response) {
    $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    Write-Host $reader.ReadToEnd()
  } else { Write-Host $_.Exception.Message }
  Write-Host "Tip: 'already exists' means that email already has an account."
}
