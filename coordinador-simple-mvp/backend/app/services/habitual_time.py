"""Horario habitual de una sesion ("a la hora de siempre").

Principio (igual que el resto del pipeline):
  LLM puede interpretar, pero Python ancla de forma determinista.

Cuando el historial de decisiones confirmadas establece un patron de
(day, start, end), ese slot se persiste en ``session.habitual_slot``. Si un
mensaje dice "a la hora de siempre" (u otras expresiones equivalentes), la frase
se reescribe con el slot literal ("el martes de 10:00 a 11:00") ANTES de entrar
al extractor, de modo que tanto el LLM como el fallback por reglas reciben un
dia y unas horas reales y el grounding temporal las acepta porque ya estan "en
el mensaje".
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from app.settings import settings

if TYPE_CHECKING:
    from app.schemas import Session, TimeSlot

# Expresiones que piden el horario de costumbre. Normalizado a minusculas sin
# tildes antes de evaluar (ver _normalize). Solo se activan con contexto de
# horario, nunca como palabra suelta ("siempre" ocupa espacio en el dialecto).
HABITUAL_PHRASE_PATTERNS = [
    r"a\s+las?\s+hora\s+de\s+siempre",
    r"a\s+la\s+hora\s+de\s+siempre",
    r"la\s+hora\s+de\s+siempre",
    r"a\s+las\s+de\s+siempre",
    r"a\s+la\s+misma\s+hora",
    r"a\s+las?\s+hora\s+(?:de\s+costumbre|de\s+habitual|habitual)",
    r"hora\s+(?:de\s+costumbre|habitual)",
    r"(?:como|de|a\s+la\s+hora)\s+siempre",
]

_HABITUAL_RE = re.compile(
    "|".join(f"(?:{pattern})" for pattern in HABITUAL_PHRASE_PATTERNS),
    re.IGNORECASE,
)


def detect_habitual_phrase(text: str | None) -> bool:
    """True si el texto pide explicitamente el horario de costumbre."""
    if not text or not text.strip():
        return False
    return bool(_HABITUAL_RE.search(text))


def anchor_habitual_phrase(text: str, slot: "TimeSlot | None") -> str:
    """Reemplaza las frases 'de siempre' por el slot literal de la sesion.

    Si no hay slot habitual (o no hay frase), devuelve el texto sin cambios.
    """
    if slot is None:
        return text
    if not detect_habitual_phrase(text):
        return text

    replacement = f"el {slot.day} de {slot.start} a {slot.end}"
    return _HABITUAL_RE.sub(replacement, text)


def compute_habitual_slot(session: "Session") -> "TimeSlot | None":
    """Slot modal (day, start, end) entre las decisiones confirmadas.

    - Solo se cuentan decisiones con opcion valida.
    - Se ignora ``week_offset``: el "horario de siempre" repite dia y ventana,
      no una semana concreta.
    - Empate: gana la decision mas reciente (orden cronologico del historial).
    - Requiere al menos ``settings.habitual_min_decisions`` confirmaciones.
    """
    from app.schemas import TimeSlot

    # Tras "reinicia historial" solo cuentan las decisiones de la ronda actual:
    # las anteriores se conservan para auditoria pero no definen la "hora de
    # siempre" de la coordinacion vigente.
    history = session.decision_history
    start = session.habitual_history_index or 0
    history = history[start:] if 0 <= start <= len(history) else history

    counts: dict[tuple, int] = defaultdict(int)
    last_seen: dict[tuple, int] = {}
    last_slots: dict[tuple, TimeSlot] = {}

    for index, record in enumerate(history):
        option = record.option
        if not option or not option.day or not option.start or not option.end:
            continue
        key = (option.day, option.start, option.end)
        counts[key] += 1
        last_seen[key] = index
        last_slots[key] = option

    if not counts:
        return None
    if max(counts.values()) < settings.habitual_min_decisions:
        return None

    best_key = max(
        counts,
        key=lambda key: (counts[key], last_seen[key]),
    )
    option = last_slots[best_key]
    return TimeSlot(
        day=option.day,
        start=option.start,
        end=option.end,
    )


def habitual_slot_label(slot: "TimeSlot | None") -> str | None:
    """Etiqueta humana corta para el bloque RAG y el preview de trazabilidad."""
    if slot is None:
        return None
    return f"{slot.day} {slot.start}-{slot.end}"