#!/usr/bin/env bash
# Create your first AccountFlow account (a "tenant") — run after ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "ERROR: run ./setup.sh first."; exit 1; }
ADMIN_KEY="$(grep '^DASHBOARD_API_KEY=' .env | cut -d= -f2-)"
[ -n "$ADMIN_KEY" ] || { echo "ERROR: DASHBOARD_API_KEY missing from .env"; exit 1; }

echo "Let's create your business account."
read -r -p "Business name (e.g. Bright Smile Dental): " NAME
read -r -p "Owner email (this becomes your login): " EMAIL
while :; do
  read -r -s -p "Choose a password (at least 12 characters): " PW; echo
  [ "${#PW}" -ge 12 ] && break
  echo "Too short — needs at least 12 characters."
done

# JSON-escape backslash and double-quote so special characters in the
# name or password can't break (or inject into) the request body.
json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
BODY=$(printf '{"name":"%s","email":"%s","password":"%s","plan":"starter"}' \
  "$(json_escape "$NAME")" "$(json_escape "$EMAIL")" "$(json_escape "$PW")")
STATUS=$(curl -s -o /tmp/accountflow_resp.json -w "%{http_code}" \
  -X POST http://127.0.0.1:8000/api/tenants \
  -H "X-API-Key: ${ADMIN_KEY}" -H "Content-Type: application/json" \
  -d "$BODY")

if [ "$STATUS" = "201" ]; then
  echo
  echo "SUCCESS — account created!"
  echo "Login email: $EMAIL"
  echo
  echo "Next: open http://127.0.0.1:8000/api/docs"
  echo "  1. Find POST /api/auth/login → 'Try it out' → enter your email + password → Execute"
  echo "  2. Copy the access_token from the response"
  echo "  3. Click the green 'Authorize' button (top right) and paste the token"
  echo "  4. Run POST /api/auth/google/start → open the returned auth_url in your browser"
  echo "     and sign in with YOUR BUSINESS Gmail account. Done — polling starts automatically."
else
  echo
  echo "Something went wrong (HTTP $STATUS):"
  cat /tmp/accountflow_resp.json; echo
  echo "Tip: HTTP 409 means that email already has an account."
fi
rm -f /tmp/accountflow_resp.json
