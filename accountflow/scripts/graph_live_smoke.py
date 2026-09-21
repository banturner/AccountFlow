#!/usr/bin/env python3
"""Run AccountFlow's OWN Graph provider against a live Microsoft 365 mailbox.

OAUTH_DECISION_TESTS.md Test B proved the OAuth dance by hand in PowerShell.
It did not touch `app/services/outlook.py`. This script does: it redeems the
refresh token through the same MSAL call the worker uses, then drives
`outlook.fetch_new_messages` twice — once as a freshly connected mailbox
(no cursor) and once as the poll after that (delta cursor) — and reports what
the pipeline would actually have received.

READ ONLY. It never sends mail, never creates an event, never writes to the
database. There is no database: the integration is an in-memory stand-in and
the token refresh is exercised directly rather than through the row-locked
`get_access_token`, which needs a real session. Senders, subjects and bodies
are redacted on stdout unless you pass --show-pii: inbound clinic mail may
carry health data and this output tends to get pasted into tickets.

    python scripts/graph_live_smoke.py                 # uses the stored refresh token
    python scripts/graph_live_smoke.py --show-pii      # real senders/subjects/bodies

The refresh token is read from the file `oauth_test_microsoft.ps1` wrote. Both
that file and the client secret are live credentials for an app that can read
a mailbox: rotate the secret and delete the file once the test is done.
"""
import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

DEFAULT_TOKEN_STORE = (
    Path(os.environ.get("USERPROFILE") or Path.home()) / ".accountflow-mstest-refresh.txt"
)


