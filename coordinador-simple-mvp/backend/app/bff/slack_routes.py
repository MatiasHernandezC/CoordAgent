"""Events API de Slack: mismo pipeline de canal que WhatsApp, sin logica duplicada."""

from __future__ import annotations

import base64
import json
import logging
import re
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.bff.routes import execute_confirm_command, invoke_channel_if_needed
from app.services.session_service import session_service
from app.services.slack_blocks import build_confirm_option_blocks
from app.services.slack_channel import (
    SlackApiError,
    SlackConfigError,
    channel_group_jid,
    post_message,
    resolve_channel_roster,
    resolve_bot_user_id,
    resolve_channel_name,
    resolve_display_name,
    respond_to_url,
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


@router.post("/interactions")
async def slack_interactions(request: Request):
    """Click en un boton de Block Kit (confirmar opcion). Slack manda esto
    como application/x-www-form-urlencoded con un campo 'payload' (JSON
    codificado), no como JSON crudo como /events."""
    if not settings.slack_configured:
        raise HTTPException(status_code=503, detail="Canal Slack no configurado.")

    body = await request.body()  # bytes crudos para la firma, ANTES de leer el form
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    try:
        valid = verify_slack_signature(timestamp=timestamp, body=body, signature=signature)
    except SlackConfigError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not valid:
        raise HTTPException(status_code=401, detail="Firma de Slack invalida.")

    form = await request.form()
    try:
        payload = json.loads(form["payload"])
    except (KeyError, ValueError):
        return {"ok": True}

    if payload.get("type") != "block_actions":
        return {"ok": True}

    await run_in_threadpool(_handle_block_action, payload)
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
    event_type = event.get("type")
    if event_type == "member_joined_channel":
        _sync_joined_channel(event.get("channel", ""), bot_user_id)
        return
    if event_type != "message" or event.get("bot_id") or event.get("subtype"):
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
        roster = resolve_channel_roster(channel_id, bot_user_id)
        with session_service.group_lock(channel_group_jid(channel_id)):
            session = session_service.resolve_channel_group(
                channel_group_jid(channel_id),
                resolve_channel_name(channel_id),
                (roster or {}).get("participant_count"),
                (roster or {}).get("participant_ids", []),
                (roster or {}).get("coordinator_ids", []),
                settings.slack_trigger_word,
                channel_label="Slack",
                participant_roster=(roster or {}).get("participant_roster"),
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


def _sync_joined_channel(channel_id: str, bot_user_id: str | None) -> None:
    if not channel_id:
        return
    try:
        roster = resolve_channel_roster(channel_id, bot_user_id)
        if not roster:
            return
        with session_service.group_lock(channel_group_jid(channel_id)):
            session_service.resolve_channel_group(
                channel_group_jid(channel_id),
                resolve_channel_name(channel_id),
                roster["participant_count"],
                roster["participant_ids"],
                roster["coordinator_ids"],
                settings.slack_trigger_word,
                channel_label="Slack",
                participant_roster=roster["participant_roster"],
            )
        logger.info("slack_channel_roster_synced channel=%s participants=%s", channel_id, roster["participant_count"])
    except Exception:
        logger.exception("slack_joined_channel_sync_failed channel=%s", channel_id)


def _deliver(channel_id: str, result) -> None:
    try:
        if result.agent_reply:
            blocks = build_confirm_option_blocks(result.session)
            post_message(channel_id, result.agent_reply, blocks)
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


def _handle_block_action(payload: dict) -> None:
    actions = payload.get("actions") or []
    if not actions:
        return
    response_url = payload.get("response_url")
    channel_id = (payload.get("channel") or {}).get("id", "")
    user_id = (payload.get("user") or {}).get("id", "")

    try:
        value = json.loads(actions[0].get("value") or "{}")
        session_id = value["sid"]
        option_id = value["oid"]
        expected_revision = value.get("rev")
    except (ValueError, KeyError):
        logger.warning("slack_block_action_bad_value payload=%r", actions[0] if actions else None)
        return

    try:
        sender = resolve_display_name(user_id)
        with session_service.session_lock(session_id):
            _session, reply, document, document_name, document_mimetype = execute_confirm_command(
                session_id,
                option_id=option_id,
                confirmed_by=sender,
                source="slack",
                external_id=actions[0].get("action_ts"),
                expected_revision=expected_revision,
            )
        logger.info("slack_block_action_confirm session=%s option=%s", session_id, option_id)
        if response_url:
            respond_to_url(response_url, reply)
        elif channel_id and reply:
            post_message(channel_id, reply)
        if document and document_name and channel_id:
            upload_file(channel_id, document_name, base64.b64decode(document))
    except Exception:  # nunca debe tumbar el handler del webhook
        logger.exception("slack_block_action_failed session=%s channel=%s", session_id, channel_id)
