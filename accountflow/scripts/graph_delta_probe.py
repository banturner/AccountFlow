#!/usr/bin/env python3
"""Find out why parking a Graph delta cursor returned no @odata.deltaLink.

graph_live_smoke.py failed its first poll with microsoft_delta_park_failed:
`GET /me/mailFolders/inbox/messages/delta?$deltatoken=latest&$select=...&$top=N`
answered 200 with no delta link. This probe sends that request and a few
variants, and prints only the SHAPE of each answer: status, which @odata links
came back, and how many items each page held. It prints no subjects, senders,
links or tokens, so its output is safe to paste.

READ ONLY. It never sends mail and never writes to a database. It does write
the rotated refresh token back to the token file, exactly as
graph_live_smoke.py does, so the next run still has a usable token.

    python scripts/graph_delta_probe.py
"""
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import msal

GRAPH = "https://graph.microsoft.com/v1.0"
DELTA = f"{GRAPH}/me/mailFolders/inbox/messages/delta"
SELECT = "id,conversationId,internetMessageId,subject,from,receivedDateTime,body,bodyPreview"
SCOPES = ["https://graph.microsoft.com/Mail.Read"]
MAX_FOLLOW = 5
TOKEN_STORE = (
    Path(os.environ.get("USERPROFILE") or Path.home()) / ".accountflow-mstest-refresh.txt"
)


def _write_secret(target: Path, value: str) -> None:
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(str(tmp), str(target))


def _shape(resp: httpx.Response) -> tuple[str, dict]:
    """Status, item count and which links came back. Never the links themselves."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}, body is not JSON", {}
    if resp.status_code >= 400:
        err = body.get("error", {})
        return f"HTTP {resp.status_code} {err.get('code')}: {err.get('message')}", {}
    links = [k for k in ("@odata.nextLink", "@odata.deltaLink") if body.get(k)]
    return (
        f"HTTP {resp.status_code}, {len(body.get('value', []))} item(s), "
        f"links: {', '.join(links) or 'NONE'}",
        body,
    )


def probe(client: httpx.Client, headers: dict, label: str, params: dict, extra: dict | None = None) -> None:
    print(f"\n{label}")
    hdrs = {**headers, **(extra or {})}
    resp = client.get(DELTA, headers=hdrs, params=params)
    line, body = _shape(resp)
    print(f"   page 1: {line}")
    page = 1
    # If Graph pages the parking call, see whether a delta link turns up later.
    while body.get("@odata.nextLink") and not body.get("@odata.deltaLink") and page < MAX_FOLLOW:
        page += 1
        resp = client.get(body["@odata.nextLink"], headers=hdrs)
        line, body = _shape(resp)
        print(f"   page {page}: {line}")
    if body.get("@odata.nextLink") and not body.get("@odata.deltaLink"):
        print(f"   (stopped following after {MAX_FOLLOW} pages)")


def main() -> int:
    if not TOKEN_STORE.exists():
        print(f"No refresh token at {TOKEN_STORE}. Run oauth_test_microsoft.ps1 first.")
        return 1
    client_id = os.environ.get("MICROSOFT_CLIENT_ID") or input("Application (client) ID: ").strip()
    secret = os.environ.get("MICROSOFT_CLIENT_SECRET") or getpass.getpass("Client secret VALUE (hidden): ")

    app = msal.ConfidentialClientApplication(
        client_id,
        authority="https://login.microsoftonline.com/common",
        client_credential=secret,
    )
    result = app.acquire_token_by_refresh_token(
        TOKEN_STORE.read_text(encoding="utf-8-sig").strip(), scopes=SCOPES
    )
    if "access_token" not in result:
        print(f"Token refresh FAILED: {result.get('error')}: {result.get('error_description')}")
        return 1
    if result.get("refresh_token"):
        _write_secret(TOKEN_STORE, result["refresh_token"])
    print("Token refresh OK.")

    headers = {"Authorization": f"Bearer {result['access_token']}"}
    with httpx.Client(timeout=30.0) as client:
        probe(client, headers, "A. What outlook.py sends today: latest + $select + $top=25",
              {"$deltatoken": "latest", "$select": SELECT, "$top": 25})
        probe(client, headers, "B. latest + $select, no $top",
              {"$deltatoken": "latest", "$select": SELECT})
        probe(client, headers, "C. latest alone",
              {"$deltatoken": "latest"})
        probe(client, headers, "D. latest + $select, page size via Prefer header",
              {"$deltatoken": "latest", "$select": SELECT},
              {"Prefer": "odata.maxpagesize=25"})
        probe(client, headers, "E. no latest: a normal full delta round, $select, Prefer header",
              {"$select": SELECT}, {"Prefer": "odata.maxpagesize=25"})

        print("\nF. The first poll backlog read (unread mail, last 30 days)")
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = client.get(
            f"{GRAPH}/me/mailFolders/inbox/messages",
            headers={**headers, "Prefer": 'outlook.body-content-type="text"'},
            params={
                "$select": SELECT,
                "$top": 25,
                "$orderby": "receivedDateTime desc",
                "$filter": f"receivedDateTime ge {since} and isRead eq false",
            },
        )
        print(f"   {_shape(resp)[0]}")
        resp = client.get(
            f"{GRAPH}/me/mailFolders/inbox/messages",
            headers=headers,
            params={"$select": "id", "$top": 25, "$filter": f"receivedDateTime ge {since}"},
        )
        print(f"   same window, read or unread: {_shape(resp)[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