def _refuse_on_a_real_deployment() -> None:
    """ENVIRONMENT=test seeded below outranks a .env file in pydantic-settings,
    which would disable app/config.py's placeholder-secret guard and shadow the
    real FERNET_KEY with a throwaway one. Nothing here writes to a database, so
    that stays in-process — but this is a laptop script and it should say so
    rather than rely on the reader noticing."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip().replace(" ", "").lower() == "environment=production":
                raise SystemExit(
                    f"Refusing to run: {candidate} is a production config.\n"
                    "This is a laptop test script. Run it against the trial tenant."
                )


def _seed_settings(client_id: str, client_secret: str, authority: str) -> None:
    """app.config refuses to build without the full production settings block,
    and importing outlook.py builds it. Seed throwaway values for everything
    this script cannot possibly use — ENVIRONMENT=test also disables the
    placeholder-secret guard, which is exactly what we want off the server."""
    from cryptography.fernet import Fernet

    defaults = {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "postgresql+asyncpg://unused:unused@localhost:5432/unused",
        "REDIS_URL": "redis://localhost:6379/0",
        "FERNET_KEY": Fernet.generate_key().decode(),
        "DASHBOARD_API_KEY": "unused",
        "JWT_SECRET_KEY": "unused",
        "ANTHROPIC_API_KEY": "unused",
        "GOOGLE_CLIENT_ID": "unused",
        "GOOGLE_CLIENT_SECRET": "unused",
        "GOOGLE_REDIRECT_URI": "https://example.com/api/auth/google/callback",
        "SENDGRID_API_KEY": "unused",
        "SENDGRID_FROM_EMAIL": "unused@example.com",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)

    os.environ["MICROSOFT_CLIENT_ID"] = client_id
    os.environ["MICROSOFT_CLIENT_SECRET"] = client_secret
    os.environ["MICROSOFT_AUTHORITY"] = authority


def _write_secret(target: Path, value: str) -> None:
    """Write owner-only and atomically.

    Owner-only because this is a live credential for an app that can read a
    mailbox, and the deploy box is shared. Atomically because `write_text`
    truncates first: a Ctrl-C in the wrong millisecond would leave an empty
    file, and that refresh token is the only thing standing between you and
    redoing the whole admin-consent dance.
    """
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


class _NullDb:
    """`fetch_new_messages` only ever calls flush() on its session."""

    async def flush(self):
        pass


def _redact_sender(address: str) -> str:
    """`j***@clinic.com.sg` — enough to see the parse worked, not enough to
    identify a patient if the terminal output is pasted somewhere."""
    if not address or "@" not in address:
        return "<none>" if not address else "<malformed>"
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def _describe(msg: dict, show_pii: bool) -> str:
    """A clinic subject line is routinely "Re: blood test results for Mr Tan".
    Inbound mail here may carry health data, and the provider logs already had
    subjects and recipient addresses stripped out of them deliberately, so this
    script does not undo that on stdout. --show-pii is the explicit override."""
    if show_pii:
        sender = f"{msg['sender_name']} <{msg['sender_email']}>"
        subject = msg["subject"]
        body = msg["body_text"]
    else:
        sender = _redact_sender(msg["sender_email"])
        subject = f"<{len(msg['subject'])} chars, hidden>"
        body = f"<{len(msg['body_text'])} chars, hidden>"
    return (
        f"  received {msg['received_at'].isoformat()}\n"
        f"  from     {sender}\n"
        f"  subject  {subject}\n"
        f"  thread   {msg['thread_id']}\n"
        f"  body     {body}\n"
    )


def _check(parsed: list[dict]) -> list[str]:
    """The pipeline assumes every one of these. A None here is a crash later."""
    problems = []
    for msg in parsed:
        for field in ("message_id", "thread_id", "subject", "received_at"):
            if not msg.get(field):
                problems.append(f"{field} missing on message {msg.get('message_id')!r}")
        if msg["received_at"].tzinfo is None:
            problems.append(f"received_at is naive on {msg.get('message_id')!r}")
    return problems


async def _run(args) -> int:
    from app.services import outlook

    token_store = Path(args.token_file)
    if not token_store.exists():
        print(f"No refresh token at {token_store}.")
        print("Run scripts/oauth_test_microsoft.ps1 first, or pass --token-file.")
        return 1
    refresh_token = token_store.read_text(encoding="utf-8-sig").strip()

    # ── 1. refresh, through the code the worker actually runs ──────────────
    print("1. Redeeming the refresh token via MSAL (the same call outlook.py makes)")
    result = await asyncio.to_thread(
        lambda: outlook._msal_app().acquire_token_by_refresh_token(
            refresh_token, scopes=outlook.SCOPES
        )
    )
    if "access_token" not in result:
        print(f"   FAILED: {result.get('error')} — {result.get('error_description')}")
        return 1
    print(f"   OK. Scopes granted: {result.get('scope')}")
    if result.get("refresh_token"):
        # Microsoft rotates on redemption; the worker persists the new one, so
        # keep the store usable for a re-run.
        _write_secret(token_store, result["refresh_token"])
        print(f"   Rotated refresh token written back to {token_store}")

    access_token = result["access_token"]

    async def _stub_token(db, integration):
        return access_token

    outlook.get_access_token = _stub_token

    # created_at drives outlook._horizon. Backdate it so a real mailbox has
    # something to return rather than an empty, uninformative result.
    integration = SimpleNamespace(
        id="live-smoke",
        sync_state=None,
        last_poll_error="(set before the run, should be cleared)",
        created_at=datetime.now(timezone.utc) - timedelta(days=args.connected_days_ago),
    )

    # ── 2. first poll ──────────────────────────────────────────────────────
    print("\n2. First poll — no cursor, so the backlog read plus $deltatoken=latest")
    first = await outlook.fetch_new_messages(_NullDb(), integration, max_results=args.max_results)
    print(f"   {len(first)} message(s); last_poll_error={integration.last_poll_error!r}")
    if not integration.sync_state:
        print("   FAILED: no delta cursor was parked. Every poll would re-read the backlog.")
        return 1
    print(f"   Cursor parked: {integration.sync_state[:80]}...")
    for msg in first[: args.show]:
        print(_describe(msg, args.show_pii))

    # ── 3. second poll ─────────────────────────────────────────────────────
    print("3. Second poll — follows the stored delta cursor")
    before = integration.sync_state
    second = await outlook.fetch_new_messages(_NullDb(), integration, max_results=args.max_results)
    print(f"   {len(second)} message(s); last_poll_error={integration.last_poll_error!r}")
    print(f"   Cursor after: {(integration.sync_state or '(none)')[:80]}")
    for msg in second[: args.show]:
        print(_describe(msg, args.show_pii))

    # ── 4. verdict ─────────────────────────────────────────────────────────
    # A quiet mailbox returning nothing twice is the MOST likely outcome, and
    # it proves nothing at all — no message was parsed, so the mapping the
    # pipeline depends on was never exercised. Treat it as inconclusive, not
    # as a pass, or this script becomes a green light that means nothing.
    problems = _check(first) + _check(second)
    if integration.last_poll_error:
        problems.append(f"the provider recorded: {integration.last_poll_error}")
    if not first and not second:
        problems.append(
            "no messages at all. Send the mailbox some mail, leave it UNREAD, "
            "and re-run — nothing was parsed, so nothing was proven."
        )
    if not integration.sync_state:
        problems.append(
            "the cursor is empty after the second poll, so the next poll would "
            "re-read the backlog instead of syncing incrementally."
        )
    elif "delta" not in integration.sync_state:
        problems.append(f"the cursor is not a delta URL: {integration.sync_state[:120]}")
    elif integration.sync_state == before and second:
        problems.append("the cursor did not move despite the poll returning mail.")

    print("\n" + "=" * 68)
    if problems:
        print("NOT PROVEN:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("PASS — outlook.py polled a live mailbox, parsed it, and advanced its cursor.")
    print("Proven here: the MSAL refresh call, the backlog query, delta parking,")
    print("cursor advance, and every field the pipeline reads.")
    print("NOT proven here: send_reply, create_appointment, check_availability,")
    print("and get_access_token itself — its row lock needs a real database, and")
    print("this script stubs it out after doing the refresh by hand.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=os.environ.get("MICROSOFT_CLIENT_ID", ""))
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_STORE))
    parser.add_argument(
        "--authority",
        default=os.environ.get("MICROSOFT_AUTHORITY", "https://login.microsoftonline.com/common"),
    )
    parser.add_argument("--max-results", type=int, default=25)
    parser.add_argument("--show", type=int, default=3, help="messages to print per poll")
    parser.add_argument(
        "--show-pii",
        action="store_true",
        help="print real senders, subjects and bodies (health data — off by default)",
    )
    parser.add_argument(
        "--connected-days-ago",
        type=int,
        default=30,
        help="how long ago to pretend the mailbox was connected (drives outlook._horizon)",
    )
    args = parser.parse_args()

    client_id = args.client_id or input("Application (client) ID: ").strip()
    # Prompted, never echoed, never stored. The Value column in Entra, not the
    # Secret ID — a GUID here is AADSTS7000215.
    client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET") or getpass.getpass(
        "Client secret VALUE (hidden): "
    )
    _refuse_on_a_real_deployment()
    _seed_settings(client_id, client_secret, args.authority)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
