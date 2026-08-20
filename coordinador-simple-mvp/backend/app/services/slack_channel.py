"""Firma HMAC y llamadas a la Web API de Slack.

La invocacion acepta @coordina como texto plano o como mencion real del bot
(<@BOT_ID>): Slack autocompleta el nombre apenas coincide, asi que exigir
solo texto plano rompe la UX por defecto.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import requests

from app.schemas import ParticipantRosterEntry
from app.settings import settings

logger = logging.getLogger("app.slack")

SLACK_API_BASE = "https://slack.com/api"
GROUP_JID_PREFIX = "slack:"

_bot_user_id_cache: str | None = None


class SlackConfigError(RuntimeError):
    pass


class SlackApiError(RuntimeError):
    pass


def channel_group_jid(channel_id: str) -> str:
    return f"{GROUP_JID_PREFIX}{channel_id}"


def channel_id_from_group_jid(group_jid: str) -> str | None:
    if not group_jid.startswith(GROUP_JID_PREFIX):
        return None
    return group_jid[len(GROUP_JID_PREFIX):]


def verify_slack_signature(*, timestamp: str, body: bytes, signature: str) -> bool:
    """Valida X-Slack-Signature siguiendo el algoritmo oficial de Slack.

    Rechaza timestamps viejos para evitar ataques de replay con una firma
    capturada anteriormente.
    """
    if not settings.slack_signing_secret:
        raise SlackConfigError("Falta SLACK_SIGNING_SECRET.")
    if not timestamp or not signature:
        return False
    try:
        request_time = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - request_time) > settings.slack_request_max_age_seconds:
        return False

    base = f"v0:{timestamp}:".encode("utf-8") + body
    computed = "v0=" + hmac.new(
        settings.slack_signing_secret.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def _headers() -> dict:
    if not settings.slack_bot_token:
        raise SlackConfigError("Falta SLACK_BOT_TOKEN.")
    return {"Authorization": f"Bearer {settings.slack_bot_token}"}


def post_message(channel: str, text: str, blocks: list[dict] | None = None) -> None:
    payload: dict = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    response = requests.post(
        f"{SLACK_API_BASE}/chat.postMessage",
        headers=_headers(),
        json=payload,
        timeout=20,
    )
    _raise_for_slack_error(response)


def respond_to_url(response_url: str, text: str, *, replace_original: bool = True) -> None:
    """Responde al webhook de un solo uso de un click de Block Kit. No lleva
    auth propio (la URL misma es el secreto de corta duracion que da Slack)."""
    try:
        response = requests.post(
            response_url,
            json={"text": text, "replace_original": replace_original},
            timeout=20,
        )
        if response.status_code >= 400:
            logger.error("slack_response_url_failed status=%s", response.status_code)
    except requests.RequestException as error:
        logger.error("slack_response_url_failed error=%s", error)


def upload_file(
    channel: str,
    filename: str,
    content: bytes,
    *,
    initial_comment: str | None = None,
) -> None:
    # files.upload esta deprecado; Slack ahora pide 3 pasos: reservar URL,
    # subir el archivo, confirmar (eso ultimo lo comparte en el canal).
    upload_url, file_id = _get_upload_url(filename, len(content))
    _put_file_content(upload_url, filename, content)
    _complete_upload(file_id, filename, channel, initial_comment)


def _get_upload_url(filename: str, length: int) -> tuple[str, str]:
    response = requests.post(
        f"{SLACK_API_BASE}/files.getUploadURLExternal",
        headers=_headers(),
        data={"filename": filename, "length": length},
        timeout=20,
    )
    payload = _payload_or_raise(response, "No fue posible reservar la subida en Slack.")
    return payload["upload_url"], payload["file_id"]


def _put_file_content(upload_url: str, filename: str, content: bytes) -> None:
    response = requests.post(upload_url, files={"file": (filename, content)}, timeout=30)
    if response.status_code >= 400:
        raise SlackApiError(f"Slack rechazo la subida del archivo (status {response.status_code}).")


def _complete_upload(file_id: str, filename: str, channel: str, initial_comment: str | None) -> None:
    body = {"files": [{"id": file_id, "title": filename}], "channel_id": channel}
    if initial_comment:
        body["initial_comment"] = initial_comment
    response = requests.post(
        f"{SLACK_API_BASE}/files.completeUploadExternal",
        headers=_headers(),
        json=body,
        timeout=20,
    )
    _raise_for_slack_error(response)


def _payload_or_raise(response: requests.Response, message: str) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise SlackApiError(message) from error
    if not payload.get("ok"):
        raise SlackApiError(f"Slack API error: {payload.get('error', 'unknown')}")
    return payload


def resolve_display_name(user_id: str) -> str:
    """Nombre visible del usuario de Slack; si falla, se conserva el ID crudo."""
    if not user_id:
        return "Participante"
    try:
        response = requests.get(
            f"{SLACK_API_BASE}/users.info",
            headers=_headers(),
            params={"user": user_id},
            timeout=10,
        )
        payload = response.json()
        if not payload.get("ok"):
            return user_id
        profile = payload.get("user", {})
        name = (
            profile.get("profile", {}).get("display_name")
            or profile.get("profile", {}).get("real_name")
            or profile.get("real_name")
            or profile.get("name")
        )
        return (name or user_id).strip() or user_id
    except (requests.RequestException, ValueError) as error:
        logger.warning("slack_users_info_failed user=%s error=%s", user_id, error)
        return user_id


def resolve_bot_user_id() -> str | None:
    """ID del bot, cacheado en memoria. Se usa para reconocer <@BOT_ID> como invocacion."""
    global _bot_user_id_cache
    if _bot_user_id_cache:
        return _bot_user_id_cache
    try:
        response = requests.post(f"{SLACK_API_BASE}/auth.test", headers=_headers(), timeout=10)
        payload = response.json()
        if payload.get("ok"):
            _bot_user_id_cache = payload.get("user_id")
    except (requests.RequestException, ValueError, SlackConfigError) as error:
        logger.warning("slack_auth_test_failed error=%s", error)
    return _bot_user_id_cache


def resolve_channel_name(channel_id: str) -> str:
    try:
        response = requests.get(
            f"{SLACK_API_BASE}/conversations.info",
            headers=_headers(),
            params={"channel": channel_id},
            timeout=10,
        )
        payload = response.json()
        if not payload.get("ok"):
            return channel_id
        name = payload.get("channel", {}).get("name")
        return f"#{name}" if name else channel_id
    except (requests.RequestException, ValueError) as error:
        logger.warning("slack_conversations_info_failed channel=%s error=%s", channel_id, error)
        return channel_id


def resolve_channel_roster(channel_id: str, bot_user_id: str | None = None) -> dict | None:
    """Obtiene los miembros humanos de un canal para hidratarlo sin esperar mensajes."""
    if not channel_id:
        return None
    try:
        members: list[str] = []
        cursor = ""
        while True:
            response = requests.get(
                f"{SLACK_API_BASE}/conversations.members",
                headers=_headers(),
                params={"channel": channel_id, "limit": 200, **({"cursor": cursor} if cursor else {})},
                timeout=20,
            )
            payload = _payload_or_raise(response, "No fue posible leer los miembros del canal.")
            members.extend(str(member) for member in payload.get("members") or [])
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        human_ids = [member for member in dict.fromkeys(members) if member and member != bot_user_id]
        return {
            "participant_count": len(human_ids),
            "participant_ids": human_ids,
            "coordinator_ids": [],
            "participant_roster": [
                ParticipantRosterEntry(id=member, name=resolve_display_name(member)) for member in human_ids
            ],
        }
    except (requests.RequestException, ValueError, SlackApiError, SlackConfigError) as error:
        logger.warning("slack_channel_roster_failed channel=%s error=%s", channel_id, error)
        return None


def _raise_for_slack_error(response: requests.Response) -> None:
    try:
        payload = response.json()
    except ValueError as error:
        raise SlackApiError(f"Respuesta invalida de Slack: {response.status_code}") from error
    if not payload.get("ok"):
        raise SlackApiError(f"Slack API error: {payload.get('error', 'unknown')}")
