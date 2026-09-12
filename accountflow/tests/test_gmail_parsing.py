"""Unit tests for Gmail message parsing (pure functions, no network)."""
import base64

import pytest

googleapiclient = pytest.importorskip("googleapiclient")

from app.services.gmail import _extract_text, parse_message  # noqa: E402


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def test_extract_plain_text_top_level():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("hello")}}
    assert _extract_text(payload) == "hello"


def test_extract_nested_multipart():
    """multipart/mixed > multipart/alternative > text/plain must be found."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64("<b>hi</b>")}},
                    {"mimeType": "text/plain", "body": {"data": _b64("nested body")}},
                ],
            }
        ],
    }
    assert _extract_text(payload) == "nested body"


def test_parse_message_fields():
    raw = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1751500000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Jane Tan <jane@example.com>"},
                {"name": "Subject", "value": "Appointment request"},
                {"name": "Message-ID", "value": "<abc123@mail.example.com>"},
                {"name": "References", "value": "<prev@mail.example.com>"},
            ],
            "body": {"data": _b64("Can I book Friday 3pm?")},
        },
    }
    parsed = parse_message(raw)
    assert parsed["sender_email"] == "jane@example.com"
    assert parsed["sender_name"] == "Jane Tan"
    assert parsed["subject"] == "Appointment request"
    assert parsed["body_text"] == "Can I book Friday 3pm?"
    assert parsed["thread_id"] == "t1"
    assert parsed["rfc_message_id"] == "<abc123@mail.example.com>"
    assert parsed["rfc_references"] == "<prev@mail.example.com>"
