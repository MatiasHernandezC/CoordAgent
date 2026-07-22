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


def test_list_sessions_returns_recent_sessions(client):
    first_id = _create(client, "Grupo A")
    second_id = _create(client, "Grupo B")

    response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    ids = [session["id"] for session in sessions]
    assert ids[:2] == [second_id, first_id]


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


def test_cancel_decision_keeps_options_and_returns_to_calculated(client):
    session_id = _create(client)
    client.post(
        f"/api/sessions/{session_id}/message",
        json={"message": "Camila puede lunes desde las 16 y Diego puede lunes en la tarde"},
    )
    calculated = client.post(f"/api/sessions/{session_id}/calculate").json()["session"]
    confirmed = client.post(
        f"/api/sessions/{session_id}/confirm",
        json={"option_id": calculated["options"][0]["id"]},
    ).json()["session"]
    assert confirmed["status"] == "confirmed"

    response = client.post(f"/api/sessions/{session_id}/cancel-decision")
    session = response.json()["session"]

    assert response.status_code == 200
    assert session["status"] == "calculated"
    assert session["selected_option"] is None
    assert session["decision_summary"] is None
    assert len(session["options"]) >= 1


def test_archive_and_reopen_session(client):
    session_id = _create(client, "Sesion para archivar")

    archived = client.post(f"/api/sessions/{session_id}/archive").json()["session"]
    assert archived["archived_at"]
    assert archived["status"] == "draft"

    listed = client.get("/api/sessions").json()["sessions"][0]
    assert listed["archived_at"] == archived["archived_at"]

    reopened = client.post(f"/api/sessions/{session_id}/reopen").json()["session"]
    assert reopened["archived_at"] is None
    assert any("reabierta" in message["content"] for message in reopened["messages"])


def test_calendar_export_requires_confirmed_decision(client):
    session_id = _create(client)

    response = client.get(f"/api/sessions/{session_id}/calendar")

    assert response.status_code == 400
    assert "Confirma una opcion" in response.json()["detail"]


def test_calendar_export_returns_ics_for_confirmed_decision(client):
    session_id = _create(client, "Planificacion TAVI")
    client.post(
        f"/api/sessions/{session_id}/message",
        json={"message": "Camila puede lunes desde las 16 y Diego puede lunes en la tarde"},
    )
    calculated = client.post(f"/api/sessions/{session_id}/calculate").json()["session"]
    client.post(f"/api/sessions/{session_id}/confirm", json={"option_id": calculated["options"][0]["id"]})

    response = client.get(f"/api/sessions/{session_id}/calendar")
    body = response.json()

    assert response.status_code == 200
    assert body["filename"] == "planificacion-tavi-evento.ics"
    assert "BEGIN:VCALENDAR" in body["text"]
    assert "BEGIN:VEVENT" in body["text"]
    assert "SUMMARY:Planificacion TAVI" in body["text"]
    assert "DTSTART;TZID=America/Santiago:" in body["text"]
    assert "END:VCALENDAR" in body["text"]


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
    session = response.json()["session"]
    names = [p["name"] for p in session["participants"]]
    assert "Sofia" in names
    assert session["status"] == "calculated"
    assert session["options"]


def test_manual_availability_clears_stale_confirmed_decision(client):
    session_id = _create(client)
    client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Camila", "day": "lunes", "start": "10:00", "end": "12:00"},
    )
    calculated = client.post(f"/api/sessions/{session_id}/calculate").json()["session"]
    confirmed = client.post(
        f"/api/sessions/{session_id}/confirm",
        json={"option_id": calculated["options"][0]["id"]},
    ).json()["session"]
    assert confirmed["status"] == "confirmed"

    response = client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Luisa", "day": "lunes", "start": "10:00", "end": "11:00"},
    )

    session = response.json()["session"]
    assert response.status_code == 200
    assert session["status"] == "calculated"
    assert session["selected_option"] is None
    assert session["decision_summary"] is None
    assert any("pendiente de reconfirmacion" in message["content"] for message in session["messages"])


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
    assert "Mejor opcion" in (body["agent_reply"] or "")
    # Por defecto (reply_format=both) el backend adjunta la imagen del calendario.
    assert body["agent_reply_format"] == "both"
    assert isinstance(body["agent_reply_image"], str) and len(body["agent_reply_image"]) > 100
    names = [p["name"] for p in body["session"]["participants"]]
    assert "Yo" not in names
    assert body["session"]["channel_messages"][-1]["kind"] == "agent"


