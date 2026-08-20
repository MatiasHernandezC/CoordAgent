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
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import app.bff.slack_routes as slack_routes
import app.services.session_service as session_module
import app.services.slack_channel as slack_channel
from app.main import app
from app.schemas import ParticipantRosterEntry, Session
from app.services.llm_service import llm_service
from app.services.slack_channel import SlackApiError, resolve_channel_roster, upload_file, verify_slack_signature
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


def _post_interaction(client, payload: dict, *, bad_signature: bool = False):
    # Slack firma el body form-encoded tal cual lo manda, no el JSON decodificado.
    body = urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = "v0=invalid" if bad_signature else _sign(body, timestamp)
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    return client.post("/api/channels/slack/interactions", content=body, headers=headers)


def _prepare_slack_group_with_options(client) -> tuple[str, dict]:
    """Sesion con options calculadas, identificada como grupo de Slack
    (group_jid con prefijo slack:) para que confirm() audite source='slack'."""
    created = client.post("/api/sessions", json={"title": "Grupo Slack"})
    session_id = created.json()["session"]["id"]
    configured = client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={"group_jid": "slack:C123", "group_name": "#equipo", "reply_format": "text"},
    )
    assert configured.status_code == 200
    for name in ("Ana", "Beto"):
        client.post(f"/api/sessions/{session_id}/participants", json={"name": name})
        client.post(
            f"/api/sessions/{session_id}/availability",
            json={"participant_name": name, "day": "lunes", "start": "10:00", "end": "11:00"},
        )
    calculated = client.post(f"/api/sessions/{session_id}/calculate")
    session = calculated.json()["session"]
    assert session["options"], "se esperaban opciones calculadas para el test"
    return session_id, session


def _block_action_payload(session_id: str, option_id: str, revision: int, *, channel="C123", user="U1"):
    return {
        "type": "block_actions",
        "channel": {"id": channel},
        "user": {"id": user},
        "response_url": "https://hooks.slack.test/actions/response",
        "actions": [
            {
                "action_id": f"confirm_option:{option_id}",
                "action_ts": "1700000003.000100",
                "value": json.dumps({"sid": session_id, "oid": option_id, "rev": revision}),
            }
        ],
    }


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
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text, blocks=None: sent.append((channel, text)))
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


def test_slack_events_hydrates_roster_without_naming_silent_members(client, monkeypatch):
    """El padron del canal (conversations.members) se sincroniza en cada
    mensaje: los miembros que todavia no escribieron no deben aparecer
    nombrados uno por uno en missing_info, y quien si escribe debe quedar con
    su nombre real (no el placeholder que le puso el padron)."""
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "resolve_channel_name", lambda channel_id: "#equipo")
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text, blocks=None: None)
    monkeypatch.setattr(slack_routes, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(
        slack_routes,
        "resolve_channel_roster",
        lambda channel_id, bot_user_id=None: {
            "participant_count": 2,
            "participant_ids": ["U123", "U456"],
            "coordinator_ids": [],
            "participant_roster": [
                ParticipantRosterEntry(id="U123", name="Camila"),
                ParticipantRosterEntry(id="U456", name="Nicolas"),
            ],
        },
    )

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

    session = client.get("/api/sessions").json()["sessions"][0]
    names = {p["name"] for p in session["participants"]}
    assert names == {"Camila", "Nicolas"}

    camila = next(p for p in session["participants"] if p["name"] == "Camila")
    assert camila["roster_only"] is False
    assert camila["availability"]

    nicolas = next(p for p in session["participants"] if p["name"] == "Nicolas")
    assert nicolas["roster_only"] is True
    assert nicolas["availability"] == []

    # Nicolas no escribio nada todavia: no debe salir nombrado en missing_info.
    assert not any("Nicolas" in item for item in session["missing_info"])


def test_slack_events_duplicate_ts_is_idempotent(client, monkeypatch):
    sent = []
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "resolve_channel_name", lambda channel_id: "#equipo")
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text, blocks=None: sent.append((channel, text)))
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
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text, blocks=None: sent.append((channel, text)))
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


def test_resolve_channel_roster_returns_participant_roster_entries(monkeypatch):
    """Bug real: devolvia dicts sueltos, y hydrate_participant_roster espera
    ParticipantRosterEntry (accede a .id/.name). Con red real disponible
    (produccion), esto reventaba silenciosamente cada mensaje de Slack apenas
    la sincronizacion del padron funcionaba (se ve en el intercept de abajo)."""
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test-token")
    monkeypatch.setattr(slack_channel, "resolve_display_name", lambda user_id: f"Nombre {user_id}")

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/conversations.members")
        return _FakeResponse(
            {"ok": True, "members": ["U123", "U456", "UBOT"], "response_metadata": {"next_cursor": ""}}
        )

    monkeypatch.setattr(slack_channel.requests, "get", fake_get)

    roster = resolve_channel_roster("C123", bot_user_id="UBOT")

    assert roster["participant_count"] == 2
    assert all(isinstance(entry, ParticipantRosterEntry) for entry in roster["participant_roster"])
    assert {entry.id for entry in roster["participant_roster"]} == {"U123", "U456"}


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


