"""Microsoft Graph provider: pure parsing, plus the delta-sync state machine
with Graph replaced by an httpx MockTransport. No network, no database.

outlook.py has not yet been run against a live tenant; these tests pin the
behaviour the pipeline relies on so the live test cannot silently regress it.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.services import outlook


def _graph_message(**overrides):
    msg = {
        "id": "AAMk-1",
        "conversationId": "AAQk-conv",
        "internetMessageId": "<abc@outlook.com>",
        "subject": "Appointment request",
        "from": {"emailAddress": {"name": "Jane Tan", "address": "jane@example.com"}},
        "receivedDateTime": "2026-08-06T05:00:00Z",
        "body": {"contentType": "text", "content": "Can I book Friday 3pm?"},
        "bodyPreview": "Can I book Friday 3pm?",
    }
    msg.update(overrides)
    return msg


# ── parse_message ───────────────────────────────────────────────────────────

def test_parse_message_maps_pipeline_fields():
    parsed = outlook.parse_message(_graph_message())
    assert parsed["message_id"] == "AAMk-1"
    assert parsed["thread_id"] == "AAQk-conv"
    assert parsed["sender_email"] == "jane@example.com"
    assert parsed["sender_name"] == "Jane Tan"
    assert parsed["subject"] == "Appointment request"
    assert parsed["body_text"] == "Can I book Friday 3pm?"
    assert parsed["rfc_message_id"] == "<abc@outlook.com>"
    assert parsed["rfc_references"] is None
    assert parsed["received_at"].utcoffset().total_seconds() == 0


def test_parse_message_falls_back_to_preview_and_defaults():
    parsed = outlook.parse_message(_graph_message(body=None, subject=None, **{"from": None}))
    assert parsed["body_text"] == "Can I book Friday 3pm?"
    assert parsed["subject"] == "(no subject)"
    assert parsed["sender_email"] == ""
    assert parsed["sender_name"] == ""


def test_parse_message_truncates_body_to_4000():
    parsed = outlook.parse_message(_graph_message(body={"content": "x" * 10_000}))
    assert len(parsed["body_text"]) == 4000


def test_parse_message_survives_bad_timestamp():
    parsed = outlook.parse_message(_graph_message(receivedDateTime="not-a-date"))
    assert parsed["received_at"].tzinfo is not None


# ── delta sync ──────────────────────────────────────────────────────────────

DELTA = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=old"
PARKED = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=parked"


class _FakeDb:
    async def flush(self):
        pass


def _integration(sync_state, created_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        sync_state=sync_state,
        last_poll_error="stale error from last time",
        created_at=created_at,
    )


def _install_graph(monkeypatch, handler):
    """Route every httpx.AsyncClient the module builds through `handler`, and
    stub the token refresh so no database is needed."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(outlook.httpx, "AsyncClient", _client)

    async def _token(db, integration):
        return "test-access-token"

    monkeypatch.setattr(outlook, "get_access_token", _token)


@pytest.mark.asyncio
async def test_expired_delta_token_resets_cursor_instead_of_dying(monkeypatch):
    """Graph answers 410 Gone when a delta token is too old. The poll must
    clear the cursor so the next cycle resyncs — the Gmail historyId-404
    equivalent. A tenant whose cursor never resets is a silently dead inbox."""
    def handler(request):
        return httpx.Response(410, json={"error": {"code": "SyncStateNotFound"}})

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert messages == []
    assert integration.sync_state is None
    assert integration.last_poll_error is None


@pytest.mark.asyncio
async def test_delta_page_yields_messages_and_persists_new_link(monkeypatch):
    new_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=new"

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-access-token"
        assert request.headers["Prefer"] == 'outlook.body-content-type="text"'
        return httpx.Response(
            200,
            json={
                "value": [
                    _graph_message(),
                    {"id": "AAMk-gone", "@removed": {"reason": "deleted"}},
                ],
                "@odata.deltaLink": new_link,
            },
        )

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert [m["message_id"] for m in messages] == ["AAMk-1"]
    assert integration.sync_state == new_link
    assert integration.last_poll_error is None


@pytest.mark.asyncio
async def test_graph_http_error_is_recorded_not_raised(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="Service Unavailable")

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert messages == []
    assert integration.sync_state == DELTA
    assert integration.last_poll_error == "Graph error 503"


