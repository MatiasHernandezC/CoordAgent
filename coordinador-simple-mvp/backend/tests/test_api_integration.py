"""Tests de integracion de la API HTTP con FastAPI TestClient.

Ejercen la app completa (rutas -> servicios -> motor -> repositorio) en proceso,
sin necesidad de Postgres ni de un servidor corriendo. El repositorio se apunta a
un JSON temporal y el LLM se fija en 'mock' (extractor por reglas, determinista).
"""
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


def _create(client, title="Reunion integracion"):
    response = client.post("/api/sessions", json={"title": title})
    assert response.status_code == 200
    return response.json()["session"]["id"]


# --- Salud / runtime --------------------------------------------------------

def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_runtime_reports_mock_provider(client):
    response = client.get("/api/runtime")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["model"] == "rules"


# --- Flujo clasico completo -------------------------------------------------

def test_full_flow_create_extract_calculate_confirm(client):
    session_id = _create(client)

    message = (
        "Yo puedo lunes en la tarde, Camila puede lunes desde las 16 y "
        "Diego puede martes en la manana, Pedro puede a cualquier hora todos los dias"
    )
    response = client.post(f"/api/sessions/{session_id}/message", json={"message": message})
    assert response.status_code == 200
    body = response.json()
    assert body["llm_source"] == "mock"
    names = {p["name"] for p in body["session"]["participants"]}
    assert {"Yo", "Camila", "Diego", "Pedro"}.issubset(names)

    response = client.post(f"/api/sessions/{session_id}/calculate")
    assert response.status_code == 200
    session = response.json()["session"]
    assert session["status"] == "calculated"
    assert 1 <= len(session["options"]) <= 3
    assert len(session["availability_matrix"]) == 45
    best = session["options"][0]
    assert (best["day"], best["start"], best["end"]) == ("lunes", "16:00", "17:00")
    assert best["coverage_percent"] == 75

    response = client.post(f"/api/sessions/{session_id}/confirm", json={"option_id": best["id"]})
    assert response.status_code == 200
    confirmed = response.json()["session"]
    assert confirmed["status"] == "confirmed"
    assert "decision confirmada" in (confirmed["decision_summary"] or "")


def test_removal_leaves_participant_without_availability(client):
    session_id = _create(client)
    client.post(f"/api/sessions/{session_id}/message", json={"message": "Nicolas puede lunes en la tarde"})
    response = client.post(
        f"/api/sessions/{session_id}/message",
        json={"message": "Nicolas ya no puede lunes a ninguna hora"},
    )
    session = response.json()["session"]
    nicolas = next(p for p in session["participants"] if p["name"] == "Nicolas")
    assert nicolas["availability"] == []
    assert "Falta disponibilidad de Nicolas." in session["missing_info"]


def test_exclusive_availability_keeps_only_named_day(client):
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/message",
        json={"message": "Ana solo puede los miercoles"},
    )
    ana = next(p for p in response.json()["session"]["participants"] if p["name"] == "Ana")
    assert sorted({s["day"] for s in ana["availability"]}) == ["miercoles"]


def test_manual_availability_creates_participant_in_one_call(client):
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Sofia", "day": "jueves", "start": "10:00", "end": "12:00"},
    )
    assert response.status_code == 200
    names = [p["name"] for p in response.json()["session"]["participants"]]
    assert "Sofia" in names


# --- Validaciones y errores -------------------------------------------------

def test_empty_message_returns_422(client):
    session_id = _create(client)
    response = client.post(f"/api/sessions/{session_id}/message", json={"message": ""})
    assert response.status_code == 422


def test_invalid_day_returns_422(client):
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "X", "day": "sabado", "start": "10:00", "end": "12:00"},
    )
    assert response.status_code == 422


def test_start_not_before_end_returns_422(client):
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "X", "day": "lunes", "start": "18:00", "end": "09:00"},
    )
    assert response.status_code == 422


def test_unknown_session_returns_404(client):
    response = client.get("/api/sessions/no-existe")
    assert response.status_code == 404


def test_confirm_unknown_option_returns_400(client):
    session_id = _create(client)
    response = client.post(f"/api/sessions/{session_id}/confirm", json={"option_id": "fantasma"})
    assert response.status_code == 400


# --- Canal tipo WhatsApp ----------------------------------------------------

def test_channel_batch_with_trigger_invokes_agent(client):
    session_id = _create(client)
    payload = {
        "messages": [
            {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
            {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
            {"sender": "Nicolas", "text": "@coordina cerramos horario?"},
        ]
    }
    response = client.post(f"/api/sessions/{session_id}/channel/batch", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["invoked"] is True
    assert "Mejor opcion sugerida" in (body["agent_reply"] or "")
    names = [p["name"] for p in body["session"]["participants"]]
    assert "Yo" not in names
    assert body["session"]["channel_messages"][-1]["kind"] == "agent"


def test_channel_message_without_trigger_does_not_invoke(client):
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Ana", "text": "yo puedo lunes en la tarde"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["invoked"] is False
    assert not body.get("agent_reply")


# --- Cache ------------------------------------------------------------------

def test_repeated_message_is_served_from_cache(client):
    session_id = _create(client)
    first = client.post(f"/api/sessions/{session_id}/message", json={"message": "Rodrigo puede viernes en la tarde"})
    second = client.post(f"/api/sessions/{session_id}/message", json={"message": "Rodrigo puede viernes en la tarde"})
    assert first.json()["llm_source"] == "mock"
    assert second.json()["llm_source"] == "mock_cache"


# --- Hardening (P3) ---------------------------------------------------------

def test_new_session_exposes_schema_version(client):
    session_id = _create(client)
    response = client.get(f"/api/sessions/{session_id}")
    assert response.json()["session"]["schema_version"] == 1


def test_unexpected_error_returns_clean_500(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    session_module.repository = JsonRepository(tmp_path / "sessions.json")

    def boom(*args, **kwargs):
        raise RuntimeError("fallo interno simulado")

    monkeypatch.setattr(session_module.session_service, "get", boom)
    safe_client = TestClient(app, raise_server_exceptions=False)

    response = safe_client.get("/api/sessions/cualquiera")
    assert response.status_code == 500
    assert response.json() == {"detail": "Error interno del servidor."}
