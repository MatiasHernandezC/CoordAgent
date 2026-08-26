import base64

import pytest
from fastapi import HTTPException
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
    me_user = me.json()["user"]
    assert {key: me_user[key] for key in ("username", "display_name", "is_admin", "role", "active")} == {
        "username": "usuario",
        "display_name": "Usuario",
        "is_admin": False,
        "role": "group_admin",
        "active": True,
    }
    assert me_user["created_at"]

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
    registered_user = registered.json()["user"]
    assert {key: registered_user[key] for key in ("username", "display_name", "is_admin", "role", "active")} == {
        "username": "propietaria",
        "display_name": "Propietaria Grupo",
        "is_admin": False,
        "role": "group_admin",
        "active": True,
    }
    assert registered_user["created_at"]

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


def test_admin_can_edit_and_reset_password_but_group_admin_cannot(protected_client):
    admin = basic("admin", "admin-seguro")
    user = basic("usuario", "usuario-seguro")

    denied = protected_client.patch(
        "/api/auth/users/usuario",
        headers=user,
        json={"display_name": "Nombre indebido"},
    )
    assert denied.status_code == 403

    updated = protected_client.patch(
        "/api/auth/users/usuario",
        headers=admin,
        json={"display_name": "Usuario Editado"},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["display_name"] == "Usuario Editado"

    reset = protected_client.post(
        "/api/auth/users/usuario/password",
        headers=admin,
        json={"password": "usuario-nueva-segura"},
    )
    assert reset.status_code == 200
    assert reset.json()["password_reset"] is True
    assert "password" not in reset.json()["user"]
    assert protected_client.get("/api/auth/me", headers=user).status_code == 401
    assert protected_client.get(
        "/api/auth/me",
        headers=basic("usuario", "usuario-nueva-segura"),
    ).status_code == 200


def test_owner_must_transfer_groups_before_deactivation(protected_client):
    admin = basic("admin", "admin-seguro")
    owner = basic("usuario", "usuario-seguro")
    new_owner = basic("sin-acceso", "sin-acceso-seguro")
    session = protected_client.post(
        "/api/sessions",
        headers=owner,
        json={"title": "Grupo transferible"},
    ).json()["session"]

    blocked = protected_client.patch(
        "/api/auth/users/usuario",
        headers=admin,
        json={"active": False},
    )
    assert blocked.status_code == 409
    assert "Transfiere primero" in blocked.json()["detail"]

    denied_transfer = protected_client.put(
        f"/api/sessions/{session['id']}/owner",
        headers=owner,
        json={"username": "sin-acceso"},
    )
    assert denied_transfer.status_code == 403

    transferred = protected_client.put(
        f"/api/sessions/{session['id']}/owner",
        headers=admin,
        json={"username": "sin-acceso"},
    )
    assert transferred.status_code == 200
    transferred_session = transferred.json()["session"]
    assert transferred_session["owner_username"] == "sin-acceso"
    assert transferred_session["assigned_usernames"] == ["sin-acceso"]
    assert protected_client.get(f"/api/sessions/{session['id']}", headers=owner).status_code == 403
    assert protected_client.get(f"/api/sessions/{session['id']}", headers=new_owner).status_code == 200

    disabled = protected_client.patch(
        "/api/auth/users/usuario",
        headers=admin,
        json={"active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["user"]["active"] is False
    assert protected_client.get("/api/auth/me", headers=owner).status_code == 401

    inactive_assignment = protected_client.put(
        f"/api/sessions/{session['id']}/access",
        headers=admin,
        json={"usernames": ["sin-acceso", "usuario"]},
    )
    assert inactive_assignment.status_code == 400
    assert "Usuarios inactivos" in inactive_assignment.json()["detail"]

    reactivated = protected_client.patch(
        "/api/auth/users/usuario",
        headers=admin,
        json={"active": True},
    )
    assert reactivated.status_code == 200
    assert protected_client.get("/api/auth/me", headers=owner).status_code == 200


def test_admin_cannot_deactivate_self_or_last_active_admin(tmp_path):
    service = AuthService(path=tmp_path / "users.json")
    service.create_user("admin-uno", "Admin Uno", "admin-uno-seguro", is_admin=True)
    service.create_user("admin-dos", "Admin Dos", "admin-dos-seguro", is_admin=True)

    with pytest.raises(HTTPException) as self_error:
        service.update_user("admin-uno", active=False, actor_username="admin-uno")
    assert getattr(self_error.value, "status_code", None) == 400

    service.update_user("admin-dos", active=False, actor_username="admin-uno")
    with pytest.raises(HTTPException) as last_admin_error:
        service.update_user("admin-uno", active=False, actor_username="admin-dos")
    assert getattr(last_admin_error.value, "status_code", None) == 409
