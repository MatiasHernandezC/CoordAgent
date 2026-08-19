from fastapi import APIRouter, Request

from fastapi import HTTPException

from app.schemas import CreatePanelUserRequest, RegisterPanelUserRequest
from app.settings import settings
from app.services.auth_service import auth_service, current_user, require_admin_user


router = APIRouter()


def public_user(user):
    return {
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "role": user.role,
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
