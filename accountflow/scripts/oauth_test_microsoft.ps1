<#
.SYNOPSIS
    Runs steps 7 and 8 of OAUTH_DECISION_TESTS.md Test B, and the day 8 re-check.

.DESCRIPTION
    Test B asks whether a Microsoft 365 tenant can consent to mail access with
    no audit, and whether that consent survives a week. This script does the
    fiddly parts: it pulls the authorisation code out of the callback URL,
    exchanges it for tokens, reads three inbox messages through Graph, and
    stores the refresh token so the day 8 run can prove it still works.

    The client secret is read from the console as a secure string and is never
    echoed, logged, or written to disk. The refresh token IS written to disk,
    because the whole point of the test is to still have it in eight days.
    Treat that file as a live credential and delete it when the test is done.

.PARAMETER Refresh
    Day 8 mode. Skips the consent code entirely and redeems the stored refresh
    token instead. If this succeeds, Microsoft has no 7 day expiry and Test B
    passes.

.EXAMPLE
    .\oauth_test_microsoft.ps1
    Prompts for client id, secret and the callback URL, then runs steps 7 and 8.

.EXAMPLE
    .\oauth_test_microsoft.ps1 -Refresh
    Day 8 re-check against the saved refresh token.
#>
[CmdletBinding()]
param(
    [switch]$Refresh,
    [string]$ClientId,
    [string]$RedirectUri = "http://localhost:8000/api/auth/microsoft/callback",
    [string]$Authority   = "https://login.microsoftonline.com/common",
    [string]$TokenStore  = (Join-Path $env:USERPROFILE ".accountflow-mstest-refresh.txt")
)

$ErrorActionPreference = "Stop"

function Read-PlainSecret([string]$Prompt) {
    # Keeps the secret off the screen and out of the PowerShell history buffer.
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    (New-Object System.Net.NetworkCredential("", $secure)).Password
}

function Invoke-TokenRequest([hashtable]$Body) {
    # Windows PowerShell throws away the response body on a 4xx, which is where
    # the AADSTS code lives. Without this you get a bare "400 Bad Request".
    try {
        return Invoke-RestMethod -Method Post -Uri "$Authority/oauth2/v2.0/token" -Body $Body
    } catch {
        # PowerShell 7 puts the body in ErrorDetails; 5.1 makes you read the stream.
        $detail = $_.ErrorDetails.Message
        if (-not $detail) {
            $response = $_.Exception.Response
            if ($null -eq $response -or -not $response.PSObject.Methods['GetResponseStream']) { throw }
            $detail = (New-Object System.IO.StreamReader($response.GetResponseStream())).ReadToEnd()
        }
        Write-Host ""
        Write-Host "Microsoft rejected the request:" -ForegroundColor Red
        Write-Host $detail
        Write-Host ""
        Write-Host "Common causes:" -ForegroundColor Yellow
        Write-Host "  AADSTS70008 / 54005  the code expired or was already used. Redo step 6."
        Write-Host "  AADSTS7000215        wrong client secret. You want the Value, not the Secret ID."
        Write-Host "  AADSTS50011          redirect_uri does not match the app registration exactly."
        Write-Host "  AADSTS65001 / 90094  admin consent was never granted for this tenant."
        exit 1
    }
}

if (-not $ClientId) {
    $ClientId = Read-Host -Prompt "Application (client) ID"
}
$ClientSecret = Read-PlainSecret "Client secret VALUE (hidden; not the Secret ID)"

$body = @{
    client_id     = $ClientId
    client_secret = $ClientSecret
    redirect_uri  = $RedirectUri
}

if ($Refresh) {
    if (-not (Test-Path $TokenStore)) {
        throw "No stored refresh token at $TokenStore. Run without -Refresh first."
    }
    $body.grant_type    = "refresh_token"
    $body.refresh_token = (Get-Content $TokenStore -Raw).Trim()
    $body.scope         = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Calendars.ReadWrite https://graph.microsoft.com/User.Read"
    Write-Host "Redeeming the stored refresh token..." -ForegroundColor Cyan
} else {
    Write-Host ""
    Write-Host "Paste the WHOLE callback URL from the browser address bar." -ForegroundColor Cyan
    Write-Host "The 'This site can't be reached' page is expected. The URL is what matters." -ForegroundColor DarkGray
    $callbackUrl = Read-Host -Prompt "Callback URL"

    if ($callbackUrl -notmatch "[?&]code=([^&#]+)") {
        throw "No code= parameter in that URL. Redo step 6 and copy the address bar in full."
    }
    # Stops at the first & so session_state and the trailing # are dropped.
    $body.code       = [uri]::UnescapeDataString($Matches[1])
    $body.grant_type = "authorization_code"
    Write-Host "Code found, $($body.code.Length) characters. Exchanging..." -ForegroundColor Cyan
}

$token = Invoke-TokenRequest $body

Write-Host ""
Write-Host "STEP 7 PASSED" -ForegroundColor Green
$token | Select-Object token_type, expires_in, scope | Format-List

if ($token.refresh_token) {
    $token.refresh_token | Set-Content -Path $TokenStore -NoNewline
    Write-Host "Refresh token saved to $TokenStore" -ForegroundColor Green
    Write-Host "That file is a live credential. Delete it once the test is done." -ForegroundColor Yellow
} else {
    Write-Host "No refresh token returned. offline_access was missing from the consent scopes." -ForegroundColor Red
}

Write-Host ""
Write-Host "STEP 8 — reading the inbox through Graph" -ForegroundColor Cyan
$graphUri = 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$top=3&$select=subject,from,receivedDateTime'
try {
    $messages = Invoke-RestMethod -Uri $graphUri -Headers @{ Authorization = "Bearer $($token.access_token)" }
} catch {
    Write-Host "Graph call failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not $messages.value -or $messages.value.Count -eq 0) {
    Write-Host "Graph answered, but the inbox is empty. Send yourself a mail and re-run." -ForegroundColor Yellow
    exit 0
}

$messages.value | ForEach-Object {
    [pscustomobject]@{
        Received = $_.receivedDateTime
        From     = $_.from.emailAddress.address
        Subject  = $_.subject
    }
} | Format-Table -AutoSize

Write-Host "STEP 8 PASSED — Graph returned real messages." -ForegroundColor Green
Write-Host ""
if ($Refresh) {
    Write-Host "Day 8 check passed. Microsoft has no 7 day guillotine. Test B is a full pass." -ForegroundColor Green
} else {
    $day8 = (Get-Date).AddDays(8).ToString("dddd d MMMM")
    Write-Host "Now set a reminder for $day8 and run:  .\oauth_test_microsoft.ps1 -Refresh" -ForegroundColor Cyan
    Write-Host "That last check is the whole test. Everything before it is setup." -ForegroundColor DarkGray
}
