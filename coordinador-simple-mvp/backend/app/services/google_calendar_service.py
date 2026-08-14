"""OAuth de Google Calendar a nivel administrador (una cuenta, no por participante).

Refresh token cifrado con la misma llave maestra de las llaves Gemini. Sin
cuenta conectada, la creacion de eventos es un no-op y sigue el fallback de
siempre (link + .ics). REST directo con requests, sin google-api-python-client.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from app.schemas import Session
from app.services.calendar_export import session_event_datetimes
from app.services.credential_crypto import (
    CredentialEncryptionError,
    decode_master_key,
    decrypt_secret,
    encrypt_secret,
)
from app.settings import settings

if settings.db_backend == "json":
    from app.storage.json_google_calendar_repository import repository
else:
    from app.storage.postgres_google_calendar_repository import repository

logger = logging.getLogger("app.google_calendar")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"
EVENTS_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
# AAD del cifrado (ver credential_crypto._aad); el "gemini-key" del modulo
# compartido es solo un nombre historico, no describe este contenido.
CREDENTIAL_ID = "google-calendar:default"
STATE_TTL_SECONDS = 600


class GoogleCalendarConfigError(RuntimeError):
    pass


class GoogleCalendarApiError(RuntimeError):
    pass


class GoogleCalendarService:
    def __init__(self) -> None:
        self._pending_states: dict[str, float] = {}

    def management_enabled(self) -> bool:
        if not settings.google_oauth_configured:
            return False
        try:
            decode_master_key(settings.llm_keys_master_key)
        except CredentialEncryptionError:
            return False
        return True

    def status(self) -> dict:
        record = repository.get()
        return {
            "configured": settings.google_oauth_configured,
            "connected": bool(record),
            "account_email": record.get("account_email") if record else None,
            "connected_at": record.get("connected_at") if record else None,
        }

    def is_connected(self) -> bool:
        return bool(repository.get())

    def authorization_url(self) -> str:
        self._require_management()
        state = secrets.token_urlsafe(24)
        self._pending_states[state] = time.time() + STATE_TTL_SECONDS
        self._prune_states()
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{AUTH_ENDPOINT}?{urlencode(params)}"

    def handle_callback(self, code: str, state: str) -> dict:
        self._require_management()
        if not self._consume_state(state):
            raise GoogleCalendarConfigError("El enlace de autorizacion expiro o ya fue usado. Intenta conectar de nuevo.")

        response = requests.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        payload = _json_or_raise(response, "No fue posible canjear el codigo de Google.")
        refresh_token = payload.get("refresh_token")
        access_token = payload.get("access_token")
        if not refresh_token:
            raise GoogleCalendarApiError(
                "Google no devolvio un refresh token. Revoca el acceso previo en "
                "myaccount.google.com/permissions y vuelve a conectar."
            )

        account_email = self._fetch_account_email(access_token) if access_token else None
        record = {
            "account_email": account_email,
            "encrypted_refresh_token": encrypt_secret(refresh_token, settings.llm_keys_master_key, CREDENTIAL_ID),
            "connected_at": _now_iso(),
        }
        repository.save(record)
        return self.status()

    def disconnect(self) -> None:
        repository.clear()

    def create_event(self, session: Session) -> dict | None:
        """Best-effort: devuelve {id, htmlLink} o None si no hay cuenta conectada."""
        record = repository.get()
        if not record:
            return None
        if session.selected_option is None:
            return None

        access_token = self._refresh_access_token(record)
        start_at, end_at = session_event_datetimes(session)
        available = ", ".join(session.selected_option.available_participants) or "por confirmar"
        body = {
            "summary": session.channel_config.group_name or session.title or "Reunion",
            "description": f"Coordinado con Coordina. Asisten: {available}.",
            "start": {"dateTime": start_at.isoformat(), "timeZone": start_at.tzinfo.key if hasattr(start_at.tzinfo, "key") else str(start_at.tzinfo)},
            "end": {"dateTime": end_at.isoformat(), "timeZone": end_at.tzinfo.key if hasattr(end_at.tzinfo, "key") else str(end_at.tzinfo)},
        }
        response = requests.post(
            EVENTS_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
            timeout=20,
        )
        payload = _json_or_raise(response, "Google Calendar rechazo la creacion del evento.")
        return {"id": payload.get("id"), "htmlLink": payload.get("htmlLink")}

    def _refresh_access_token(self, record: dict) -> str:
        refresh_token = decrypt_secret(
            record["encrypted_refresh_token"],
            settings.llm_keys_master_key,
            CREDENTIAL_ID,
        )
        response = requests.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        payload = _json_or_raise(response, "No fue posible renovar el acceso a Google Calendar.")
        access_token = payload.get("access_token")
        if not access_token:
            raise GoogleCalendarApiError("Google no devolvio un access token al renovar.")
        return access_token

    def _fetch_account_email(self, access_token: str) -> str | None:
        try:
            response = requests.get(
                USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            payload = response.json()
            return payload.get("email")
        except (requests.RequestException, ValueError):
            return None

    def _consume_state(self, state: str) -> bool:
        self._prune_states()
        return self._pending_states.pop(state, None) is not None

    def _prune_states(self) -> None:
        now = time.time()
        expired = [key for key, expires_at in self._pending_states.items() if expires_at < now]
        for key in expired:
            self._pending_states.pop(key, None)

    def _require_management(self) -> None:
        if not settings.google_oauth_configured:
            raise GoogleCalendarConfigError(
                "Configura GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET y GOOGLE_OAUTH_REDIRECT_URI."
            )
        try:
            decode_master_key(settings.llm_keys_master_key)
        except CredentialEncryptionError as error:
            raise GoogleCalendarConfigError(str(error)) from error


def _json_or_raise(response: requests.Response, message: str) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise GoogleCalendarApiError(message) from error
    if response.status_code >= 400 or payload.get("error"):
        detail = payload.get("error_description") or payload.get("error") or message
        raise GoogleCalendarApiError(str(detail))
    return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


google_calendar_service = GoogleCalendarService()
