import base64
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request

from app.schemas import (
    AddAvailabilityRequest,
    AddParticipantRequest,
    ChannelBatchRequest,
    ChannelConfigRequest,
    ChannelMessageRequest,
    ChannelMessageResponse,
    ConfirmRequest,
    CreateSessionRequest,
    CreateLlmKeyRequest,
    DeleteLlmKeyRequest,
    ExportResponse,
    LlmKeyListResponse,
    MessageRequest,
    MessageResponse,
    RuntimeInfo,
    ResolveChannelGroupRequest,
    TimeSlot,
    UpdateLlmKeyRequest,
)
from app.settings import settings
from app.services.calendar_export import build_calendar_ics
from app.services.image_render import render_availability_base64
from app.services.llm_service import LlmUnavailableError, llm_service
from app.services.llm_key_service import llm_key_service
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
    confirmation_blockers,
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
    key_state = llm_key_service.list_state(audit_limit=1)
    active_key = next(
        (item for item in key_state["keys"] if item["id"] == key_state["active_key_id"]),
        None,
    )
    warnings = list(settings.runtime_warnings)
    if settings.llm_provider == "gemini" and key_state["available_count"] == 0:
        warnings.append("No hay llaves Gemini disponibles; el sistema usara el fallback configurado.")
    return RuntimeInfo(
        provider=settings.llm_provider,
        provider_label=f"{settings.llm_provider}:{model}",
        model=model,
        cache_enabled=settings.llm_cache_enabled,
        fallback_enabled=settings.llm_fallback_enabled,
        gemini_configured=key_state["available_count"] > 0,
        gemini_key_count=len(key_state["keys"]),
        gemini_available_key_count=key_state["available_count"],
        gemini_active_key_name=active_key["name"] if active_key else None,
        gemini_key_management_enabled=key_state["management_enabled"],
        warnings=warnings,
    )


@router.get("/ops/status")
def operational_status():
    gateway = fetch_gateway_status()
    return {"ok": gateway["connected"], "gateway": gateway}


@router.get("/admin/llm-keys", response_model=LlmKeyListResponse)
def list_llm_keys(request: Request):
    require_admin_actor(request)
    return LlmKeyListResponse.model_validate(llm_key_service.list_state())


@router.post("/admin/llm-keys")
def create_llm_key(payload: CreateLlmKeyRequest, request: Request):
    actor = require_admin_actor(request)
    key = llm_key_service.add(
        payload.name,
        payload.secret.get_secret_value(),
        None,
        actor,
    )
    return {"key": key}


@router.patch("/admin/llm-keys/{credential_id}")
def update_llm_key(credential_id: str, payload: UpdateLlmKeyRequest, request: Request):
    actor = require_admin_actor(request)
    key = llm_key_service.update(
        credential_id,
        name=payload.name,
        priority=payload.priority,
        enabled=payload.enabled,
        actor=actor,
    )
    return {"key": key}


@router.post("/admin/llm-keys/{credential_id}/test")
def test_llm_key(credential_id: str, request: Request):
    actor = require_admin_actor(request)
    return llm_service.test_gemini_credential(credential_id, actor)


