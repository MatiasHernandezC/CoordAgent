"""Directorio de administradores: cada admin autenticado queda registrado al
tocar la API (require_admin_actor -> admin_directory_service.touch), y un
superadmin puede promover/degradar a otros desde /api/admin/users sin tocar
SUPERADMIN_USERS ni reiniciar el backend."""
import pytest
from fastapi.testclient import TestClient

import app.services.admin_directory_service as directory_module
import app.services.session_service as session_module
from app.main import app
from app.settings import settings
from app.storage.json_admin_directory_repository import JsonAdminDirectoryRepository
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "admin_proxy_header_required", True)
    monkeypatch.setattr(settings, "superadmin_users", {"boss"})
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    directory_repository = JsonAdminDirectoryRepository(tmp_path / "admins.json")
    monkeypatch.setattr(directory_module, "repository", directory_repository)
    return TestClient(app)


def _headers(actor):
    return {"X-Coordina-Admin": actor}


def _create(client, title, actor="boss"):
    response = client.post("/api/sessions", json={"title": title}, headers=_headers(actor))
    assert response.status_code == 200
    return response.json()["session"]["id"]


def test_touch_registers_a_new_admin_on_first_request(client):
    client.get("/api/sessions", headers=_headers("ti_admin"))

    response = client.get("/api/admin/users", headers=_headers("boss"))
    assert response.status_code == 200
    actors = {item["actor"] for item in response.json()["users"]}
    assert "ti_admin" in actors


def test_list_admin_users_requires_superadmin(client):
    client.get("/api/sessions", headers=_headers("ti_admin"))
    blocked = client.get("/api/admin/users", headers=_headers("ti_admin"))
    assert blocked.status_code == 403


def test_env_superadmin_appears_even_without_activity(client):
    response = client.get("/api/admin/users", headers=_headers("boss"))
    assert response.status_code == 200
    boss = next(item for item in response.json()["users"] if item["actor"] == "boss")
    assert boss["is_superadmin"] is True
    assert boss["superadmin_locked"] is True


def test_superadmin_can_promote_another_admin(client):
    client.get("/api/sessions", headers=_headers("ti_admin"))

    promoted = client.patch(
        "/api/admin/users/ti_admin", json={"is_superadmin": True}, headers=_headers("boss")
    )
    assert promoted.status_code == 200
    ti_admin = next(item for item in promoted.json()["users"] if item["actor"] == "ti_admin")
    assert ti_admin["is_superadmin"] is True
    assert ti_admin["superadmin_locked"] is False

    # La promocion es real: ahora ti_admin puede administrar grupos ajenos.
    finance_id = _create(client, "Grupo Finanzas")
    assert client.patch(
        f"/api/sessions/{finance_id}/channel/config",
        json={"owner_admin": "finanzas"},
        headers=_headers("boss"),
    ).status_code == 200
    access = client.get(f"/api/sessions/{finance_id}", headers=_headers("ti_admin"))
    assert access.status_code == 200


def test_non_superadmin_cannot_promote(client):
    client.get("/api/sessions", headers=_headers("ti_admin"))
    client.get("/api/sessions", headers=_headers("otro"))

    blocked = client.patch(
        "/api/admin/users/otro", json={"is_superadmin": True}, headers=_headers("ti_admin")
    )
    assert blocked.status_code == 403


def test_cannot_demote_env_locked_superadmin(client):
    blocked = client.patch(
        "/api/admin/users/boss", json={"is_superadmin": False}, headers=_headers("boss")
    )
    assert blocked.status_code == 400


def test_owned_groups_count_reflects_claims(client):
    client.get("/api/sessions", headers=_headers("finanzas"))
    session_id = _create(client, "Grupo Finanzas")
    assert client.patch(
        f"/api/sessions/{session_id}/channel/config",
        json={"owner_admin": "finanzas"},
        headers=_headers("boss"),
    ).status_code == 200

    response = client.get("/api/admin/users", headers=_headers("boss"))
    finanzas = next(item for item in response.json()["users"] if item["actor"] == "finanzas")
    assert finanzas["owned_groups"] == 1
