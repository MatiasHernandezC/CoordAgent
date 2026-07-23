"""Regresiones del ciclo de vida de propuestas y confirmaciones.

Estas pruebas fijan los casos que causaron el fallo observado en Torneo: una
revision que cambia sin una propuesta nueva, comandos que muestran un R3 fijo,
y confirmaciones repetidas que duplican el historial.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.services.session_service as session_module
from app.main import app
from app.schemas import ChannelConfig, Participant, ProcessingSummary, Session
from app.services.llm_service import llm_service
from app.services.session_service import build_channel_reply, session_service
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    llm_service._cache.clear()
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return TestClient(app)


def _prepare_group(client: TestClient) -> tuple[str, dict]:
    created = client.post("/api/sessions", json={"title": "Grupo de regresion"})
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]

    configured = client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={
            "group_jid": "regression@g.us",
            "group_name": "Regresion",
            "group_participant_count": 2,
            "group_participant_ids": ["admin@wa", "member@wa"],
            "coordinator_ids": ["admin@wa"],
            "reply_format": "text",
        },
    )
    assert configured.status_code == 200

    for name in ("Admin", "Miembro"):
        added = client.post(f"/api/sessions/{session_id}/participants", json={"name": name})
        assert added.status_code == 200
        available = client.post(
            f"/api/sessions/{session_id}/availability",
            json={
                "participant_name": name,
                "day": "lunes",
                "start": "10:00",
                "end": "11:00",
            },
        )
        assert available.status_code == 200

    calculated = client.post(f"/api/sessions/{session_id}/calculate")
    assert calculated.status_code == 200
    session = calculated.json()["session"]
    assert session["options"]
    assert session["missing_info"] == []
    return session_id, session


def test_reordered_roster_does_not_change_revision_or_clear_decision(client):
    session_id, calculated = _prepare_group(client)
    revision = calculated["proposal_revision"]
    option_id = calculated["options"][0]["id"]

    confirmed_response = client.post(
        f"/api/sessions/{session_id}/confirm",
        json={"option_id": option_id, "expected_proposal_revision": revision},
    )
    assert confirmed_response.status_code == 200
    confirmed = confirmed_response.json()["session"]

    reordered_response = client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={
            "group_participant_count": 2,
            "group_participant_ids": ["member@wa", "admin@wa"],
            "coordinator_ids": ["admin@wa"],
        },
    )
    assert reordered_response.status_code == 200
    reordered = reordered_response.json()["session"]

    assert reordered["proposal_revision"] == confirmed["proposal_revision"]
    assert reordered["selected_option"]["id"] == option_id
    assert reordered["decision_summary"] == confirmed["decision_summary"]
    assert len(reordered["decision_history"]) == len(confirmed["decision_history"]) == 1


def test_reset_keeps_revision_monotonic_and_invalidates_old_token(client):
    session_id, calculated = _prepare_group(client)
    old_revision = calculated["proposal_revision"]
    old_option_id = calculated["options"][0]["id"]

    reset = session_service.reset_channel_context(session_id)

    assert reset.proposal_revision == old_revision + 1
    assert reset.options == []
    with pytest.raises(HTTPException) as error:
        session_service.confirm(
            session_id,
            old_option_id,
            expected_proposal_revision=old_revision,
        )
    assert error.value.status_code == 409
    assert "propuesta cambio" in str(error.value.detail).lower()


def test_repeated_confirmation_is_idempotent_and_does_not_duplicate_history(client):
    session_id, calculated = _prepare_group(client)
    payload = {
        "option_id": calculated["options"][0]["id"],
        "expected_proposal_revision": calculated["proposal_revision"],
    }

    first = client.post(f"/api/sessions/{session_id}/confirm", json=payload)
    second = client.post(f"/api/sessions/{session_id}/confirm", json=payload)

    assert first.status_code == second.status_code == 200
    first_session = first.json()["session"]
    second_session = second.json()["session"]
    assert second_session["selected_option"]["id"] == first_session["selected_option"]["id"]
    assert second_session["decision_summary"] == first_session["decision_summary"]
    assert len(first_session["decision_history"]) == 1
    assert len(second_session["decision_history"]) == 1


def test_whatsapp_confirmation_reports_fallback_blocker_before_stale_revision(client):
    session_id, calculated = _prepare_group(client)
    stored = session_module.repository.get(session_id)
    assert stored is not None
    stored.last_processing = ProcessingSummary(
        source="channel_mock_fallback_gemini_404",
        confidence="low",
        confidence_label="Baja",
        detail="Se uso fallback por error del proveedor principal.",
        fallback_used=True,
    )
    session_module.repository.save(stored)
    stale_revision = max(0, calculated["proposal_revision"] - 1)

    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={
            "sender": "Admin",
            "sender_id": "admin@wa",
            "text": f"@coordina confirmar 1 R{stale_revision}",
        },
    )

    assert response.status_code == 200
    body = response.json()
    reply = (body["agent_reply"] or "").lower()
    assert "ultima interpretacion" in reply
    assert "llm principal" in reply
    assert "la propuesta cambio" not in reply
    assert body["session"]["selected_option"] is None


def test_whatsapp_confirmation_without_revision_uses_current_proposal(client):
    session_id, calculated = _prepare_group(client)
    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={
            "sender": "Admin",
            "sender_id": "admin@wa",
            "text": "@coordina confirmar 1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["selected_option"]["id"] == calculated["options"][0]["id"]
    assert "Decision confirmada" in (body["agent_reply"] or "")


def test_no_new_availability_processing_does_not_block_confirmation(client):
    session_id, calculated = _prepare_group(client)
    stored = session_module.repository.get(session_id)
    assert stored is not None
    stored.last_processing = ProcessingSummary(
        source="channel_no_new_availability",
        confidence="low",
        confidence_label="Baja",
        detail="No se detectaron datos nuevos de disponibilidad en los mensajes pendientes.",
    )
    session_module.repository.save(stored)

    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={
            "sender": "Admin",
            "sender_id": "admin@wa",
            "text": "@coordina confirmar",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["selected_option"]["id"] == calculated["options"][0]["id"]
    assert "ultima interpretacion" not in (body["agent_reply"] or "").lower()


def test_channel_reply_distinguishes_current_members_from_people_with_availability():
    session = Session(
        title="Padron distinto",
        participants=[Participant(name=name) for name in ("Ana", "Beto", "Carla", "Diego")],
        channel_config=ChannelConfig(
            group_jid="roster@g.us",
            group_name="Padron",
            group_participant_count=2,
        ),
    )

    reply = build_channel_reply(session)

    assert "2 integrante(s) actual(es) del grupo" in reply
    assert "4 persona(s) con disponibilidad" in reply