# ── first poll ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_poll_filter_leads_with_the_sorted_property(monkeypatch):
    """Graph answers 400 ErrorInefficientFilter — "the restriction or sort
    order is too complex" — unless every property in $orderby also appears in
    $filter, ahead of the properties that are not sorted on. `isRead eq false`
    with `$orderby=receivedDateTime desc` broke every first poll."""
    seen = {}

    def handler(request):
        if "/delta" in request.url.path:
            return httpx.Response(200, json={"@odata.deltaLink": PARKED})
        seen["filter"] = request.url.params.get("$filter")
        seen["orderby"] = request.url.params.get("$orderby")
        return httpx.Response(200, json={"value": [_graph_message()]})

    _install_graph(monkeypatch, handler)
    integration = _integration(None)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert [m["message_id"] for m in messages] == ["AAMk-1"]
    assert seen["orderby"] == "receivedDateTime desc"
    # receivedDateTime is sorted on, so it must be the first filter clause.
    assert seen["filter"].startswith("receivedDateTime ge ")
    assert seen["filter"].endswith(" and isRead eq false")
    assert integration.sync_state == PARKED


@pytest.mark.asyncio
async def test_first_poll_parks_cursor_before_reading_the_backlog(monkeypatch):
    """The delta link must be taken BEFORE the backlog read. Mail arriving in
    between is then delivered twice (dedup drops it) rather than never."""
    order = []

    def handler(request):
        if "/delta" in request.url.path:
            order.append("park")
            return httpx.Response(200, json={"@odata.deltaLink": PARKED})
        order.append("read")
        return httpx.Response(200, json={"value": []})

    _install_graph(monkeypatch, handler)
    await outlook.fetch_new_messages(_FakeDb(), _integration(None))

    assert order == ["park", "read"]


@pytest.mark.asyncio
async def test_failed_backlog_read_leaves_the_cursor_unparked(monkeypatch):
    """Parking the cursor past mail we never handed to the pipeline would lose
    that mail permanently. A failed read must resync from scratch instead."""
    def handler(request):
        if "/delta" in request.url.path:
            return httpx.Response(200, json={"@odata.deltaLink": PARKED})
        return httpx.Response(503, text="Service Unavailable")

    _install_graph(monkeypatch, handler)
    integration = _integration(None)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert messages == []
    assert integration.sync_state is None
    assert integration.last_poll_error == "Graph error 503"


# ── delta: cursor always advances ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_delta_persists_next_link_when_it_stops_on_max_results(monkeypatch):
    """Stopping mid-round used to discard the nextLink and keep the old cursor,
    so every poll re-read the same page and mail behind it was never reached.
    A busy inbox would have gone permanently deaf."""
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=page2"

    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [_graph_message(id=f"AAMk-{n}") for n in range(3)],
                "@odata.nextLink": next_link,
            },
        )

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration, max_results=2)

    assert len(messages) >= 2
    assert integration.sync_state == next_link


@pytest.mark.asyncio
async def test_delta_pager_is_bounded_when_pages_yield_nothing(monkeypatch):
    """A round of pure read-state churn yields no messages, so the max_results
    guard never trips. Without a page cap one poll would walk forever."""
    calls = {"n": 0}
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=loop"

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"value": [], "@odata.nextLink": next_link})

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert messages == []
    assert calls["n"] == outlook._MAX_DELTA_PAGES
    assert integration.sync_state == next_link


# ── delta: updates to pre-connection mail ───────────────────────────────────

@pytest.mark.asyncio
async def test_delta_ignores_mail_older_than_the_connection(monkeypatch):
    """Graph republishes a message on ANY change, including isRead. A clinic
    clearing last month's unread backlog the day after connecting must not have
    AccountFlow draft replies to all of it."""
    connected = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [
                    _graph_message(id="old", receivedDateTime="2026-08-06T05:00:00Z"),
                    _graph_message(id="new", receivedDateTime="2026-09-15T05:00:00Z"),
                ],
                "@odata.deltaLink": PARKED,
            },
        )

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA, created_at=connected)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert [m["message_id"] for m in messages] == ["new"]
    assert integration.sync_state == PARKED