# --- Botones de Block Kit ------------------------------------------------------

def test_deliver_attaches_confirm_option_blocks(client, monkeypatch):
    session_id, session_dict = _prepare_slack_group_with_options(client)
    session = Session.model_validate(session_dict)

    sent = []
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text, blocks=None: sent.append((channel, text, blocks)))

    class _FakeResult:
        session = None
        agent_reply = "Coordina: elige una opcion"
        agent_reply_image = None
        agent_reply_document = None
        llm_source = "gemini"

    result = _FakeResult()
    result.session = session
    slack_routes._deliver("C123", result)

    assert len(sent) == 1
    _channel, _text, blocks = sent[0]
    assert blocks is not None
    values = [json.loads(el["value"]) for el in blocks[0]["elements"]]
    assert values[0] == {"sid": session_id, "oid": session_dict["options"][0]["id"], "rev": session_dict["proposal_revision"]}


def test_deliver_omits_blocks_for_command_replies(client, monkeypatch):
    """El bug real: '@coordina ayuda' (y reinicia/idempotente) volvia a
    mostrar los botones de confirmar como si fuera la propuesta, aunque
    hubiera una propuesta pendiente. Solo la propuesta real debe llevarlos."""
    session_id, session_dict = _prepare_slack_group_with_options(client)
    session = Session.model_validate(session_dict)

    sent = []
    monkeypatch.setattr(slack_routes, "post_message", lambda channel, text, blocks=None: sent.append((channel, text, blocks)))

    class _FakeResult:
        session = None
        agent_reply = "Coordina: esto es ayuda"
        agent_reply_image = None
        agent_reply_document = None
        llm_source = "channel_command"

    result = _FakeResult()
    result.session = session
    slack_routes._deliver("C123", result)

    assert len(sent) == 1
    _channel, _text, blocks = sent[0]
    assert blocks is None


def test_block_action_bad_signature_rejected(client):
    response = _post_interaction(client, {"type": "block_actions"}, bad_signature=True)
    assert response.status_code == 401


def test_block_action_confirms_option_and_replaces_message(client, monkeypatch):
    session_id, session = _prepare_slack_group_with_options(client)
    option_id = session["options"][0]["id"]
    revision = session["proposal_revision"]

    responded = []
    uploaded = []
    monkeypatch.setattr(slack_routes, "respond_to_url", lambda url, text, **k: responded.append((url, text)))
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "upload_file", lambda *a, **k: uploaded.append((a, k)))

    response = _post_interaction(client, _block_action_payload(session_id, option_id, revision))
    assert response.status_code == 200
    assert uploaded, "se esperaba que el .ics se subiera tras confirmar"
    assert len(responded) == 1
    url, text = responded[0]
    assert url == "https://hooks.slack.test/actions/response"
    assert "Coordina" in text

    confirmed = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert confirmed["status"] == "confirmed"
    assert confirmed["selected_option"]["id"] == option_id
    assert confirmed["decision_history"][-1]["source"] == "slack"


def test_block_action_rejects_stale_revision(client, monkeypatch):
    session_id, session = _prepare_slack_group_with_options(client)
    option_id = session["options"][0]["id"]
    stale_revision = session["proposal_revision"] - 1 or 999

    responded = []
    monkeypatch.setattr(slack_routes, "respond_to_url", lambda url, text, **k: responded.append((url, text)))
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")

    response = _post_interaction(client, _block_action_payload(session_id, option_id, stale_revision))
    assert response.status_code == 200
    assert len(responded) == 1
    assert "propuesta cambio" in responded[0][1]

    still_open = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert still_open["status"] != "confirmed"


def test_block_action_duplicate_click_is_idempotent(client, monkeypatch):
    session_id, session = _prepare_slack_group_with_options(client)
    option_id = session["options"][0]["id"]
    revision = session["proposal_revision"]

    responded = []
    monkeypatch.setattr(slack_routes, "respond_to_url", lambda url, text, **k: responded.append((url, text)))
    monkeypatch.setattr(slack_routes, "resolve_display_name", lambda user_id: "Camila")
    monkeypatch.setattr(slack_routes, "upload_file", lambda *a, **k: None)

    payload = _block_action_payload(session_id, option_id, revision)
    first = _post_interaction(client, payload)
    second = _post_interaction(client, payload)
    assert first.status_code == 200
    assert second.status_code == 200

    confirmed = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert len(confirmed["decision_history"]) == 1
