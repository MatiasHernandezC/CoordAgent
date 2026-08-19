import base64

import pytest
from fastapi.testclient import TestClient

import app.bff.auth_routes as auth_routes_module
import app.bff.routes as routes_module
import app.services.auth_service as auth_module
import app.services.session_service as session_module
from app.main import app
from app.services.auth_service import AuthService
from app.settings import settings
from app.storage.json_repository import JsonRepository


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def protected_client(tmp_path, monkeypatch):
    service = AuthService(path=tmp_path / "users.json")
    service.create_user("admin", "Administrador", "admin-seguro", is_admin=True)
    service.create_user("usuario", "Usuario", "usuario-seguro")
    service.create_user("sin-acceso", "Sin acceso", "sin-acceso-seguro")
    monkeypatch.setattr(auth_module, "auth_service", service)
    monkeypatch.setattr(auth_routes_module, "auth_service", service)
    monkeypatch.setattr(routes_module, "auth_service", service)
    monkeypatch.setattr(session_module, "repository", JsonRepository(tmp_path / "sessions.json"))
    monkeypatch.setattr(settings, "panel_auth_required", True)
    monkeypatch.setattr(settings, "gateway_api_token", "gateway-test-token")
    return TestClient(app)


def test_login_and_user_creation_are_admin_only(protected_client):
    assert protected_client.get("/api/auth/me").status_code == 401
    assert protected_client.get("/api/auth/me", headers=basic("usuario", "incorrecta")).status_code == 401

    me = protected_client.get("/api/auth/me", headers=basic("usuario", "usuario-seguro"))
    assert me.status_code == 200
    assert me.json()["user"] == {
        "username": "usuario",
        "display_name": "Usuario",
        "is_admin": False,
        "role": "group_admin",
    }

    denied = protected_client.post(
        "/api/auth/users",
        headers=basic("usuario", "usuario-seguro"),
        json={"username": "nuevo", "display_name": "Nuevo", "password": "nuevo-seguro"},
    )
    assert denied.status_code == 403

    created = protected_client.post(
        "/api/auth/users",
        headers=basic("admin", "admin-seguro"),
        json={"username": "nuevo", "display_name": "Nuevo", "password": "nuevo-seguro"},
    )
    assert created.status_code == 201
    assert "password" not in str(created.json()).lower()
    duplicate = protected_client.post(
        "/api/auth/users",
        headers=basic("admin", "admin-seguro"),
        json={"username": "nuevo", "display_name": "Nuevo", "password": "otro-seguro"},
    )
    assert duplicate.status_code == 409


def test_user_only_sees_assigned_groups_and_can_assign_chief(protected_client):
    admin = basic("admin", "admin-seguro")
    user = basic("usuario", "usuario-seguro")
    outsider = basic("sin-acceso", "sin-acceso-seguro")

    session_id = protected_client.post("/api/sessions", headers=admin, json={"title": "Slack - #tavi"}).json()["session"]["id"]
    participant = protected_client.post(
        f"/api/sessions/{session_id}/participants",
        headers=admin,
        json={"name": "Daniel"},
    ).json()["session"]["participants"][0]
    assignment = protected_client.put(
        f"/api/sessions/{session_id}/access",
        headers=admin,
        json={"usernames": ["usuario"]},
    )
    assert assignment.status_code == 200

    visible = protected_client.get("/api/sessions", headers=user)
    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()["sessions"]] == [session_id]
    assert visible.json()["sessions"][0]["messages"] == []
    assert visible.json()["sessions"][0]["channel_config"]["coordinator_ids"] == []
    assert protected_client.get(f"/api/sessions/{session_id}", headers=outsider).status_code == 403
    assert protected_client.post(
        f"/api/sessions/{session_id}/participants",
        headers=user,
        json={"name": "Nicolas"},
    ).status_code == 403

    chief = protected_client.put(
        f"/api/sessions/{session_id}/chief",
        headers=user,
        json={"participant_id": participant["id"]},
    )
    assert chief.status_code == 200
    assert chief.json()["session"]["chief_participant_id"] == participant["id"]
    assert chief.json()["session"]["messages"] == []
    assert protected_client.get("/api/runtime", headers=user).status_code == 403


def test_gateway_routes_require_the_internal_token(protected_client):
    payload = {
        "group_jid": "demo@g.us",
        "group_name": "Demo",
        "group_participant_count": 2,
        "group_participant_ids": ["a", "b"],
        "coordinator_ids": ["a"],
        "trigger_word": "@coordina",
    }
    assert protected_client.post("/api/channel/groups/resolve", json=payload).status_code == 401
    accepted = protected_client.post(
        "/api/channel/groups/resolve",
        headers={"X-Coordina-Gateway": "gateway-test-token"},
        json=payload,
    )
    assert accepted.status_code == 200


def test_public_registration_creates_group_admin_and_owner_can_manage_own_group(protected_client):
    registered = protected_client.post(
        "/api/auth/register",
        json={
            "username": "propietaria",
            "display_name": "Propietaria Grupo",
            "password": "propietaria-segura",
        },
    )
    assert registered.status_code == 201
    assert registered.json()["user"] == {
        "username": "propietaria",
        "display_name": "Propietaria Grupo",
        "is_admin": False,
        "role": "group_admin",
    }

    owner = basic("propietaria", "propietaria-segura")
    created = protected_client.post(
        "/api/sessions",
        headers=owner,
        json={"title": "Equipo de proyecto"},
    )
    assert created.status_code == 200
    session = created.json()["session"]
    assert session["owner_username"] == "propietaria"
    assert session["assigned_usernames"] == ["propietaria"]
    assert len(session["link_code"]) == 10

    participant = protected_client.post(
        f"/api/sessions/{session['id']}/participants",
        headers=owner,
        json={"name": "Daniel"},
    )
    assert participant.status_code == 200
    assert participant.json()["session"]["participants"][0]["name"] == "Daniel"

    outsider = basic("sin-acceso", "sin-acceso-seguro")
    assert protected_client.get(f"/api/sessions/{session['id']}", headers=outsider).status_code == 403
    assert protected_client.get("/api/admin/llm-keys", headers=owner).status_code == 403


def test_gateway_links_pending_group_once(protected_client):
    owner = basic("usuario", "usuario-seguro")
    session = protected_client.post(
        "/api/sessions",
        headers=owner,
        json={"title": "Grupo pendiente"},
    ).json()["session"]
    gateway = {"X-Coordina-Gateway": "gateway-test-token"}
    payload = {
        "link_code": session["link_code"],
        "group_jid": "nuevo@g.us",
        "group_name": "Grupo real",
        "group_participant_count": 3,
        "group_participant_ids": ["a", "b", "c"],
        "coordinator_ids": ["a"],
        "trigger_word": "@coordina",
        "create_if_missing": False,
    }
    linked = protected_client.post("/api/channel/groups/link", headers=gateway, json=payload)
    assert linked.status_code == 200
    assert linked.json()["session"]["owner_username"] == "usuario"
    assert linked.json()["session"]["link_code"] is None
    assert linked.json()["session"]["channel_config"]["group_jid"] == "nuevo@g.us"
    assert protected_client.post("/api/channel/groups/link", headers=gateway, json=payload).status_code == 404
