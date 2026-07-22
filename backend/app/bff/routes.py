import base64
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
    ExportResponse,
    MessageRequest,
    MessageResponse,
    RuntimeInfo,
    ResolveChannelGroupRequest,
    TimeSlot,
)
from app.settings import settings
from app.services.calendar_export import build_calendar_ics
from app.services.image_render import render_availability_base64
from app.services.llm_service import LlmUnavailableError, llm_service
from app.services.ops_status import fetch_gateway_status
from app.services.session_service import (
    build_cancelled_channel_reply,
    build_channel_caption,
    build_channel_reply,
    build_confirmed_channel_reply,
    build_export_csv,
    build_export_text,
    build_help_reply,
    build_missing_info_reply,
    classify_channel_command,
    last_invoking_sender,
    resolve_reply_format,
    session_service,
)

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


@router.get("/ops/status")
def operational_status():
    gateway = fetch_gateway_status()
    return {"ok": gateway["connected"], "gateway": gateway}


@router.post("/sessions")
def create_session(payload: CreateSessionRequest):
    session = session_service.create(payload.title)
    return {"session": session}


@router.post("/channel/groups/resolve")
def resolve_channel_group(payload: ResolveChannelGroupRequest):
    with session_service.group_lock(payload.group_jid):
        session = session_service.resolve_channel_group(
            payload.group_jid,
            payload.group_name,
            payload.group_participant_count,
            payload.trigger_word,
        )
        return {"session": session}


