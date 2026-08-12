"""Tests de integracion: endpoint de requerimientos, reply "sin habitual" y
comandos de WhatsApp para requeridos/prioridad gateados a coordinadores."""

import pytest
from fastapi.testclient import TestClient

import app.services.session_service as session_module
from app.main import app
from app.services.llm_service import llm_service
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    llm_service._cache.clear()
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return TestClient(app)


def _resolve_group(client, jid="g1@g.us", coordinators=("jefe@s.whatsapp.net",)):
    response = client.post(
        "/api/channel/groups/resolve",
        json={
            "group_jid": jid,
            "group_name": "Demo",
            "group_participant_count": 3,
            "group_participant_ids": [
                "ana@s.whatsapp.net",
                "jefe@s.whatsapp.net",
                "cata@s.whatsapp.net",
            ],
            "coordinator_ids": list(coordinators),
            "trigger_word": "@coordina",
        },
    )
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _channel(client, session_id, sender, sender_id, text, message_id, mentioned=None):
    payload = {
        "sender": sender,
        "sender_id": sender_id,
        "text": text,
        "message_id": message_id,
    }
    if mentioned:
        payload["mentioned_jids"] = mentioned
    return client.post(f"/api/sessions/{session_id}/channel/messages", json=payload)


def test_requirements_endpoint(client):
    session_id = _create_session(client)
    client.post(f"/api/sessions/{session_id}/participants", json={"name": "Ana"})
    response = client.post(
        f"/api/sessions/{session_id}/participants/requirements",
        json={"name": "Ana", "required": True, "priority": 3},
    )
    assert response.status_code == 200
    participant = response.json()["session"]["participants"][0]
    assert participant["required"] is True
    assert participant["priority"] == 3


def test_requirements_endpoint_partial_update(client):
    session_id = _create_session(client)
    client.post(f"/api/sessions/{session_id}/participants", json={"name": "Ana"})
    response = client.post(
        f"/api/sessions/{session_id}/participants/requirements",
        json={"name": "Ana", "priority": 5},
    )
    participant = response.json()["session"]["participants"][0]
    assert participant["required"] is False
    assert participant["priority"] == 5


def test_no_habitual_reply_when_no_history(client):
    session_id = _resolve_group(client)
    response = _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "@coordina a la hora de siempre", "n1")
    body = response.json()
    assert body["invoked"] is True
    assert body["llm_source"] == "channel_no_habitual_slot"
    assert "horario habitual" in body["agent_reply"]


def test_requirement_command_gated_to_coordinators(client):
    session_id = _resolve_group(client)
    _channel(client, session_id, "Ana", "ana@s.whatsapp.net", "yo puedo martes de 10 a 11", "c1")
    _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "yo puedo martes de 10 a 11", "c2")
    _channel(client, session_id, "Cata", "cata@s.whatsapp.net", "yo puedo martes de 10 a 11", "c3")
    _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "@coordina", "c4")

    # Coordinador puede marcar requerido.
    response = _channel(
        client,
        session_id,
        "Jefe",
        "jefe@s.whatsapp.net",
        "@coordina requerido @Ana",
        "c5",
        mentioned=["ana@s.whatsapp.net"],
    )
    assert "requerido" in response.json()["agent_reply"].lower()

    session = client.get(f"/api/sessions/{session_id}").json()["session"]
    ana = next(p for p in session["participants"] if p["name"] == "Ana")
    assert ana["required"] is True

    # Un no coordinador es rechazado.
    response = _channel(
        client,
        session_id,
        "Cata",
        "cata@s.whatsapp.net",
        "@coordina prioridad @Ana 3",
        "c6",
        mentioned=["ana@s.whatsapp.net"],
    )
    assert "administradoras" in response.json()["agent_reply"]
    session = client.get(f"/api/sessions/{session_id}").json()["session"]
    ana = next(p for p in session["participants"] if p["name"] == "Ana")
    assert ana["priority"] == 0


def test_habitual_slot_flow_with_confirmation(client):
    session_id = _resolve_group(client)
    _channel(client, session_id, "Ana", "ana@s.whatsapp.net", "yo puedo martes de 10 a 11", "h1")
    _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "yo puedo martes de 10 a 11", "h2")
    _channel(client, session_id, "Cata", "cata@s.whatsapp.net", "yo puedo martes de 10 a 11", "h3")
    _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "@coordina", "h4")
    _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "@coordina confirmar 1", "h5")

    session = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert session["habitual_slot"]["day"] == "martes"

    # "a la hora de siempre" se resuelve al slot habitual.
    _channel(client, session_id, "Ana", "ana@s.whatsapp.net", "a la hora de siempre no puedo", "h6")
    response = _channel(client, session_id, "Jefe", "jefe@s.whatsapp.net", "@coordina", "h7")
    assert response.json()["invoked"] is True
    session = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert session["last_processing"]["habitual_used"] is True
    ana = next(p for p in session["participants"] if p["name"] == "Ana")
    assert ana["availability"] == []


def _create_session(client):
    response = client.post("/api/sessions", json={"title": "Reunion"})
    return response.json()["session"]["id"]
