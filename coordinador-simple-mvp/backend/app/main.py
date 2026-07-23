import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.bff.routes import router
from app.settings import settings
from app.services.session_service import session_service

logger = logging.getLogger("app")

app = FastAPI(title="Coordinador Simple MVP", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