def test_channel_reply_text_only_when_requested(client):
    session_id = _create(client)
    payload = {
        "messages": [
            {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
            {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
            {"sender": "Nicolas", "text": "@coordina cerramos, solo texto por favor"},
        ]
    }
    response = client.post(f"/api/sessions/{session_id}/channel/batch", json=payload)
    body = response.json()

    assert body["invoked"] is True
    assert body["agent_reply_format"] == "text"
    assert body["agent_reply_image"] is None


def test_channel_confirm_command_confirms_option_without_llm_roundtrip(client):
    session_id = _create(client)
    first_payload = {
        "messages": [
            {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
            {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
            {"sender": "Nicolas", "text": "@coordina cerramos horario?"},
        ]
    }
    first = client.post(f"/api/sessions/{session_id}/channel/batch", json=first_payload).json()
    assert first["session"]["status"] == "calculated"

    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Nicolas", "text": "@coordina confirmar 1"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["llm_source"] == "channel_command"
    assert body["agent_reply_format"] == "text"
    assert "Decision confirmada" in (body["agent_reply"] or "")
    assert body["session"]["status"] == "confirmed"
    assert body["session"]["decision_history"][-1]["source"] == "whatsapp"
    assert body["session"]["decision_history"][-1]["confirmed_by"] == "Nicolas"


def test_channel_confirm_attaches_ics_document(client):
    import base64

    session_id = _create(client)
    client.post(
        f"/api/sessions/{session_id}/channel/batch",
        json={
            "messages": [
                {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
                {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
                {"sender": "Nicolas", "text": "@coordina"},
            ]
        },
    )

    body = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Camila", "text": "@coordina confirma"},
    ).json()

    assert body["session"]["status"] == "confirmed"
    assert body["agent_reply_document_name"], body
    assert body["agent_reply_document_name"].endswith(".ics")
    assert body["agent_reply_document_mimetype"] == "text/calendar"
    ics_text = base64.b64decode(body["agent_reply_document"]).decode("utf-8")
    assert ics_text.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" in ics_text


def test_channel_command_merges_pending_availability_first(client):
    # Regresion: la disponibilidad escrita justo antes de un comando se perdia,
    # porque la respuesta del comando reseteaba los pendientes sin extraerlos.
    session_id = _create(client)
    body = client.post(
        f"/api/sessions/{session_id}/channel/batch",
        json={
            "messages": [
                {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
                {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
                {"sender": "Nicolas", "text": "@coordina resumen"},
            ]
        },
    ).json()

    names = {participant["name"] for participant in body["session"]["participants"]}
    assert body["llm_source"] == "channel_command"
    assert {"Nicolas", "Camila"}.issubset(names)
    reply = body["agent_reply"] or ""
    assert "Lunes" in reply and "16:00-17:00" in reply


def test_channel_understands_unavailable_after_hour(client):
    # Caso reportado en el grupo real: "Elon no puede despues de las 4" debe
    # dejar a Elon disponible 09:00-16:00 ese dia, no sin disponibilidad.
    session_id = _create(client)
    body = client.post(
        f"/api/sessions/{session_id}/channel/batch",
        json={
            "messages": [
                {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
                {"sender": "Camila", "text": "Elon no puede despues de las 4 el lunes"},
                {"sender": "Nicolas", "text": "@coordina"},
            ]
        },
    ).json()

    elon = next(p for p in body["session"]["participants"] if p["name"] == "Elon")
    slots = [(s["day"], s["start"], s["end"]) for s in elon["availability"]]
    assert slots == [("lunes", "09:00", "16:00")]
    # Y no aparece como dato faltante.
    assert not any("Elon" in item for item in body["session"]["missing_info"])


def test_channel_help_command_lists_commands(client):
    session_id = _create(client)
    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Ana", "text": "@coordina ayuda"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["llm_source"] == "channel_command"
    reply = body["agent_reply"] or ""
    assert "confirma" in reply
    assert "resumen" in reply
    assert "cancela" in reply
    # La ayuda no debe inventar participantes ni tocar los datos.
    assert body["session"]["participants"] == []


def test_channel_cancel_command_reverts_confirmed_decision(client):
    session_id = _create(client)
    client.post(
        f"/api/sessions/{session_id}/channel/batch",
        json={
            "messages": [
                {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
                {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
                {"sender": "Nicolas", "text": "@coordina confirma"},
            ]
        },
    )

    body = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Nicolas", "text": "@coordina cancela"},
    ).json()

    assert body["llm_source"] == "channel_command"
    assert "Decision cancelada" in (body["agent_reply"] or "")
    assert body["session"]["status"] == "calculated"
    assert body["session"]["selected_option"] is None


def test_channel_cancel_without_decision_replies_gracefully(client):
    session_id = _create(client)
    body = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Ana", "text": "@coordina cancelar"},
    ).json()

    assert body["llm_source"] == "channel_command"
    assert "No hay una decision confirmada" in (body["agent_reply"] or "")
    assert body["session"]["status"] == "draft"


def test_channel_command_removes_participant_and_recalculates(client):
    session_id = _create(client)
    payload = {
        "messages": [
            {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
            {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
            {"sender": "Nicolas", "text": "@coordina"},
        ]
    }
    client.post(f"/api/sessions/{session_id}/channel/batch", json=payload)

    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Nicolas", "text": "@coordina quitar Camila"},
    )
    body = response.json()
    names = [participant["name"] for participant in body["session"]["participants"]]

    assert response.status_code == 200
    assert body["llm_source"] == "channel_command"
    assert "Camila" not in names
    assert "quite a camila" in (body["agent_reply"] or "").lower()


def test_session_export_endpoint_returns_text_report(client):
    session_id = _create(client)
    client.post(
        f"/api/sessions/{session_id}/message",
        json={"message": "Nicolas puede lunes en la tarde y Camila puede lunes desde las 16"},
    )
    client.post(f"/api/sessions/{session_id}/calculate")

    response = client.get(f"/api/sessions/{session_id}/export")
    body = response.json()

    assert response.status_code == 200
    assert body["filename"].endswith("-reporte.txt")
    assert "Disponibilidades por persona" in body["text"]
    assert "Nicolas" in body["text"]


def test_session_csv_export_returns_structured_rows(client):
    session_id = _create(client, "Equipo CSV")
    client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Ana", "day": "miercoles", "start": "10:00", "end": "12:00"},
    )

    response = client.get(f"/api/sessions/{session_id}/export.csv")
    body = response.json()

    assert response.status_code == 200
    assert body["filename"] == "equipo-csv-datos.csv"
    assert "tipo,grupo,estado,nombre,dia,inicio,fin,cobertura_pct,asisten,no_calzan" in body["text"]
    assert "disponibilidad,Equipo CSV,calculated,Ana,miercoles,10:00,12:00" in body["text"]
    assert "opcion_1,Equipo CSV,calculated," in body["text"]


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


def test_channel_config_accepts_group_metadata(client):
    session_id = _create(client, "Coordinacion WhatsApp")
    response = client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={
            "listening_enabled": True,
            "trigger_word": "@coordina",
            "group_jid": "120363000000000000@g.us",
            "group_name": "Equipo TAVI",
            "group_participant_count": 8,
        },
    )

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["title"] == "WhatsApp - Equipo TAVI"
    assert session["channel_config"]["group_jid"] == "120363000000000000@g.us"
    assert session["channel_config"]["group_name"] == "Equipo TAVI"
    assert session["channel_config"]["group_participant_count"] == 8


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


def test_extraction_exposes_last_processing_summary(client):
    session_id = _create(client)
    response = client.post(f"/api/sessions/{session_id}/message", json={"message": "Ana puede jueves a las 11"})
    processing = response.json()["session"]["last_processing"]

    assert processing["source"] == "mock"
    assert processing["confidence"] == "high"
    assert processing["participants_detected"] == 1


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
