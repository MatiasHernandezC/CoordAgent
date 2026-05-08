from time import perf_counter

from fastapi import APIRouter, HTTPException

from app.schemas import (
    AddAvailabilityRequest,
    AddParticipantRequest,
    ChannelBatchRequest,
    ChannelConfigRequest,
    ChannelMessageRequest,
    ChannelMessageResponse,
    ConfirmRequest,
    CreateSessionRequest,
    MessageRequest,
    MessageResponse,
    RuntimeInfo,
    TimeSlot,
)
from app.settings import settings
from app.services.llm_service import LlmUnavailableError, llm_service
from app.services.session_service import build_channel_reply, session_service

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
        fallback_enabled=settings.llm_fallback_enabled,
        gemini_configured=bool(settings.gemini_api_key),
        warnings=settings.runtime_warnings,
    )


@router.post("/sessions")
def create_session(payload: CreateSessionRequest):
    session = session_service.create(payload.title)
    return {"session": session}


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    return {"session": session_service.get(session_id)}


@router.post("/sessions/{session_id}/message", response_model=MessageResponse)
def add_message(session_id: str, payload: MessageRequest):
    started = perf_counter()
    try:
        extraction, source, token_usage = llm_service.extract_availability(payload.message)
    except LlmUnavailableError as error:
        raise_llm_http_error(error)

    session = session_service.merge_extraction(session_id, extraction, payload.message, source, token_usage)
    return MessageResponse(
        session=session,
        llm_source=source,
        elapsed_ms=elapsed_ms(started),
        token_usage=token_usage,
    )


@router.post("/sessions/{session_id}/participants")
def add_participant(session_id: str, payload: AddParticipantRequest):
    return {"session": session_service.add_participant(session_id, payload.name)}


@router.post("/sessions/{session_id}/availability")
def add_availability(session_id: str, payload: AddAvailabilityRequest):
    slot = TimeSlot(day=payload.day, start=payload.start, end=payload.end)
    return {"session": session_service.add_availability(session_id, payload.participant_name, slot)}


@router.post("/sessions/{session_id}/calculate")
def calculate(session_id: str):
    return {"session": session_service.calculate(session_id)}


@router.post("/sessions/{session_id}/confirm")
def confirm(session_id: str, payload: ConfirmRequest):
    return {"session": session_service.confirm(session_id, payload.option_id)}


@router.patch("/sessions/{session_id}/channel/config")
def configure_channel(session_id: str, payload: ChannelConfigRequest):
    session = session_service.configure_channel(session_id, payload.listening_enabled, payload.trigger_word)
    return {"session": session}


@router.post("/sessions/{session_id}/channel/messages", response_model=ChannelMessageResponse)
def add_channel_message(session_id: str, payload: ChannelMessageRequest):
    started = perf_counter()
    session, invoked = session_service.add_channel_message(session_id, payload.sender, payload.text)
    return invoke_channel_if_needed(session_id, session, invoked, started)


@router.post("/sessions/{session_id}/channel/batch", response_model=ChannelMessageResponse)
def add_channel_batch(session_id: str, payload: ChannelBatchRequest):
    started = perf_counter()
    messages = [(message.sender, message.text) for message in payload.messages]
    session, invoked = session_service.add_channel_messages(session_id, messages)
    return invoke_channel_if_needed(session_id, session, invoked, started)


def invoke_channel_if_needed(
    session_id: str,
    session,
    invoked: bool,
    started: float,
) -> ChannelMessageResponse:
    if not invoked:
        return ChannelMessageResponse(session=session, invoked=False, elapsed_ms=elapsed_ms(started))

    pending_messages = session_service.pending_channel_messages(session)
    try:
        extraction, source, token_usage = llm_service.extract_channel_availability(pending_messages)
    except LlmUnavailableError as error:
        raise_llm_http_error(error)

    session = session_service.merge_extraction(
        session_id,
        extraction,
        original_message="Invocacion desde canal simulado.",
        source=source,
        token_usage=token_usage,
    )
    session = session_service.calculate(session_id)
    reply = build_channel_reply(session)
    session = session_service.add_agent_channel_reply(session_id, reply)

    return ChannelMessageResponse(
        session=session,
        invoked=True,
        llm_source=source,
        agent_reply=reply,
        elapsed_ms=elapsed_ms(started),
        token_usage=token_usage,
    )


def raise_llm_http_error(error: LlmUnavailableError) -> None:
    headers = {}
    if error.retry_after_seconds:
        headers["Retry-After"] = str(error.retry_after_seconds)

    raise HTTPException(
        status_code=error.status_code,
        detail=error.message,
        headers=headers,
    )


def elapsed_ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)
