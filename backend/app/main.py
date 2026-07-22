import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.bff.routes import router
from app.settings import settings

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


app.include_router(router, prefix="/api")