@pytest.mark.asyncio
async def test_naive_created_at_is_treated_as_utc(monkeypatch):
    """Postgres hands back tz-aware datetimes, but a naive one must not blow up
    the comparison against an aware receivedDateTime."""
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [_graph_message(receivedDateTime="2026-09-15T05:00:00Z")],
                "@odata.deltaLink": PARKED,
            },
        )

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA, created_at=datetime(2026, 9, 1))

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert [m["message_id"] for m in messages] == ["AAMk-1"]


# ── delta: the cursor can always be recovered ───────────────────────────────

@pytest.mark.asyncio
async def test_stale_skiptoken_400_resets_the_cursor_like_a_410(monkeypatch):
    """410 is the documented delta expiry, but a stale $skiptoken comes back
    400. Left to raise_for_status() that URL would be re-issued every poll
    forever with nothing able to clear it — worse than the stall it replaced."""
    def handler(request):
        return httpx.Response(400, json={"error": {"code": "ErrorInvalidSkipToken"}})

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration)

    assert messages == []
    assert integration.sync_state is None
    assert integration.last_poll_error is None


@pytest.mark.asyncio
async def test_rejected_cursor_mid_round_keeps_the_pages_already_read(monkeypatch):
    """Dropping them loses any the resync cannot reach: the first-poll branch
    only sees unread mail inside FIRST_POLL_LOOKBACK, so anything a
    receptionist has already opened would be gone silently."""
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=page2"
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={"value": [_graph_message()], "@odata.nextLink": next_link},
            )
        return httpx.Response(410, json={"error": {"code": "SyncStateNotFound"}})

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    messages = await outlook.fetch_new_messages(_FakeDb(), integration, max_results=50)

    assert [m["message_id"] for m in messages] == ["AAMk-1"]
    assert integration.sync_state is None


@pytest.mark.asyncio
async def test_page_with_no_links_is_recorded_as_a_stalled_cursor(monkeypatch):
    """Keeping the old cursor and clearing the error is the exact silent stall
    this branch was rewritten to remove."""
    def handler(request):
        return httpx.Response(200, json={"value": []})

    _install_graph(monkeypatch, handler)
    integration = _integration(DELTA)

    await outlook.fetch_new_messages(_FakeDb(), integration)

    assert integration.sync_state == DELTA
    assert "did not advance" in integration.last_poll_error


@pytest.mark.asyncio
async def test_max_results_is_enforced_within_a_page(monkeypatch):
    """The page-level check alone let one poll hold 20 full pages in memory.
    The worker has 512m on a box it shares with the kitchen bots."""
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [_graph_message(id=f"AAMk-{n}") for n in range(50)],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/x/delta?$skiptoken=p2",
            },
        )

    _install_graph(monkeypatch, handler)

    messages = await outlook.fetch_new_messages(_FakeDb(), _integration(DELTA), max_results=5)

    assert len(messages) == 5


# ── first poll: parking failures are visible ────────────────────────────────

@pytest.mark.asyncio
async def test_missing_delta_link_is_recorded_not_swallowed(monkeypatch):
    """Without a cursor the mailbox re-reads its backlog every 120s forever.
    Dedup hides it from the pipeline, so it looks perfectly healthy while new
    mail a staff member opens before the poll is never seen."""
    def handler(request):
        if "/delta" in request.url.path:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"value": []})

    _install_graph(monkeypatch, handler)
    integration = _integration(None)

    await outlook.fetch_new_messages(_FakeDb(), integration)

    assert integration.sync_state is None
    assert "not syncing incrementally" in integration.last_poll_error


@pytest.mark.asyncio
async def test_parked_cursor_carries_select_so_delta_pages_are_not_full_resources(monkeypatch):
    """Graph bakes query options into the token, so $select has to be set on
    the parking request or every later delta page drags back everything."""
    seen = {}

    def handler(request):
        if "/delta" in request.url.path:
            seen["select"] = request.url.params.get("$select")
            return httpx.Response(200, json={"@odata.deltaLink": PARKED})
        return httpx.Response(200, json={"value": []})

    _install_graph(monkeypatch, handler)
    await outlook.fetch_new_messages(_FakeDb(), _integration(None))

    assert seen["select"] is not None
    assert "body" in seen["select"]
    assert "conversationId" in seen["select"]

