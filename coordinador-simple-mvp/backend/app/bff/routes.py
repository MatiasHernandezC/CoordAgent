from fastapi import APIRouter

from app.schemas import CreateSessionRequest, RuntimeInfo, Session
from app.settings import settings

router = APIRouter()


@router.get("/runtime", response_model=RuntimeInfo)
def runtime_info():
    model_by_provider = {
        "local": settings.local_llm_model,
        "gemini": settings.gemini_model,
        "ollama": settings.ollama_model,
        "mock": "rules",
    }
    model = model_by_provider.get(settings.llm_provider, "rules")
    return RuntimeInfo(
        provider=settings.llm_provider,
        provider_label=f"{settings.llm_provider}:{model}",
        model=model,
        cache_enabled=settings.llm_cache_enabled,
        gemini_configured=bool(settings.gemini_api_key),
    )


@router.post("/sessions")
def create_session(payload: CreateSessionRequest):
    session = Session(title=payload.title)
    return {"session": session}
