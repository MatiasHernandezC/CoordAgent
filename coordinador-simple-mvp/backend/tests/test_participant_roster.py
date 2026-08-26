"""Regresiones del padron de canal (Slack y WhatsApp comparten la misma
logica en session_service.hydrate_participant_roster).

Un padron completo se puede sincronizar antes de que nadie escriba (ver
resolve_channel_roster en slack_channel.py, o group_participant_ids del
gateway de WhatsApp). Estos tests fijan dos cosas que se rompieron cuando esa
sincronizacion se agrego: no debe reventar con ids invalidos, y no debe
nombrar uno por uno a gente que todavia no dijo nada."""

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


def _resolve_group(client, **overrides):
    payload = {
        "group_jid": "roster@g.us",
        "group_name": "Roster",
        "group_participant_count": 2,
        "group_participant_ids": [],
        "coordinator_ids": [],
        "participant_roster": [],
        "trigger_word": "@coordina",
    }
    payload.update(overrides)
    response = client.post("/api/channel/groups/resolve", json=payload)
    assert response.status_code == 200
    return response.json()["session"]


def test_roster_hydration_does_not_nag_silent_members_by_name(client):
    session = _resolve_group(
        client,
        group_participant_count=2,
        participant_roster=[
            {"id": "ana@s.whatsapp.net", "name": "Ana"},
            {"id": "jefe@s.whatsapp.net", "name": "Jefe"},
        ],
    )

    assert len(session["participants"]) == 2
    # Nadie escribio todavia: se cuentan y se identifican por el padrón.
    assert any("2 integrante" in item and "Ana" in item and "Jefe" in item for item in session["missing_info"])
    assert any("2 integrante" in item for item in session["missing_info"])


def test_roster_hydration_skips_ids_below_minimum_length_instead_of_crashing(client):
    session = _resolve_group(
        client,
        group_jid="short-ids@g.us",
        group_participant_count=2,
        group_participant_ids=["a", "bb", "valid_id_ok"],
    )

    ids = {participant["external_id"] for participant in session["participants"]}
    assert "a" not in ids
    assert "bb" not in ids
    assert "valid_id_ok" in ids


def test_roster_placeholder_gets_real_name_once_the_person_writes(client):
    session_id = _resolve_group(
        client,
        group_jid="rename@g.us",
        group_participant_count=1,
        group_participant_ids=["59899999999@s.whatsapp.net"],
    )["id"]

    before = client.get(f"/api/sessions/{session_id}").json()["session"]
    placeholder = before["participants"][0]
    assert placeholder["name"] == "+59899999999"
    assert placeholder["roster_only"] is True

    client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={
            "sender": "Camila",
            "sender_id": "59899999999@s.whatsapp.net",
            "text": "yo puedo lunes en la tarde",
        },
    )
    # La extraccion recien corre cuando llega la palabra gatillo (se acumulan
    # mensajes pendientes hasta entonces, igual que en produccion).
    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={
            "sender": "Camila",
            "sender_id": "59899999999@s.whatsapp.net",
            "text": "@coordina",
        },
    )
    assert response.status_code == 200
    session = response.json()["session"]

    assert len(session["participants"]) == 1
    participant = session["participants"][0]
    assert participant["name"] == "Camila"
    assert participant["roster_only"] is False
    assert participant["availability"]


def test_required_roster_member_is_still_named_even_if_silent(client):
    session_id = _resolve_group(
        client,
        group_jid="required@g.us",
        group_participant_count=1,
        participant_roster=[{"id": "jefe@s.whatsapp.net", "name": "Jefe"}],
    )["id"]

    marked = client.post(
        f"/api/sessions/{session_id}/participants/requirements",
        json={"name": "Jefe", "required": True},
    )
    assert marked.status_code == 200
    session = marked.json()["session"]

    assert any("Jefe" in item and "requerido" in item for item in session["missing_info"])
