"""Events API de Slack: mismo pipeline de canal que WhatsApp, sin logica duplicada."""

from __future__ import annotations

import base64
import logging
import re
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.bff.routes import invoke_channel_if_needed
from app.services.session_service import session_service
from app.services.slack_channel import (
    SlackApiError,
    SlackConfigError,
    channel_group_jid,
    post_message,
    resolve_bot_user_id,
    resolve_channel_name,
    resolve_display_name,
    upload_file,
    verify_slack_signature,
)
from app.settings import settings

router = APIRouter()
logger = logging.getLogger("app.slack")


@router.post("/events")
async def slack_events(request: Request):
    if not settings.slack_configured:
        raise HTTPException(status_code=503, detail="Canal Slack no configurado.")

    body = await request.body()
    payload = await request.json()

    if payload.get("type") == "url_verification":
        # Slack exige devolver el challenge en texto plano para validar la URL
        # del Events API antes de aceptar suscripciones reales.
        return {"challenge": payload.get("challenge", "")}

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    try:
        valid = verify_slack_signature(timestamp=timestamp, body=body, signature=signature)
    except SlackConfigError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not valid:
        raise HTTPException(status_code=401, detail="Firma de Slack invalida.")

    if payload.get("type") != "event_callback":
        return {"ok": True}

    if request.headers.get("X-Slack-Retry-Num"):
        # Slack reintenta si no respondemos en ~3s. El mensaje original ya se
        # está procesando o se proceso; no repetimos el trabajo ni reenviamos
        # la respuesta para no duplicar mensajes en el canal.
        return {"ok": True}

    event = payload.get("event") or {}
    bot_user_id = _extract_bot_user_id(payload)
    await run_in_threadpool(_handle_event, event, bot_user_id)
    return {"ok": True}


def _extract_bot_user_id(payload: dict) -> str | None:
    for authorization in payload.get("authorizations") or []:
        if authorization.get("is_bot") and authorization.get("user_id"):
            return authorization["user_id"]
    return resolve_bot_user_id()


def _normalize_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Slack autocompleta @coordina a <@BOT_ID>; lo devolvemos a texto plano."""
    if not bot_user_id:
        return text
    pattern = re.compile(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>")
    return pattern.sub(settings.slack_trigger_word, text)


def _handle_event(event: dict, bot_user_id: str | None = None) -> None:
    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") == "im":
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    text = _normalize_bot_mention((event.get("text") or "").strip(), bot_user_id)
    ts = event.get("ts", "")
    logger.info("slack_event_received channel=%s user=%s ts=%s text=%r", channel_id, user_id, ts, text[:200])
    if not channel_id or not text or not ts:
        logger.info("slack_event_skipped channel=%s ts=%s reason=missing_fields", channel_id, ts)
        return

    try:
        with session_service.group_lock(channel_group_jid(channel_id)):
            session = session_service.resolve_channel_group(
                channel_group_jid(channel_id),
                resolve_channel_name(channel_id),
                None,
                [],
                [],
                settings.slack_trigger_word,
                channel_label="Slack",
            )

        sender = resolve_display_name(user_id)
        with session_service.session_lock(session.id):
            session, invoked = session_service.add_channel_message(
                session.id,
                sender,
                text,
                external_id=ts,
                sender_id=user_id,
            )
            logger.info("slack_message_stored session=%s sender=%s invoked=%s", session.id, sender, invoked)
            if not invoked:
                return
            result = invoke_channel_if_needed(
                session.id,
                session,
                invoked,
                perf_counter(),
                external_id=ts,
            )
        logger.info(
            "slack_invocation_result session=%s llm_source=%s reply_len=%s",
            session.id,
            result.llm_source,
            len(result.agent_reply or ""),
        )
        _deliver(channel_id, result)
    except Exception:  # nunca debe tumbar el handler del webhook
        logger.exception("slack_event_processing_failed channel=%s ts=%s", channel_id, ts)


def _deliver(channel_id: str, result) -> None:
    try:
        if result.agent_reply:
            post_message(channel_id, result.agent_reply)
        if result.agent_reply_image:
            upload_file(
                channel_id,
                "coordina-calendario.png",
                base64.b64decode(result.agent_reply_image),
                initial_comment=result.agent_reply_caption or "",
            )
        if result.agent_reply_document:
            upload_file(
                channel_id,
                result.agent_reply_document_name or "evento.ics",
                base64.b64decode(result.agent_reply_document),
            )
    except SlackApiError as error:
        logger.error("slack_delivery_failed channel=%s error=%s", channel_id, error)