@router.get("/sessions")
def list_sessions():
    return {"sessions": session_service.list_sessions()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    return {"session": session_service.get(session_id)}


@router.post("/sessions/{session_id}/message", response_model=MessageResponse)
def add_message(session_id: str, payload: MessageRequest):
    started = perf_counter()
    with session_service.session_lock(session_id):
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
    with session_service.session_lock(session_id):
        return {"session": session_service.add_participant(session_id, payload.name)}


@router.delete("/sessions/{session_id}/participants/{participant_name}")
def remove_participant(session_id: str, participant_name: str):
    with session_service.session_lock(session_id):
        return {"session": session_service.remove_participant(session_id, participant_name)}


@router.post("/sessions/{session_id}/availability")
def add_availability(session_id: str, payload: AddAvailabilityRequest):
    slot = TimeSlot(day=payload.day, start=payload.start, end=payload.end)
    with session_service.session_lock(session_id):
        return {"session": session_service.add_availability(session_id, payload.participant_name, slot)}


@router.post("/sessions/{session_id}/calculate")
def calculate(session_id: str):
    with session_service.session_lock(session_id):
        return {"session": session_service.calculate(session_id)}


@router.post("/sessions/{session_id}/confirm")
def confirm(session_id: str, payload: ConfirmRequest):
    with session_service.session_lock(session_id):
        return {"session": session_service.confirm(session_id, payload.option_id, source="panel")}


@router.post("/sessions/{session_id}/cancel-decision")
def cancel_decision(session_id: str):
    with session_service.session_lock(session_id):
        return {"session": session_service.cancel_decision(session_id)}


@router.post("/sessions/{session_id}/archive")
def archive_session(session_id: str):
    with session_service.session_lock(session_id):
        return {"session": session_service.archive(session_id)}


@router.post("/sessions/{session_id}/reopen")
def reopen_session(session_id: str):
    with session_service.session_lock(session_id):
        return {"session": session_service.reopen(session_id)}


@router.get("/sessions/{session_id}/export", response_model=ExportResponse)
def export_session(session_id: str):
    session = session_service.get(session_id)
    safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
    return ExportResponse(filename=f"{safe_name}-reporte.txt", text=build_export_text(session))


@router.get("/sessions/{session_id}/export.csv", response_model=ExportResponse)
def export_session_csv(session_id: str):
    session = session_service.get(session_id)
    safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
    return ExportResponse(filename=f"{safe_name}-datos.csv", text=build_export_csv(session))


@router.get("/sessions/{session_id}/calendar", response_model=ExportResponse)
def export_calendar(session_id: str):
    session = session_service.get(session_id)
    if not session.selected_option:
        raise HTTPException(status_code=400, detail="Confirma una opcion antes de generar calendario.")

    safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
    return ExportResponse(filename=f"{safe_name}-evento.ics", text=build_calendar_ics(session))


@router.patch("/sessions/{session_id}/channel/config")
def configure_channel(session_id: str, payload: ChannelConfigRequest):
    with session_service.session_lock(session_id):
        session = session_service.configure_channel(
            session_id,
            payload.listening_enabled,
            payload.trigger_word,
            payload.reply_format,
            payload.group_jid,
            payload.group_name,
            payload.group_participant_count,
        )
        return {"session": session}


@router.post("/sessions/{session_id}/channel/messages", response_model=ChannelMessageResponse)
def add_channel_message(session_id: str, payload: ChannelMessageRequest):
    started = perf_counter()
    with session_service.session_lock(session_id):
        session = session_service.get(session_id)
        existing = session_service.channel_message_by_external_id(session, payload.message_id)
        if existing and payload.message_id:
            saved_reply = session_service.agent_reply_for_external_id(session, payload.message_id)
            saved_receipt = session_service.command_receipt_by_external_id(session, payload.message_id)
            if saved_receipt:
                return replay_command_receipt(
                    session_id,
                    session,
                    saved_receipt,
                    started,
                    persist_reply=saved_reply is None,
                )
            if saved_reply:
                return replay_channel_response(session, saved_reply, started)
            saved_decision = session_service.decision_by_external_id(session, payload.message_id)
            if saved_decision:
                return recover_confirmed_response(session_id, session, payload.message_id, started)
            return invoke_channel_if_needed(
                session_id,
                session,
                existing.detected_invocation,
                started,
                external_id=payload.message_id,
                duplicate=True,
            )

        session, invoked = session_service.add_channel_message(
            session_id,
            payload.sender,
            payload.text,
            external_id=payload.message_id,
        )
        return invoke_channel_if_needed(
            session_id,
            session,
            invoked,
            started,
            external_id=payload.message_id,
        )


@router.post("/sessions/{session_id}/channel/batch", response_model=ChannelMessageResponse)
def add_channel_batch(session_id: str, payload: ChannelBatchRequest):
    started = perf_counter()
    messages = [(message.sender, message.text) for message in payload.messages]
    with session_service.session_lock(session_id):
        session, invoked = session_service.add_channel_messages(session_id, messages)
        return invoke_channel_if_needed(session_id, session, invoked, started)


def invoke_channel_if_needed(
    session_id: str,
    session,
    invoked: bool,
    started: float,
    external_id: str | None = None,
    duplicate: bool = False,
) -> ChannelMessageResponse:
    if not invoked:
        return ChannelMessageResponse(
            session=session,
            invoked=False,
            elapsed_ms=elapsed_ms(started),
            duplicate=duplicate,
        )

    command_response = invoke_channel_command_if_needed(
        session_id,
        session,
        started,
        external_id=external_id,
        duplicate=duplicate,
    )
    if command_response:
        return command_response

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

    reply_format = resolve_reply_format(session)
    reply_image = None
    reply_caption = None
    # Solo tiene sentido generar la imagen si hay datos que graficar.
    if reply_format in {"image", "both"} and session.participants:
        try:
            reply_image = render_availability_base64(session)
            reply_caption = build_channel_caption(session) if reply_format == "image" else reply
        except Exception as error:  # la imagen es opcional: nunca debe romper la respuesta
            reply_image = None
            reply_caption = None

    session = session_service.add_agent_channel_reply(
        session_id,
        reply,
        reply_to_external_id=external_id,
        reply_format=reply_format,
    )

    return ChannelMessageResponse(
        session=session,
        invoked=True,
        llm_source=source,
        agent_reply=reply,
        agent_reply_image=reply_image,
        agent_reply_caption=reply_caption,
        agent_reply_format=reply_format,
        elapsed_ms=elapsed_ms(started),
        token_usage=token_usage,
        duplicate=duplicate,
    )


def invoke_channel_command_if_needed(
    session_id: str,
    session,
    started: float,
    *,
    external_id: str | None = None,
    duplicate: bool = False,
) -> ChannelMessageResponse | None:
    command = classify_channel_command(session)
    if not command:
        return None

    # Fusiona la disponibilidad pendiente ANTES de ejecutar el comando. Sin esto,
    # los mensajes escritos desde la ultima respuesta del agente quedarian fuera
    # (la respuesta del comando resetea los pendientes) y ademas "confirma" o
    # "resumen" operarian sobre datos desactualizados.
    token_usage = None
    pending_messages = session_service.pending_channel_messages(session)
    if pending_messages:
        try:
            extraction, source, token_usage = llm_service.extract_channel_availability(pending_messages)
        except LlmUnavailableError as error:
            raise_llm_http_error(error)
        if extraction.participants or extraction.removals:
            session = session_service.merge_extraction(
                session_id,
                extraction,
                original_message="Invocacion desde canal simulado.",
                source=source,
                token_usage=token_usage,
            )

    command_name = command["name"]
    reply = ""
    document = None
    document_name = None
    document_mimetype = None

    if command_name == "help":
        reply = build_help_reply(session)

    elif command_name == "confirm":
        # Recalcula siempre: si llego disponibilidad nueva junto al comando,
        # las opciones deben reflejarla antes de cerrar la decision.
        session = session_service.calculate(session_id)

        option_index = command["option_index"]
        if option_index < 1 or option_index > len(session.options):
            option_label = command.get("option_label", option_index)
            reply = (
                "*Coordina*\n\n"
                f"No encuentro la opcion {option_label}. Pide *@coordina* para ver las opciones actuales."
            )
        else:
            option = session.options[option_index - 1]
            session = session_service.confirm(
                session_id,
                option.id,
                confirmed_by=last_invoking_sender(session),
                source="whatsapp",
                external_id=external_id,
            )
            reply = build_confirmed_channel_reply(session)
            # Adjunta el evento .ics para que cada uno lo agregue a su calendario.
            # Es opcional: si falla, la confirmacion en texto sigue saliendo.
            try:
                ics_text = build_calendar_ics(session)
                document = base64.b64encode(ics_text.encode("utf-8")).decode("ascii")
                safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
                document_name = f"{safe_name}-evento.ics"
                document_mimetype = "text/calendar"
            except Exception:
                document = None
                document_name = None
                document_mimetype = None

    elif command_name == "cancel":
        try:
            session = session_service.cancel_decision(session_id, external_id=external_id)
        except HTTPException:
            reply = (
                "*Coordina*\n\n"
                "No hay una decision confirmada que cancelar. "
                f"Escribe *{session.channel_config.trigger_word} resumen* para ver el estado."
            )
        else:
            reply = build_cancelled_channel_reply(session)

    elif command_name == "missing":
        if session.participants:
            session = session_service.calculate(session_id)
        reply = build_missing_info_reply(session)

    elif command_name == "export":
        reply = build_export_text(session)

    elif command_name == "summary":
        if session.participants:
            session = session_service.calculate(session_id)
        if session.status == "confirmed" and session.selected_option:
            reply = build_confirmed_channel_reply(session)
            try:
                ics_text = build_calendar_ics(session)
                document = base64.b64encode(ics_text.encode("utf-8")).decode("ascii")
                safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
                document_name = f"{safe_name}-evento.ics"
                document_mimetype = "text/calendar"
            except Exception:
                document = None
                document_name = None
                document_mimetype = None
        else:
            reply = build_channel_reply(session)

    elif command_name == "remove_participant":
        participant_name = command["participant_name"]
        try:
            session = session_service.remove_participant(
                session_id,
                participant_name,
                external_id=external_id,
            )
        except HTTPException:
            reply = (
                "*Coordina*\n\n"
                f"No encontre a {participant_name}. Revisa el nombre o escribe *@coordina exportar* para ver la lista."
            )
        else:
            reply = f"Listo: quite a {participant_name} de la sesion.\n\n{build_channel_reply(session)}"

    if not reply:
        return None

    session = session_service.add_agent_channel_reply(
        session_id,
        reply,
        reply_to_external_id=external_id,
        reply_format="text",
        attachment_kind="calendar" if document else None,
    )
    return ChannelMessageResponse(
        session=session,
        invoked=True,
        llm_source="channel_command",
        agent_reply=reply,
        agent_reply_format="text",
        agent_reply_document=document,
        agent_reply_document_name=document_name,
        agent_reply_document_mimetype=document_mimetype,
        elapsed_ms=elapsed_ms(started),
        token_usage=token_usage,
        duplicate=duplicate,
    )


def replay_channel_response(session, saved_reply, started: float) -> ChannelMessageResponse:
    """Reconstruye la entrega de un request ya procesado tras timeout/reinicio."""
    reply_format = saved_reply.reply_format or "text"
    reply_image = None
    reply_caption = None
    document = None
    document_name = None
    document_mimetype = None

    if reply_format in {"image", "both"} and session.participants:
        try:
            reply_image = render_availability_base64(session)
            reply_caption = build_channel_caption(session) if reply_format == "image" else saved_reply.text
        except Exception:
            reply_image = None
            reply_caption = None

    if saved_reply.attachment_kind == "calendar" and session.selected_option:
        try:
            ics_text = build_calendar_ics(session)
            document = base64.b64encode(ics_text.encode("utf-8")).decode("ascii")
            safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
            document_name = f"{safe_name}-evento.ics"
            document_mimetype = "text/calendar"
        except Exception:
            document = None
            document_name = None
            document_mimetype = None

    return ChannelMessageResponse(
        session=session,
        invoked=True,
        llm_source="idempotent_replay",
        agent_reply=saved_reply.text,
        agent_reply_image=reply_image,
        agent_reply_caption=reply_caption,
        agent_reply_format=reply_format,
        agent_reply_document=document,
        agent_reply_document_name=document_name,
        agent_reply_document_mimetype=document_mimetype,
        elapsed_ms=elapsed_ms(started),
        duplicate=True,
    )


def recover_confirmed_response(
    session_id: str,
    session,
    external_id: str,
    started: float,
) -> ChannelMessageResponse:
    """Cierra la ventana de crash entre guardar la decision y guardar su reply."""
    reply = build_confirmed_channel_reply(session)
    document = None
    document_name = None
    try:
        ics_text = build_calendar_ics(session)
        document = base64.b64encode(ics_text.encode("utf-8")).decode("ascii")
        safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
        document_name = f"{safe_name}-evento.ics"
    except Exception:
        document = None
        document_name = None

    session = session_service.add_agent_channel_reply(
        session_id,
        reply,
        reply_to_external_id=external_id,
        reply_format="text",
        attachment_kind="calendar" if document else None,
    )
    return ChannelMessageResponse(
        session=session,
        invoked=True,
        llm_source="idempotent_recovery",
        agent_reply=reply,
        agent_reply_format="text",
        agent_reply_document=document,
        agent_reply_document_name=document_name,
        agent_reply_document_mimetype="text/calendar" if document else None,
        elapsed_ms=elapsed_ms(started),
        duplicate=True,
    )


def replay_command_receipt(
    session_id: str,
    session,
    receipt,
    started: float,
    *,
    persist_reply: bool,
) -> ChannelMessageResponse:
    document = None
    document_name = None
    document_mimetype = None
    if receipt.document_text:
        document = base64.b64encode(receipt.document_text.encode("utf-8")).decode("ascii")
        safe_name = safe_filename(session.channel_config.group_name or session.title or "coordina")
        document_name = f"{safe_name}-evento.ics"
        document_mimetype = "text/calendar"

    if persist_reply:
        session = session_service.add_agent_channel_reply(
            session_id,
            receipt.reply,
            reply_to_external_id=receipt.external_id,
            reply_format=receipt.reply_format,
            attachment_kind=receipt.attachment_kind,
        )

    return ChannelMessageResponse(
        session=session,
        invoked=True,
        llm_source="idempotent_replay",
        agent_reply=receipt.reply,
        agent_reply_format=receipt.reply_format,
        agent_reply_document=document,
        agent_reply_document_name=document_name,
        agent_reply_document_mimetype=document_mimetype,
        elapsed_ms=elapsed_ms(started),
        duplicate=True,
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


def safe_filename(value: str) -> str:
    safe = value.strip().lower()
    safe = "".join(char if char.isalnum() else "-" for char in safe).strip("-")
    return safe or "coordina"
