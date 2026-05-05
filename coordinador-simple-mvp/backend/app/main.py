from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.bff.routes import router
from app.settings import settings

app = FastAPI(title="Coordinador Simple MVP", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "service": "coordinador-simple-mvp"}


app.include_router(router, prefix="/api")
