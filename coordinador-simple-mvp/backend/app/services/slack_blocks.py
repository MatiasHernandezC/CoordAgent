"""Block Kit: botones para confirmar una opcion sin escribir '@coordina
confirmar N'. El texto plano (build_channel_reply) sigue mandandose igual,
como fallback y como notificacion; los blocks son un agregado opcional.
"""
from __future__ import annotations

import json

from app.schemas import Session
from app.services.calendar_export import format_short_date, option_event_date


def build_confirm_option_blocks(session: Session) -> list[dict] | None:
    """Un boton por opcion vigente. None si no hay nada que confirmar."""
    if not session.options or session.selected_option is not None:
        return None

    elements = []
    for index, option in enumerate(session.options, start=1):
        event_date = format_short_date(option_event_date(option))
        label = f"Opcion {index}: {option.day.capitalize()} {event_date} {option.start}-{option.end}"
        value = json.dumps({"sid": session.id, "oid": option.id, "rev": session.proposal_revision})
        elements.append(
            {
                "type": "button",
                "action_id": f"confirm_option:{option.id}",
                "text": {"type": "plain_text", "text": label[:75], "emoji": True},
                "value": value,
                "style": "primary" if index == 1 else None,
            }
        )
    # Slack rechaza "style": null explicito.
    for element in elements:
        if element["style"] is None:
            del element["style"]

    return [{"type": "actions", "elements": elements}]
