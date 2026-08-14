"""Tests HTTP de los endpoints admin de Google Calendar y su enganche en confirm.

Usa TestClient contra la app completa, igual que test_api_integration.py.
"""

import base64
import os

import pytest
from fastapi.testclient import TestClient

import app.bff.routes as routes_module
import app.services.session_service as session_module
from app.main import app
from app.services.llm_service import llm_service
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "")
    llm_service._cache.clear()
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return TestClient(app)


def _create_and_calculate(client) -> tuple[str, str]:
    session_id = client.post("/api/sessions", json={"title": "Reunion"}).json()["session"]["id"]
    client.post(
        f"/api/sessions/{session_id}/message",
        json={"message": "Camila puede lunes de 9 a 10"},
    )
    session = client.post(f"/api/sessions/{session_id}/calculate").json()["session"]
    option_id = session["options"][0]["id"]
    return session_id, option_id


def test_status_reports_not_configured_by_default(client):
    response = client.get("/api/admin/google-calendar/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["connected"] is False


def test_auth_url_returns_503_when_not_configured(client):
    response = client.get("/api/admin/google-calendar/auth-url")
    assert response.status_code == 503


def test_confirm_without_connected_calendar_keeps_link_fallback(client):
    session_id, option_id = _create_and_calculate(client)
    response = client.post(f"/api/sessions/{session_id}/confirm", json={"option_id": option_id})
    assert response.status_code == 200
    calendar_event = response.json()["session"]["selected_calendar_event"]
    assert calendar_event["google_event_html_link"] is None


def test_confirm_attaches_real_google_event_when_connected(client, monkeypatch):
    session_id, option_id = _create_and_calculate(client)

    class FakeGoogleCalendarService:
        def is_connected(self):
            return True

        def create_event(self, session):
            return {"id": "evt-42", "htmlLink": "https://calendar.google.com/event?eid=evt-42"}

    monkeypatch.setattr(routes_module, "google_calendar_service", FakeGoogleCalendarService())

    response = client.post(f"/api/sessions/{session_id}/confirm", json={"option_id": option_id})
    assert response.status_code == 200
    calendar_event = response.json()["session"]["selected_calendar_event"]
    assert calendar_event["google_event_id"] == "evt-42"
    assert calendar_event["google_event_html_link"] == "https://calendar.google.com/event?eid=evt-42"


def test_confirm_survives_google_calendar_failure(client, monkeypatch):
    session_id, option_id = _create_and_calculate(client)

    class FailingGoogleCalendarService:
        def is_connected(self):
            return True

        def create_event(self, session):
            from app.services.google_calendar_service import GoogleCalendarApiError

            raise GoogleCalendarApiError("Google esta caido")

    monkeypatch.setattr(routes_module, "google_calendar_service", FailingGoogleCalendarService())

    response = client.post(f"/api/sessions/{session_id}/confirm", json={"option_id": option_id})
    assert response.status_code == 200
    calendar_event = response.json()["session"]["selected_calendar_event"]
    assert calendar_event["google_event_html_link"] is None


def test_whatsapp_confirm_reply_prefers_real_event_link(client, monkeypatch):
    session_id = client.post("/api/sessions", json={"title": "Reunion"}).json()["session"]["id"]
    client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={
            "group_jid": "wa-group-1",
            "group_name": "Equipo",
            "group_participant_count": 1,
            "group_participant_ids": ["camila@wa"],
            "coordinator_ids": ["camila@wa"],
            "reply_format": "text",
        },
    )
    client.post(f"/api/sessions/{session_id}/participants", json={"name": "Camila"})
    client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Camila", "day": "lunes", "start": "09:00", "end": "10:00"},
    )

    class FakeGoogleCalendarService:
        def is_connected(self):
            return True

        def create_event(self, session):
            return {"id": "evt-99", "htmlLink": "https://calendar.google.com/event?eid=evt-99"}

    monkeypatch.setattr(routes_module, "google_calendar_service", FakeGoogleCalendarService())

    confirm = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Camila", "sender_id": "camila@wa", "text": "@coordina confirmar"},
    ).json()

    assert "Evento creado en Google Calendar" in confirm["agent_reply"]
    assert "https://calendar.google.com/event?eid=evt-99" in confirm["agent_reply"]
