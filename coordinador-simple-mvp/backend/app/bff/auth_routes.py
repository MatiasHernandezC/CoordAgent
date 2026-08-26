from fastapi import APIRouter, Request

from fastapi import HTTPException

from app.schemas import (
    CreatePanelUserRequest,
    RegisterPanelUserRequest,
    ResetPanelUserPasswordRequest,
    UpdatePanelUserRequest,
)
from app.settings import settings
from app.services.auth_service import auth_service, current_user, require_admin_user
from app.services.session_service import session_service


router = APIRouter()


def public_user(user):
    return {
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "role": user.role,
        "active": user.active,
        "created_at": user.created_at,
    }


@router.post("/register", status_code=201)
def register(payload: RegisterPanelUserRequest):
    if not settings.panel_self_registration_enabled:
        raise HTTPException(status_code=403, detail="El registro de cuentas esta desactivado.")
    user = auth_service.create_user(
        payload.username,
        payload.display_name,
        payload.password,
        role="group_admin",
    )
    return {"user": public_user(user)}


@router.get("/me")
def me(request: Request):
    return {"user": public_user(current_user(request))}


@router.get("/users")
def users(request: Request):
    require_admin_user(request)
    return {"users": [public_user(user) for user in auth_service.list_users()]}


@router.post("/users", status_code=201)
def create_user(payload: CreatePanelUserRequest, request: Request):
    require_admin_user(request)
    user = auth_service.create_user(payload.username, payload.display_name, payload.password)
    return {"user": public_user(user)}


@router.patch("/users/{username}")
def update_user(username: str, payload: UpdatePanelUserRequest, request: Request):
    actor = require_admin_user(request)
    target = auth_service.get_user(username)
    if not target:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if payload.active is False:
        owned_sessions = [
            session
            for session in session_service.list_sessions()
            if session.owner_username == target.username
        ]
        if owned_sessions:
            titles = ", ".join(session.title for session in owned_sessions[:3])
            suffix = "" if len(owned_sessions) <= 3 else f" y {len(owned_sessions) - 3} mas"
            raise HTTPException(
                status_code=409,
                detail=f"Transfiere primero sus {len(owned_sessions)} grupo(s): {titles}{suffix}.",
            )
    user = auth_service.update_user(
        username,
        display_name=payload.display_name,
        active=payload.active,
        actor_username=actor.username,
    )
    return {"user": public_user(user)}


@router.post("/users/{username}/password")
def reset_user_password(username: str, payload: ResetPanelUserPasswordRequest, request: Request):
    require_admin_user(request)
    user = auth_service.reset_password(username, payload.password)
    return {"user": public_user(user), "password_reset": True}
