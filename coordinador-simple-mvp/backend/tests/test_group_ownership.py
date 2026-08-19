"""Administracion por grupos: cada admin del panel (X-Coordina-Admin) solo ve
y administra las sesiones cuyo owner_admin le pertenece o que aun no tienen
dueno; un superadmin (settings.superadmin_users) ve y administra todas."""
import pytest
from fastapi.testclient import TestClient

import app.services.session_service as session_module
from app.main import app
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "admin_proxy_header_required", True)
    monkeypatch.setattr(settings, "superadmin_users", {"boss"})
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return TestClient(app)


def _create(client, title, actor="boss"):
    headers = {"X-Coordina-Admin": actor} if actor else {}
    response = client.post("/api/sessions", json={"title": title}, headers=headers)
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _claim(client, session_id, actor, owner_admin):
    return client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={"owner_admin": owner_admin},
        headers={"X-Coordina-Admin": actor},
    )


def test_list_sessions_filters_by_owner(client):
    finance_id = _create(client, "Grupo Finanzas")
    unowned_id = _create(client, "Grupo sin asignar")
    assert _claim(client, finance_id, "boss", "finanzas").status_code == 200

    response = client.get("/api/sessions", headers={"X-Coordina-Admin": "ti_admin"})
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["sessions"]}
    assert finance_id not in ids
    assert unowned_id in ids

    finance_view = client.get("/api/sessions", headers={"X-Coordina-Admin": "finanzas"})
    assert finance_id in {item["id"] for item in finance_view.json()["sessions"]}

    superadmin_view = client.get("/api/sessions", headers={"X-Coordina-Admin": "boss"})
    assert finance_id in {item["id"] for item in superadmin_view.json()["sessions"]}


def test_get_session_blocks_non_owner_and_allows_superadmin(client):
    finance_id = _create(client, "Grupo Finanzas")
    assert _claim(client, finance_id, "boss", "finanzas").status_code == 200

    blocked = client.get(f"/api/sessions/{finance_id}", headers={"X-Coordina-Admin": "ti_admin"})
    assert blocked.status_code == 403

    owner_view = client.get(f"/api/sessions/{finance_id}", headers={"X-Coordina-Admin": "finanzas"})
    assert owner_view.status_code == 200

    superadmin_view = client.get(f"/api/sessions/{finance_id}", headers={"X-Coordina-Admin": "boss"})
    assert superadmin_view.status_code == 200


def test_any_admin_can_claim_unowned_session(client):
    session_id = _create(client, "Grupo nuevo")
    response = _claim(client, session_id, "ti_admin", "ti_admin")
    assert response.status_code == 200
    assert response.json()["session"]["channel_config"]["owner_admin"] == "ti_admin"


def test_non_superadmin_cannot_reassign_owned_session(client):
    session_id = _create(client, "Grupo Finanzas")
    assert _claim(client, session_id, "boss", "finanzas").status_code == 200

    # ti_admin no puede ni ver la sesion (403 en el gate de acceso), asi que
    # tampoco puede reasignarla.
    stolen = _claim(client, session_id, "ti_admin", "ti_admin")
    assert stolen.status_code == 403


def test_owner_can_release_but_not_reassign_to_someone_else(client):
    session_id = _create(client, "Grupo Finanzas")
    assert _claim(client, session_id, "boss", "finanzas").status_code == 200

    # El dueno puede liberar su propio grupo...
    released = _claim(client, session_id, "finanzas", "")
    assert released.status_code == 200
    assert released.json()["session"]["channel_config"]["owner_admin"] is None

    # ...pero no puede reasignarlo directamente a otra persona sin ser superadmin.
    assert _claim(client, session_id, "boss", "finanzas").status_code == 200
    reassign_attempt = _claim(client, session_id, "finanzas", "otro_admin")
    assert reassign_attempt.status_code == 403


def test_superadmin_can_reassign_and_release(client):
    session_id = _create(client, "Grupo Finanzas")
    assert _claim(client, session_id, "boss", "finanzas").status_code == 200

    reassigned = _claim(client, session_id, "boss", "ti_admin")
    assert reassigned.status_code == 200
    assert reassigned.json()["session"]["channel_config"]["owner_admin"] == "ti_admin"

    released = _claim(client, session_id, "boss", "")
    assert released.status_code == 200
    assert released.json()["session"]["channel_config"]["owner_admin"] is None


def test_unowned_session_can_only_be_claimed_for_self(client):
    session_id = _create(client, "Grupo nuevo")

    stolen = _claim(client, session_id, "ti_admin", "otro_admin")
    assert stolen.status_code == 403

    claimed = _claim(client, session_id, "ti_admin", "ti_admin")
    assert claimed.status_code == 200
    assert claimed.json()["session"]["channel_config"]["owner_admin"] == "ti_admin"


