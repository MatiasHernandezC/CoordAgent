"""Tests del servicio de Google Calendar (OAuth a nivel administrador).

Todas las llamadas HTTP salientes (requests.post/get) se interceptan: no hay
red disponible en CI y tampoco credenciales reales de Google.
"""

import base64
import os

import pytest

import app.services.google_calendar_service as gcal_module
from app.schemas import Participant, Session, TimeOption
from app.services.google_calendar_service import (
    GoogleCalendarApiError,
    GoogleCalendarConfigError,
    GoogleCalendarService,
)
from app.services.session_service import session_service
from app.settings import settings
from app.storage.json_google_calendar_repository import JsonGoogleCalendarRepository


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture()
def master_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture()
def service(tmp_path, monkeypatch, master_key):
    monkeypatch.setattr(settings, "llm_keys_master_key", master_key)
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "https://coordina.example/api/admin/google-calendar/callback")
    repo = JsonGoogleCalendarRepository(tmp_path / "google_calendar_credential.json")
    monkeypatch.setattr(gcal_module, "repository", repo)
    return GoogleCalendarService()


def _confirmed_session() -> Session:
    session = Session(title="Reunion")
    session.participants = [Participant(name="Camila")]
    option = TimeOption(
        day="lunes",
        start="09:00",
        end="10:00",
        available_participants=["Camila"],
        score=1,
        coverage_percent=100,
    )
    session.options = [option]
    session.selected_option = option
    return session


# --- Configuracion -------------------------------------------------------------

def test_management_disabled_without_oauth_config(tmp_path, monkeypatch, master_key):
    monkeypatch.setattr(settings, "llm_keys_master_key", master_key)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "")
    service = GoogleCalendarService()
    assert service.management_enabled() is False
    with pytest.raises(GoogleCalendarConfigError):
        service.authorization_url()


def test_status_reports_not_connected_by_default(service):
    status = service.status()
    assert status["connected"] is False
    assert status["account_email"] is None


# --- Flujo OAuth -----------------------------------------------------------

def test_authorization_url_contains_client_and_state(service):
    url = service.authorization_url()
    assert "client_id=client-id" in url
    assert "state=" in url
    assert "access_type=offline" in url


def test_handle_callback_rejects_unknown_state(service):
    with pytest.raises(GoogleCalendarConfigError):
        service.handle_callback("some-code", "not-a-real-state")


def test_handle_callback_stores_encrypted_refresh_token(service, monkeypatch):
    url = service.authorization_url()
    state = url.split("state=")[1].split("&")[0]

    calls = []

    def fake_post(endpoint, data=None, timeout=None, **kwargs):
        calls.append((endpoint, data))
        return FakeResponse({"refresh_token": "rt-secret", "access_token": "at-123"})

    def fake_get(endpoint, headers=None, timeout=None, **kwargs):
        return FakeResponse({"email": "admin@example.com"})

    monkeypatch.setattr(gcal_module.requests, "post", fake_post)
    monkeypatch.setattr(gcal_module.requests, "get", fake_get)

    status = service.handle_callback("auth-code", state)

    assert status["connected"] is True
    assert status["account_email"] == "admin@example.com"

    record = gcal_module.repository.get()
    assert record["encrypted_refresh_token"] != "rt-secret"  # nunca en claro
    assert "rt-secret" not in str(record)


def test_handle_callback_state_is_single_use(service, monkeypatch):
    url = service.authorization_url()
    state = url.split("state=")[1].split("&")[0]
    monkeypatch.setattr(
        gcal_module.requests,
        "post",
        lambda *a, **k: FakeResponse({"refresh_token": "rt", "access_token": "at"}),
    )
    monkeypatch.setattr(gcal_module.requests, "get", lambda *a, **k: FakeResponse({"email": "a@b.com"}))

    service.handle_callback("code", state)
    with pytest.raises(GoogleCalendarConfigError):
        service.handle_callback("code", state)


def test_disconnect_clears_stored_credential(service, monkeypatch):
    url = service.authorization_url()
    state = url.split("state=")[1].split("&")[0]
    monkeypatch.setattr(
        gcal_module.requests,
        "post",
        lambda *a, **k: FakeResponse({"refresh_token": "rt", "access_token": "at"}),
    )
    monkeypatch.setattr(gcal_module.requests, "get", lambda *a, **k: FakeResponse({"email": "a@b.com"}))
    service.handle_callback("code", state)
    assert service.is_connected() is True

    service.disconnect()
    assert service.is_connected() is False


# --- Creacion de evento -----------------------------------------------------

def test_create_event_returns_none_when_not_connected(service):
    assert service.create_event(_confirmed_session()) is None


def test_create_event_posts_with_refreshed_access_token(service, monkeypatch):
    url = service.authorization_url()
    state = url.split("state=")[1].split("&")[0]
    monkeypatch.setattr(
        gcal_module.requests,
        "post",
        lambda *a, **k: FakeResponse({"refresh_token": "rt", "access_token": "at"}),
    )
    monkeypatch.setattr(gcal_module.requests, "get", lambda *a, **k: FakeResponse({"email": "a@b.com"}))
    service.handle_callback("code", state)

    calls = []

    def fake_post(endpoint, data=None, json=None, headers=None, timeout=None, **kwargs):
        calls.append((endpoint, data, json, headers))
        if endpoint == gcal_module.TOKEN_ENDPOINT:
            return FakeResponse({"access_token": "fresh-token"})
        return FakeResponse({"id": "evt-1", "htmlLink": "https://calendar.google.com/event?eid=evt-1"})

    monkeypatch.setattr(gcal_module.requests, "post", fake_post)

    result = service.create_event(_confirmed_session())

    assert result == {"id": "evt-1", "htmlLink": "https://calendar.google.com/event?eid=evt-1"}
    event_call = next(call for call in calls if call[0] == gcal_module.EVENTS_ENDPOINT)
    assert event_call[3]["Authorization"] == "Bearer fresh-token"


def test_create_event_raises_on_api_error(service, monkeypatch):
    url = service.authorization_url()
    state = url.split("state=")[1].split("&")[0]
    monkeypatch.setattr(
        gcal_module.requests,
        "post",
        lambda *a, **k: FakeResponse({"refresh_token": "rt", "access_token": "at"}),
    )
    monkeypatch.setattr(gcal_module.requests, "get", lambda *a, **k: FakeResponse({"email": "a@b.com"}))
    service.handle_callback("code", state)

    def fake_post(endpoint, data=None, json=None, headers=None, timeout=None, **kwargs):
        if endpoint == gcal_module.TOKEN_ENDPOINT:
            return FakeResponse({"access_token": "fresh-token"})
        return FakeResponse({"error": {"message": "insufficient scope"}}, status_code=403)

    monkeypatch.setattr(gcal_module.requests, "post", fake_post)

    with pytest.raises(GoogleCalendarApiError):
        service.create_event(_confirmed_session())
