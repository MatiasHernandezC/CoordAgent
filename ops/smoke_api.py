#!/usr/bin/env python3
"""Prueba HTTP E2E aislada del flujo WhatsApp -> confirmacion -> calendario."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def request_json(base_url: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} respondio {error.code}: {detail[:300]}") from error
    except URLError as error:
        raise RuntimeError(f"{method} {path} no conecto: {error.reason}") from error


def validate_ics(ics: str) -> None:
    assert ics.startswith("BEGIN:VCALENDAR\r\n"), "ICS sin cabecera o sin CRLF"
    assert "BEGIN:VEVENT\r\n" in ics
    assert "DTSTART;TZID=" in ics
    assert "DTEND;TZID=" in ics
    assert ics.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n")), "linea ICS supera 75 bytes"


def run(base_url: str) -> dict:
    ready = request_json(base_url, "/ready")
    assert ready.get("ok") is True

    created = request_json(base_url, "/api/sessions", "POST", {"title": "Smoke E2E aislado"})
    session_id = created["session"]["id"]

    proposed = request_json(
        base_url,
        f"/api/sessions/{session_id}/channel/batch",
        "POST",
        {
            "messages": [
                {"sender": "Ana", "text": "yo puedo lunes de 15 a 18"},
                {"sender": "Belen", "text": "yo puedo lunes desde las 16"},
                {"sender": "Ana", "text": "@coordina"},
            ]
        },
    )
    assert proposed["invoked"] is True
    assert proposed["session"]["status"] == "calculated"
    assert proposed["session"]["options"]

    confirmed = request_json(
        base_url,
        f"/api/sessions/{session_id}/channel/messages",
        "POST",
        {"sender": "Ana", "text": "@coordina confirma 1"},
    )
    assert confirmed["session"]["status"] == "confirmed"
    assert confirmed["session"]["selected_event_date"]
    assert confirmed["agent_reply_document_mimetype"] == "text/calendar"
    document_ics = base64.b64decode(confirmed["agent_reply_document"]).decode("utf-8")
    validate_ics(document_ics)

    exported = request_json(base_url, f"/api/sessions/{session_id}/calendar")
    validate_ics(exported["text"])
    assert _line(document_ics, "DTSTART") == _line(exported["text"], "DTSTART")
    assert _line(document_ics, "UID") == _line(exported["text"], "UID")

    summary = request_json(
        base_url,
        f"/api/sessions/{session_id}/channel/messages",
        "POST",
        {"sender": "Belen", "text": "@coordina resumen"},
    )
    assert summary["session"]["status"] == "confirmed"
    assert "Decision confirmada" in (summary.get("agent_reply") or "")

    cancelled = request_json(base_url, f"/api/sessions/{session_id}/cancel-decision", "POST", {})
    assert cancelled["session"]["status"] == "calculated"
    assert cancelled["session"]["selected_event_date"] is None

    archived = request_json(base_url, f"/api/sessions/{session_id}/archive", "POST", {})
    assert archived["session"]["archived_at"]

    return {
        "ok": True,
        "session_id": session_id,
        "participants": len(proposed["session"]["participants"]),
        "options": len(proposed["session"]["options"]),
        "calendar_filename": exported["filename"],
        "final_status": archived["session"]["status"],
        "archived": True,
    }


def _line(ics: str, prefix: str) -> str:
    unfolded = ics.replace("\r\n ", "")
    return next(line for line in unfolded.split("\r\n") if line.startswith(prefix))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permite escribir en un host no loopback. No se usa en la validacion normal.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hostname = (urlparse(args.base_url).hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        print("ERROR: el smoke test solo escribe en loopback salvo --allow-remote", file=sys.stderr)
        return 2

    try:
        result = run(args.base_url)
    except (AssertionError, RuntimeError, KeyError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
