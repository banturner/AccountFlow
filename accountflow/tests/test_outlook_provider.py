"""Microsoft Graph provider: pure parsing, plus the delta-sync state machine
with Graph replaced by an httpx MockTransport. No network, no database.

outlook.py has not yet been run against a live tenant; these tests pin the
behaviour the pipeline relies on so the live test cannot silently regress it.
"""
import uuid
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


class _FakeDb:
    async def flush(self):
        pass


def _integration(sync_state):
    return SimpleNamespace(
        id=uuid.uuid4(),
        sync_state=sync_state,
        last_poll_error="stale error from last time",
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