@router.delete("/admin/llm-keys/{credential_id}")
def delete_llm_key(credential_id: str, payload: DeleteLlmKeyRequest, request: Request):
    actor = require_admin_actor(request)
    llm_key_service.delete(credential_id, payload.confirm_name, actor)
    return {"deleted": True}


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
            payload.group_participant_ids,
            payload.coordinator_ids,
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
        session = session_service.get(session_id)
        try:
            extraction, source, token_usage = llm_service.extract_availability(
                payload.message,
                session.channel_config.workday_start_hour,
                session.channel_config.workday_end_hour,
            )
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
        return {
            "session": session_service.confirm(
                session_id,
                payload.option_id,
                source="panel",
                expected_proposal_revision=payload.expected_proposal_revision,
            )
        }


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
            payload.workday_start_hour,
            payload.workday_end_hour,
            payload.group_jid,
            payload.group_name,
            payload.group_participant_count,
            payload.group_participant_ids,
            payload.coordinator_ids,
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
            sender_id=payload.sender_id,
            sender_aliases=payload.sender_aliases,
            mentioned_jids=payload.mentioned_jids,
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
    messages = [
        (
            message.sender,
            message.text,
            message.sender_id,
            message.sender_aliases,
            message.mentioned_jids,
        )
        for message in payload.messages
    ]
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
        extraction, source, token_usage = llm_service.extract_channel_availability(
            pending_messages,
            session.channel_config.workday_start_hour,
            session.channel_config.workday_end_hour,
        )
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

    command_name = command["name"]
    # Los comandos de coordinacion del grupo son colaborativos: cualquier
    # integrante puede confirmar, cancelar, corregir personas o reiniciar. La
    # administracion web y las llaves LLM siguen protegidas por Caddy.
    # Reiniciar descarta deliberadamente el estado activo. No se deben fusionar
    # antes los mensajes pendientes: pertenecen a la ronda que el usuario acaba
    # de cerrar.
    if command_name == "reset":
        session = session_service.reset_channel_context(session_id, external_id=external_id)
        return ChannelMessageResponse(
            session=session,
            invoked=True,
            llm_source="channel_command",
            agent_reply=session.last_agent_reply,
            agent_reply_format="text",
            elapsed_ms=elapsed_ms(started),
            duplicate=duplicate,
        )

    # Fusiona la disponibilidad pendiente ANTES de ejecutar el comando. Sin esto,
    # los mensajes escritos desde la ultima respuesta del agente quedarian fuera
    # (la respuesta del comando resetea los pendientes) y ademas "confirma" o
    # "resumen" operarian sobre datos desactualizados.
    token_usage = None
    pending_messages = session_service.pending_channel_messages(session)
    if pending_messages:
        try:
            extraction, source, token_usage = llm_service.extract_channel_availability(
                pending_messages,
                session.channel_config.workday_start_hour,
                session.channel_config.workday_end_hour,
            )
        except LlmUnavailableError as error:
            raise_llm_http_error(error)
        if (
            extraction.participants
            or extraction.removals
            or extraction.implied
            or extraction.replacements
            or extraction.quality_flags
        ):
            session = session_service.merge_extraction(
                session_id,
                extraction,
                original_message="Invocacion desde canal simulado.",
                source=source,
                token_usage=token_usage,
            )

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
        expected_revision = command.get("proposal_revision")
        blockers = confirmation_blockers(session)
        if blockers:
            reply = (
                "*Coordina*\n\n"
                "No puedo confirmar todavia:\n"
                + "\n".join(f"- {item}" for item in blockers)
                + "\n\n"
                + build_channel_reply(session)
            )
        elif session.channel_config.group_jid and expected_revision is None:
            reply = (
                "*Coordina*\n\n"
                "Falta la revision de la propuesta. Confirma usando el codigo actual: "
                f"*@coordina confirmar 1 R{session.proposal_revision}*."
            )
        elif expected_revision is not None and expected_revision != session.proposal_revision:
            reply = (
                "*Coordina*\n\n"
                f"La propuesta cambio: recibí R{expected_revision} y la actual es "
                f"R{session.proposal_revision}. Estas son las opciones vigentes:\n\n"
                + build_channel_reply(session)
            )
        elif option_index < 1 or option_index > len(session.options):
            option_label = command.get("option_label", option_index)
            reply = (
                "*Coordina*\n\n"
                f"No encuentro la opcion {option_label}. Pide *@coordina* para ver las opciones actuales."
            )
        else:
            option = session.options[option_index - 1]
            try:
                session = session_service.confirm(
                    session_id,
                    option.id,
                    confirmed_by=last_invoking_sender(session),
                    source="whatsapp",
                    external_id=external_id,
                    expected_proposal_revision=expected_revision,
                )
            except HTTPException as error:
                reply = f"*Coordina*\n\nNo puedo confirmar todavia. {error.detail}"
            else:
                reply = build_confirmed_channel_reply(session)
                # Adjunta el evento .ics solo si la confirmacion realmente se
                # completo. Es opcional: si falla, el texto sigue saliendo.
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
                if not session.selected_option:
                    raise ValueError("confirmacion bloqueada")
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
        if command.get("requires_mention"):
            reply = (
                "*Coordina*\n\n"
                "Para quitar a otra persona debes seleccionarla como una mención real de WhatsApp. "
                f"Escribe, por ejemplo: *@coordina quitar @{participant_name}*."
            )
        else:
            try:
                session = session_service.remove_participant(
                    session_id,
                    participant_name,
                    external_id=external_id,
                    target_external_id=command.get("target_external_id"),
                )
            except HTTPException:
                reply = (
                    "*Coordina*\n\n"
                    f"No encontre a {participant_name}. Revisa la mención o escribe *@coordina exportar* para ver la lista."
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


def require_admin_actor(request: Request) -> str:
    actor = request.headers.get("X-Coordina-Admin", "").strip()
    if settings.admin_proxy_header_required and not actor:
        raise HTTPException(
            status_code=403,
            detail="Esta operacion requiere autenticacion administrativa.",
        )
    return actor or "local-admin"


def elapsed_ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)


def safe_filename(value: str) -> str:
    safe = value.strip().lower()
    safe = "".join(char if char.isalnum() else "-" for char in safe).strip("-")
    return safe or "coordina"
