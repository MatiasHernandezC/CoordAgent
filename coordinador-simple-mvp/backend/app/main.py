import logging
import hmac
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.bff.routes import router
from app.bff.auth_routes import router as auth_router
from app.bff.slack_routes import router as slack_router
from app.settings import settings
from app.services.session_service import session_service
from app.services.auth_service import AuthUser, authenticate_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")

app = FastAPI(title="Coordinador Simple MVP", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _is_gateway_route(path: str) -> bool:
    return path in {"/api/channel/groups/resolve", "/api/channel/groups/link"} or bool(
        re.fullmatch(r"/api/sessions/[^/]+/channel/(config|messages|batch)", path)
    )


def _denied(request: Request, status_code: int, detail: str) -> JSONResponse:
    # CORSMiddleware no le agrega headers a una respuesta devuelta directo
    # desde este middleware (solo a las que pasan por call_next), asi que hay
    # que ponerlos a mano o el navegador la trata como error de CORS en vez
    # de mostrar el 401/403 real.
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    origin = request.headers.get("origin")
    if origin and origin in settings.cors_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


@app.middleware("http")
async def protect_panel_api(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS" or not path.startswith("/api/"):
        return await call_next(request)
    if path in {
        "/api/auth/register",
        "/api/channels/slack/events",
        "/api/channels/slack/interactions",
        "/api/admin/google-calendar/callback",
    }:
        return await call_next(request)

    if _is_gateway_route(path) and not request.headers.get("Authorization"):
        supplied = request.headers.get("X-Coordina-Gateway", "")
        expected = settings.gateway_api_token
        if settings.panel_auth_required and (not expected or not hmac.compare_digest(supplied, expected)):
            return _denied(request, 401, "Credencial interna del gateway invalida.")
        return await call_next(request)

    authorization_supplied = bool(request.headers.get("Authorization"))
    user = authenticate_request(request) if authorization_supplied else None
    if authorization_supplied and not user:
        return _denied(request, 401, "Credenciales invalidas.")
    if not user:
        if settings.panel_auth_required:
            return _denied(request, 401, "Debes iniciar sesion.")
        user = AuthUser("local-admin", "Administrador local", True)
    request.state.auth_user = user

    if not user.is_admin and path in {"/api/runtime", "/api/ops/status"}:
        return _denied(request, 403, "Esta funcion requiere una cuenta administradora.")
    if not user.is_admin and path.startswith("/api/admin/"):
        return _denied(request, 403, "Esta funcion requiere una cuenta administradora.")

    session_match = re.fullmatch(r"/api/sessions/([^/]+)(/.*)?", path)
    if session_match and not user.is_admin:
        session = session_service.get(session_match.group(1))
        is_owner = session.owner_username == user.username
        if not is_owner and user.username not in session.assigned_usernames:
            return _denied(request, 403, "Este grupo no esta asignado a tu cuenta.")
        if is_owner:
            if session_match.group(2) == "/access":
                return _denied(request, 403, "Solo el administrador de plataforma puede delegar accesos.")
            return await call_next(request)
        suffix = session_match.group(2) or ""
        allowed = (
            request.method == "GET" and suffix == ""
        ) or (
            request.method == "PUT" and suffix == "/chief"
        ) or (
            request.method == "PATCH" and suffix == "/channel/config"
        )
        if not allowed:
            return _denied(request, 403, "Tu cuenta no puede modificar esa funcion.")

    return await call_next(request)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # No filtra el traceback al cliente; queda registrado en el servidor.
    logger.exception("Error no manejado en %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor."})


@app.get("/health")
def health():
    return {"ok": True, "service": "coordinador-simple-mvp"}


@app.get("/ready")
def readiness():
    try:
        session_service.healthcheck()
    except Exception as exc:
        logger.warning("Readiness fallo: %s", exc)
        raise HTTPException(status_code=503, detail="Storage unavailable") from exc
    return {
        "ok": True,
        "service": "coordinador-simple-mvp",
        "storage": settings.db_backend,
    }


app.include_router(router, prefix="/api")
app.include_router(auth_router, prefix="/api/auth")
app.include_router(slack_router, prefix="/api/channels/slack")
