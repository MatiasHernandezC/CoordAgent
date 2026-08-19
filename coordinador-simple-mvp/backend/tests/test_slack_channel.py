"""Tests del canal Slack: verificacion de firma y flujo de eventos end-to-end.

Usa TestClient contra la app completa (igual que test_api_integration.py) con
LLM_PROVIDER=mock y un repositorio JSON temporal. Las llamadas reales a la Web
API de Slack (post_message/upload_file/resolve_*) se interceptan porque no hay
red disponible ni credenciales reales en CI.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import app.bff.slack_routes as slack_routes
import app.services.session_service as session_module
import app.services.slack_channel as slack_channel
from app.main import app
from app.services.llm_service import llm_service
from app.services.slack_channel import SlackApiError, upload_file, verify_slack_signature
from app.settings import settings
from app.storage.json_repository import JsonRepository

SIGNING_SECRET = "test-signing-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test-token")
    monkeypatch.setattr(settings, "slack_signing_secret", SIGNING_SECRET)
    monkeypatch.setattr(settings, "slack_trigger_word", "@coordina")
    # Sin esto, _extract_bot_user_id caeria a un auth.test real (red no
    # disponible en tests). Los tests que ejercitan la mencion real del bot
    # pasan "authorizations" en el payload, que tiene prioridad.
    monkeypatch.setattr(slack_routes, "resolve_bot_user_id", lambda: None)
    llm_service._cache.clear()
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return TestClient(app)


def _sign(body: bytes, timestamp: str) -> str:
    base = f"v0:{timestamp}:".encode("utf-8") + body
    return "v0=" + hmac.new(SIGNING_SECRET.encode("utf-8"), base, hashlib.sha256).hexdigest()


def _post_event(client, payload: dict, *, retry_num: str | None = None, bad_signature: bool = False):
    body = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = "v0=invalid" if bad_signature else _sign(body, timestamp)
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
        "Content-Type": "application/json",
    }
    if retry_num:
        headers["X-Slack-Retry-Num"] = retry_num
    return client.post("/api/channels/slack/events", content=body, headers=headers)


# --- Firma -------------------------------------------------------------------

def test_verify_slack_signature_accepts_valid(monkeypatch):
    monkeypatch.setattr(settings, "slack_signing_secret", SIGNING_SECRET)
    body = b'{"type":"event_callback"}'
    timestamp = str(int(time.time()))
    signature = _sign(body, timestamp)
    assert verify_slack_signature(timestamp=timestamp, body=body, signature=signature) is True


def test_verify_slack_signature_rejects_tampered_body(monkeypatch):
    monkeypatch.setattr(settings, "slack_signing_secret", SIGNING_SECRET)
    timestamp = str(int(time.time()))
    signature = _sign(b'{"type":"event_callback"}', timestamp)
    tampered = b'{"type":"other"}'
    assert verify_slack_signature(timestamp=timestamp, body=tampered, signature=signature) is False


def test_verify_slack_signature_rejects_old_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "slack_signing_secret", SIGNING_SECRET)
    monkeypatch.setattr(settings, "slack_request_max_age_seconds", 300)
    body = b'{"type":"event_callback"}'
    old_timestamp = str(int(time.time()) - 3600)
    signature = _sign(body, old_timestamp)
    assert verify_slack_signature(timestamp=old_timestamp, body=body, signature=signature) is False


# --- Endpoint ------------------------------------------------------------------

def test_slack_events_not_configured_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", "")
    response = _post_event(client, {"type": "event_callback", "event": {}})
    assert response.status_code == 503


def test_slack_events_url_verification_echoes_challenge(client):
    response = _post_event(client, {"type": "url_verification", "challenge": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}


def test_slack_events_rejects_invalid_signature(client):
    response = _post_event(
        client,
        {"type": "event_callback", "event": {}},
        bad_signature=True,
    )
    assert response.status_code == 401


def test_slack_events_retry_short_circuits_without_reprocessing(client, monkeypatch):
    calls = []
    monkeypatch.setattr(slack_routes, "_handle_event", lambda event: calls.append(event))
    response = _post_event(
        client,
        {"type": "event_callback", "event": {"type": "message", "text": "hola"}},
        retry_num="1",
    )
    assert response.status_code == 200
    assert calls == []


def test_slack_events_ignores_bot_messages(client, monkeypatch):
    calls = []
    monkeypatch.setattr(slack_routes, "post_message", lambda *a, **k: calls.append(("post", a, k)))
    response = _post_event(
        client,
        {
            "type": "event_callback",
            "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hola", "ts": "1.1", "bot_id": "B1"},
        },
    )
    assert response.status_code == 200
    assert calls == []


def test_slack_events_full_flow_creates_session_and_replies(client, monkeypatch):
    sent = []
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "resolve_channel_name", lambda channel_id: "#equipo")
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text: sent.append((channel, text)))
    monkeypatch.setattr(slack_routes, "upload_file", lambda *a, **k: sent.append(("upload", a, k)))

    response = _post_event(
        client,
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "text": "Yo puedo lunes en la tarde @coordina",
                "ts": "1700000000.000100",
                "channel_type": "channel",
            },
        },
    )

    assert response.status_code == 200
    assert sent, "se esperaba una respuesta enviada al canal de Slack"
    channel, text = sent[0]
    assert channel == "C123"
    assert "Coordina" in text

    sessions = client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["channel_config"]["group_jid"] == "slack:C123"
    assert sessions[0]["channel_config"]["group_name"] == "#equipo"


def test_slack_events_duplicate_ts_is_idempotent(client, monkeypatch):
    sent = []
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "resolve_channel_name", lambda channel_id: "#equipo")
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text: sent.append((channel, text)))
    monkeypatch.setattr(slack_routes, "upload_file", lambda *a, **k: sent.append(("upload", a, k)))

    event = {
        "type": "message",
        "channel": "C123",
        "user": "U123",
        "text": "Yo puedo lunes en la tarde @coordina",
        "ts": "1700000000.000100",
        "channel_type": "channel",
    }
    first = _post_event(client, {"type": "event_callback", "event": event})
    second = _post_event(client, {"type": "event_callback", "event": dict(event)})

    assert first.status_code == 200
    assert second.status_code == 200
    sessions = client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 1, "un ts duplicado no debe crear una segunda sesion ni reprocesar"


# --- Mencion real del bot (Slack autocompleta @coordina) --------------------

def test_normalize_bot_mention_replaces_plain_mention():
    assert (
        slack_routes._normalize_bot_mention("<@U0BOT123> organiza el horario", "U0BOT123")
        == "@coordina organiza el horario"
    )


def test_normalize_bot_mention_replaces_labeled_mention():
    assert (
        slack_routes._normalize_bot_mention("<@U0BOT123|coordina> organiza", "U0BOT123")
        == "@coordina organiza"
    )


def test_normalize_bot_mention_ignores_other_users():
    text = "<@U0OTRO456> puede el lunes"
    assert slack_routes._normalize_bot_mention(text, "U0BOT123") == text


def test_normalize_bot_mention_noop_without_bot_id():
    text = "<@U0BOT123> organiza"
    assert slack_routes._normalize_bot_mention(text, None) == text


def test_extract_bot_user_id_prefers_authorizations_field(monkeypatch):
    monkeypatch.setattr(
        slack_routes,
        "resolve_bot_user_id",
        lambda: (_ for _ in ()).throw(AssertionError("no deberia llamar auth.test si hay authorizations")),
    )
    payload = {
        "authorizations": [
            {"user_id": "UHUMAN", "is_bot": False},
            {"user_id": "UBOT123", "is_bot": True},
        ]
    }
    assert slack_routes._extract_bot_user_id(payload) == "UBOT123"


def test_extract_bot_user_id_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(slack_routes, "resolve_bot_user_id", lambda: "UBOT_FALLBACK")
    assert slack_routes._extract_bot_user_id({}) == "UBOT_FALLBACK"


def test_slack_events_invokes_on_real_bot_mention(client, monkeypatch):
    sent = []
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "resolve_channel_name", lambda channel_id: "#equipo")
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text: sent.append((channel, text)))
    monkeypatch.setattr(slack_routes, "upload_file", lambda *a, **k: sent.append(("upload", a, k)))

    client.post(
        "/api/sessions",
        json={"title": "no usado"},
    )
    # Ronda: disponibilidad primero, luego invocacion vía mencion real del bot.
    _post_event(
        client,
        {
            "type": "event_callback",
            "authorizations": [{"user_id": "UBOT123", "is_bot": True}],
            "event": {
                "type": "message",
                "channel": "C999",
                "user": "U123",
                "text": "Yo puedo lunes en la tarde",
                "ts": "1700000001.000100",
                "channel_type": "channel",
            },
        },
    )
    response = _post_event(
        client,
        {
            "type": "event_callback",
            "authorizations": [{"user_id": "UBOT123", "is_bot": True}],
            "event": {
                "type": "message",
                "channel": "C999",
                "user": "U123",
                "text": "<@UBOT123> organiza el horario",
                "ts": "1700000002.000100",
                "channel_type": "channel",
            },
        },
    )

    assert response.status_code == 200
    assert sent, "la mencion real del bot deberia disparar la invocacion, igual que el texto plano"
    channel, text = sent[0]
    assert channel == "C999"
    assert "Coordina" in text


# --- Subida de archivos (files.getUploadURLExternal, files.upload esta deprecado) --

class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self._payload


def test_upload_file_uses_three_step_external_flow(monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test-token")
    calls = []

    def fake_post(url, headers=None, data=None, json=None, files=None, timeout=None):
        calls.append({"url": url, "data": data, "json": json, "files": files})
        if url.endswith("/files.getUploadURLExternal"):
            return _FakeResponse({"ok": True, "upload_url": "https://upload.slack.com/xyz", "file_id": "F123"})
        if url == "https://upload.slack.com/xyz":
            return _FakeResponse(status_code=200)
        if url.endswith("/files.completeUploadExternal"):
            return _FakeResponse({"ok": True})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(slack_channel.requests, "post", fake_post)

    upload_file("C123", "evento.ics", b"BEGIN:VCALENDAR", initial_comment="hola")

    assert [c["url"] for c in calls] == [
        f"{slack_channel.SLACK_API_BASE}/files.getUploadURLExternal",
        "https://upload.slack.com/xyz",
        f"{slack_channel.SLACK_API_BASE}/files.completeUploadExternal",
    ]
    assert calls[0]["data"] == {"filename": "evento.ics", "length": len(b"BEGIN:VCALENDAR")}
    assert calls[2]["json"]["channel_id"] == "C123"
    assert calls[2]["json"]["files"] == [{"id": "F123", "title": "evento.ics"}]
    assert calls[2]["json"]["initial_comment"] == "hola"


def test_upload_file_raises_when_get_upload_url_fails(monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test-token")
    monkeypatch.setattr(
        slack_channel.requests,
        "post",
        lambda *a, **k: _FakeResponse({"ok": False, "error": "method_deprecated"}),
    )
    with pytest.raises(SlackApiError):
        upload_file("C123", "evento.ics", b"data")