def test_mutating_endpoints_respect_group_access(client):
    session_id = _create(client, "Grupo Finanzas")
    assert _claim(client, session_id, "boss", "finanzas").status_code == 200
    owner_headers = {"X-Coordina-Admin": "finanzas"}
    other_headers = {"X-Coordina-Admin": "ti_admin"}

    assert client.post(
        f"/api/sessions/{session_id}/participants", json={"name": "Ana"}, headers=other_headers
    ).status_code == 403
    assert client.post(
        f"/api/sessions/{session_id}/participants", json={"name": "Ana"}, headers=owner_headers
    ).status_code == 200

    assert client.post(
        f"/api/sessions/{session_id}/participants/requirements",
        json={"name": "Ana", "required": True},
        headers=other_headers,
    ).status_code == 403
    assert client.post(
        f"/api/sessions/{session_id}/participants/requirements",
        json={"name": "Ana", "required": True},
        headers=owner_headers,
    ).status_code == 200

    assert client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Ana", "day": "lunes", "start": "09:00", "end": "10:00"},
        headers=other_headers,
    ).status_code == 403
    assert client.post(
        f"/api/sessions/{session_id}/availability",
        json={"participant_name": "Ana", "day": "lunes", "start": "09:00", "end": "10:00"},
        headers=owner_headers,
    ).status_code == 200

    assert client.post(f"/api/sessions/{session_id}/calculate", headers=other_headers).status_code == 403
    assert client.post(f"/api/sessions/{session_id}/calculate", headers=owner_headers).status_code == 200

    assert client.post(f"/api/sessions/{session_id}/cancel-decision", headers=other_headers).status_code == 403
    # No hay decision confirmada que cancelar (400), pero eso prueba que el
    # gate de acceso ya no es lo que bloquea al dueno.
    assert client.post(f"/api/sessions/{session_id}/cancel-decision", headers=owner_headers).status_code == 400

    assert client.post(
        f"/api/sessions/{session_id}/message", json={"message": "hola"}, headers=other_headers
    ).status_code == 403
    assert client.post(
        f"/api/sessions/{session_id}/message", json={"message": "hola"}, headers=owner_headers
    ).status_code == 200

    assert client.get(f"/api/sessions/{session_id}/export", headers=other_headers).status_code == 403
    assert client.get(f"/api/sessions/{session_id}/export", headers=owner_headers).status_code == 200

    assert client.get(f"/api/sessions/{session_id}/export.csv", headers=other_headers).status_code == 403
    assert client.get(f"/api/sessions/{session_id}/export.csv", headers=owner_headers).status_code == 200

    assert client.delete(
        f"/api/sessions/{session_id}/participants/Ana", headers=other_headers
    ).status_code == 403
    assert client.delete(
        f"/api/sessions/{session_id}/participants/Ana", headers=owner_headers
    ).status_code == 200

    # Sin opcion confirmada el endpoint devolveria 400, pero el gate de acceso
    # debe cortar antes con 403 para quien no es dueno del grupo.
    assert client.get(f"/api/sessions/{session_id}/calendar", headers=other_headers).status_code == 403


def test_archive_endpoint_respects_group_access(client):
    session_id = _create(client, "Grupo Finanzas")
    assert _claim(client, session_id, "boss", "finanzas").status_code == 200

    blocked = client.post(f"/api/sessions/{session_id}/archive", headers={"X-Coordina-Admin": "ti_admin"})
    assert blocked.status_code == 403

    allowed = client.post(f"/api/sessions/{session_id}/archive", headers={"X-Coordina-Admin": "finanzas"})
    assert allowed.status_code == 200


def test_dev_mode_without_header_still_lists_unowned_sessions(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_proxy_header_required", False)
    session_id = _create(client, "Grupo de prueba local", actor="")

    response = client.get("/api/sessions")
    assert response.status_code == 200
    assert session_id in {item["id"] for item in response.json()["sessions"]}


def test_runtime_reports_actor_and_superadmin_flag(client):
    superadmin_view = client.get("/api/runtime", headers={"X-Coordina-Admin": "boss"})
    assert superadmin_view.status_code == 200
    body = superadmin_view.json()
    assert body["actor"] == "boss"
    assert body["is_superadmin"] is True

    area_admin_view = client.get("/api/runtime", headers={"X-Coordina-Admin": "finanzas"})
    body = area_admin_view.json()
    assert body["actor"] == "finanzas"
    assert body["is_superadmin"] is False
