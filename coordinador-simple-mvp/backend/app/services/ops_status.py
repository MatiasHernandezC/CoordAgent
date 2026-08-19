import json
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from app.settings import settings


def fetch_gateway_status() -> dict:
    """Consulta el endpoint privado del gateway y normaliza una caida como estado."""
    checked_at = datetime.now(timezone.utc).isoformat()
    request = Request(settings.gateway_status_url, headers={"Accept": "application/json"})

    try:
        with urlopen(request, timeout=settings.gateway_status_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {
            "ok": False,
            "connected": False,
            "state": "unavailable",
            "checked_at": checked_at,
        }

    if not isinstance(payload, dict):
        payload = {}

    return {
        "ok": bool(payload.get("ok")),
        "connected": bool(payload.get("connected")),
        "state": str(payload.get("state") or "unknown"),
        "known_groups": int(payload.get("knownGroups") or 0),
        "reconnect_attempt": int(payload.get("reconnectAttempt") or 0),
        "pending_messages": int(payload.get("pendingMessages") or 0),
        "dead_letter_messages": int(payload.get("deadLetterMessages") or 0),
        "queue_overflow_count": int(payload.get("queueOverflowCount") or 0),
        "bot_phone_number": payload.get("botPhoneNumber"),
        "last_queue_error_at": payload.get("lastQueueErrorAt"),
        "last_connected_at": payload.get("lastConnectedAt"),
        "last_disconnected_at": payload.get("lastDisconnectedAt"),
        "checked_at": checked_at,
    }
