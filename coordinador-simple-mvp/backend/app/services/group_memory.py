"""Structured group memory for retrieval-augmented availability extraction.

This is structured RAG over the session store (not a vector database):
retrieve roster, identity hints, active constraints and recent decisions,
then inject a compact block into the LLM extraction prompt.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.schemas import DecisionRecord, Participant, Session, TimeSlot

MAX_PARTICIPANTS = 40
MAX_IDENTITY_HINTS = 30
MAX_CONSTRAINTS = 20
MAX_PAST_DECISIONS = 3
MAX_PREVIEW_LENGTH = 240
MAX_GROUP_NAME_LENGTH = 80
MAX_HINT_LENGTH = 100
MAX_CONSTRAINT_LENGTH = 120
MAX_DECISION_LENGTH = 140

# Redact long numeric tokens that look like phone fragments or raw JIDs.
_SENSITIVE_TOKEN = re.compile(
    r"(?:@s\.whatsapp\.net|@lid|@g\.us)|(?:\b\d{8,}\b)",
    re.IGNORECASE,
)


class GroupMemoryContext(BaseModel):
    """Compact, serializable memory recovered from a coordination session."""

    group_name: str | None = None
    group_jid: str | None = None
    known_participants: list[str] = Field(default_factory=list)
    identity_hints: list[str] = Field(default_factory=list)
    known_constraints_summary: list[str] = Field(default_factory=list)
    past_decisions: list[str] = Field(default_factory=list)
    expected_group_size: int | None = None
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "session_store"

    def has_content(self) -> bool:
        return bool(
            (self.group_name and self.group_name.strip())
            or self.known_participants
            or self.identity_hints
            or self.known_constraints_summary
            or self.past_decisions
            or (self.expected_group_size is not None and self.expected_group_size > 0)
        )


def build_group_memory_context(
    session: Session,
    *,
    max_participants: int = MAX_PARTICIPANTS,
    max_identity_hints: int = MAX_IDENTITY_HINTS,
    max_constraints: int = MAX_CONSTRAINTS,
    max_past_decisions: int = MAX_PAST_DECISIONS,
) -> GroupMemoryContext:
    """Retrieve structured memory from the live session model."""
    config = session.channel_config
    group_name = _clean_text(config.group_name or session.title, MAX_GROUP_NAME_LENGTH)
    # Keep jid only for internal fingerprint/source metadata, never full in preview.
    group_jid = _clean_text(config.group_jid, 120) if config.group_jid else None

    known_participants = _collect_participants(session, max_participants)
    identity_hints = _collect_identity_hints(session, max_identity_hints)
    constraints = _collect_constraints(session, max_constraints)
    past_decisions = _collect_past_decisions(session, max_past_decisions)

    expected = config.group_participant_count
    if expected is not None and expected <= 0:
        expected = None

    return GroupMemoryContext(
        group_name=group_name or None,
        group_jid=group_jid,
        known_participants=known_participants,
        identity_hints=identity_hints,
        known_constraints_summary=constraints,
        past_decisions=past_decisions,
        expected_group_size=expected,
        source="session_store",
    )


def memory_fingerprint(memory: GroupMemoryContext | str | None) -> str:
    """Stable hash of semantic memory fields (excludes retrieved_at)."""
    if memory is None:
        return "none"
    if isinstance(memory, str):
        normalized = " ".join(memory.strip().lower().split())
        if not normalized:
            return "none"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    payload = {
        "group_name": (memory.group_name or "").strip().lower(),
        # Fingerprint uses a redacted/stable group id token, not displayed in preview.
        "group_jid": (memory.group_jid or "").strip().lower(),
        "known_participants": sorted({p.strip().lower() for p in memory.known_participants if p.strip()}),
        "identity_hints": sorted({h.strip().lower() for h in memory.identity_hints if h.strip()}),
        "known_constraints_summary": sorted(
            {c.strip().lower() for c in memory.known_constraints_summary if c.strip()}
        ),
        "past_decisions": list(memory.past_decisions),  # already newest-first; keep order
        "expected_group_size": memory.expected_group_size,
        "source": memory.source,
    }
    # Sort keys for stable JSON-like encoding without importing json overhead differently
    encoded = repr(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def format_group_memory_for_prompt(memory: GroupMemoryContext | None) -> str | None:
    """Render the retrieval block injected into the extraction prompt."""
    if memory is None or not memory.has_content():
        return None

    lines: list[str] = [
        "## Memoria recuperada del grupo (RAG estructurado)",
        "Esta seccion proviene del almacén de sesiones (retrieval estructurado, no embeddings).",
    ]

    if memory.group_name:
        lines.append(f"- Grupo: {memory.group_name}")
    if memory.expected_group_size is not None:
        lines.append(f"- Tamaño declarado del grupo: {memory.expected_group_size}")
    if memory.known_participants:
        lines.append(
            "- Participantes conocidos: " + ", ".join(memory.known_participants)
        )
    if memory.identity_hints:
        lines.append("- Pistas de identidad:")
        lines.extend(f"  - {hint}" for hint in memory.identity_hints)
    if memory.known_constraints_summary:
        lines.append("- Restricciones ya registradas en esta ronda (estado activo del sistema):")
        lines.extend(f"  - {item}" for item in memory.known_constraints_summary)
    if memory.past_decisions:
        lines.append("- Decisiones previas del grupo (historial; NO son disponibilidad actual):")
        lines.extend(f"  - {item}" for item in memory.past_decisions)

    lines.extend(
        [
            "",
            "Reglas de uso de esta memoria:",
            "- Usala unicamente para desambiguar nombres, participantes y referencias contextuales.",
            "- Si un alias del texto coincide con la memoria, usa el nombre canonico del roster",
            "  (ej. Yuli -> Julissa (Yuli) si asi figura en participantes conocidos).",
            "- NO inventes disponibilidad que no aparezca en el texto a analizar.",
            "- NO inventes un dia si el texto no lo indica (no rellenes con el dia de una decision previa).",
            "- NO conviertas decisiones historicas en disponibilidad actual.",
            "- Las restricciones listadas como 'ya registradas en esta ronda' son estado activo del sistema;",
            "  no las re-emitas como si vinieran del texto salvo que el mensaje actual las confirme o las cambie.",
            "- Una decision previa solo da contexto de habitos del grupo; no implica que siga vigente.",
            "- Si el texto actual contradice la memoria, prioriza el texto actual.",
        ]
    )
    return "\n".join(lines)


def format_group_memory_preview(
    memory: GroupMemoryContext | None,
    *,
    max_length: int = MAX_PREVIEW_LENGTH,
) -> str:
    """Short, demo-safe summary for last_processing (no full JIDs or prompt)."""
    if memory is None or not memory.has_content():
        return ""

    parts: list[str] = []
    if memory.group_name:
        parts.append(f"grupo={_redact_sensitive(memory.group_name)}")
    if memory.known_participants:
        sample = ", ".join(memory.known_participants[:5])
        extra = len(memory.known_participants) - 5
        if extra > 0:
            sample = f"{sample} (+{extra})"
        parts.append(f"roster={sample}")
    if memory.identity_hints:
        parts.append(f"alias={len(memory.identity_hints)}")
    if memory.known_constraints_summary:
        parts.append(f"restricciones={len(memory.known_constraints_summary)}")
    if memory.past_decisions:
        parts.append(f"decisiones_previas={len(memory.past_decisions)}")
    if memory.expected_group_size is not None:
        parts.append(f"tamano={memory.expected_group_size}")

    preview = "; ".join(parts)
    preview = _redact_sensitive(preview)
    if len(preview) > max_length:
        return preview[: max_length - 1].rstrip() + "…"
    return preview


def resolve_memory_prompt_block(memory: GroupMemoryContext | str | None) -> str | None:
    if memory is None:
        return None
    if isinstance(memory, str):
        text = memory.strip()
        return text or None
    return format_group_memory_for_prompt(memory)


# Sentinel: alias maps to more than one distinct canonical person.
_AMBIGUOUS_ALIAS = object()


def build_alias_lookup(memory: GroupMemoryContext | None) -> dict[str, str | object]:
    """Map normalized alias/bare names -> canonical roster display name.

    Exact normalized match only (case/accent insensitive). No fuzzy/partial
    matching. Conflicting aliases map to ``_AMBIGUOUS_ALIAS``.
    """
    if memory is None:
        return {}
    lookup: dict[str, str | object] = {}

    def remember(alias: str, canonical: str) -> None:
        key = _normalize_name(alias)
        canon = (canonical or "").strip()
        if not key or not canon:
            return
        existing = lookup.get(key)
        if existing is None:
            lookup[key] = canon
            return
        if existing is _AMBIGUOUS_ALIAS:
            return
        if isinstance(existing, str):
            if _normalize_name(existing) == _normalize_name(canon):
                # Same person under longer/shorter roster label: keep longer.
                if len(canon) > len(existing):
                    lookup[key] = canon
                return
            # Distinct people share this token.
            lookup[key] = _AMBIGUOUS_ALIAS

    for name in memory.known_participants:
        display = (name or "").strip()
        if not display:
            continue
        remember(display, display)
        bare = _PAREN_ALIAS.sub(" ", display)
        bare = " ".join(bare.split())
        if bare:
            remember(bare, display)
        for alias in _extract_parenthetical_aliases(display):
            remember(alias, display)

    for hint in memory.identity_hints:
        match = re.match(
            r"(.+?)\s+tambien es conocida/o como\s+(.+)$",
            hint.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        canonical = match.group(1).strip()
        alias = match.group(2).strip()
        remember(alias, canonical)
        remember(canonical, canonical)

    return lookup


def resolve_display_name(name: str, lookup: dict[str, str | object]) -> str:
    raw = (name or "").strip().lstrip("@").strip()
    if not raw:
        return raw
    if raw.lower() in {"yo", "me", "mi"}:
        return raw
    key = _normalize_name(raw)
    if key not in lookup:
        return raw
    value = lookup[key]
    if value is _AMBIGUOUS_ALIAS or not isinstance(value, str):
        # Keep original label; do not guess among conflicts.
        return raw
    return value


def apply_memory_identity(
    extraction: "ExtractedAvailability",
    memory: GroupMemoryContext | None,
) -> "ExtractedAvailability":
    """Rewrite extracted person labels using retrieved alias/roster memory.

    Precedence (higher first) is applied elsewhere for channel IDs:
      1) reliable channel id binding (normalize_channel_extraction)
      2) unique known alias / exact roster name (this function)
      3) leave ambiguous / unknown unchanged

    Deterministic post-step: does not invent slots, only renames unique aliases.
    Never uses partial/fuzzy name matching.
    """
    from app.schemas import ExtractedAvailability  # local import avoids cycles at module load

    if memory is None or not isinstance(extraction, ExtractedAvailability):
        return extraction
    lookup = build_alias_lookup(memory)
    if not lookup:
        return extraction

    flags = list(extraction.quality_flags)

    def map_name(label: str) -> str:
        key = _normalize_name(label.lstrip("@"))
        if key in lookup and lookup[key] is _AMBIGUOUS_ALIAS:
            if "ambiguous_alias_identity" not in flags:
                flags.append("ambiguous_alias_identity")
        return resolve_display_name(label, lookup)

    for participant in extraction.participants:
        # Channel-bound identities are authoritative; do not rename away from id.
        if participant.external_id or participant.external_ids:
            continue
        participant.name = map_name(participant.name)
    for removal in extraction.removals:
        if removal.external_id or removal.external_ids:
            continue
        removal.participant_name = map_name(removal.participant_name)
    for item in extraction.implied:
        if item.external_id or item.external_ids:
            continue
        item.participant_name = map_name(item.participant_name)
    extraction.replacements = [map_name(name) for name in extraction.replacements]
    if flags != extraction.quality_flags:
        extraction = extraction.model_copy(update={"quality_flags": flags})
    return extraction


def _normalize_name(value: str) -> str:
    text = " ".join((value or "").strip().lower().lstrip("@").split())
    # Fold accents lightly for alias matching without importing unicodedata heavy path.
    replacements = (
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
        ("ü", "u"),
        ("ñ", "n"),
    )
    for src, dst in replacements:
        text = text.replace(src, dst)
    return text


# --- collectors -----------------------------------------------------------------


def _collect_participants(session: Session, limit: int) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for participant in session.participants:
        label = _participant_label(participant)
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        names.append(label)
        if len(names) >= limit:
            break
    return names


def _collect_identity_hints(session: Session, limit: int) -> list[str]:
    """Build human-readable identity/alias hints without exposing raw JIDs.

    Sources:
    - Parenthetical aliases in roster names: ``Julissa (Yuli)``
    - Channel senders whose technical id matches a known participant
    - Other observed sender display names (as soft evidence only)
    """
    hints: list[str] = []
    seen: set[str] = set()

    def add(hint: str) -> None:
        cleaned = _clean_text(hint, MAX_HINT_LENGTH)
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        hints.append(cleaned)

    # Map technical channel identities -> canonical participant display name.
    id_to_canonical: dict[str, str] = {}
    for participant in session.participants:
        display = (participant.name or "").strip()
        if not display:
            continue
        for raw_id in [participant.external_id, *list(participant.external_ids or [])]:
            if not raw_id:
                continue
            token = str(raw_id).strip().lower()
            if token:
                id_to_canonical[token] = display
        # Explicit alias embedded in the display name, e.g. "Julissa (Yuli)".
        for alias in _extract_parenthetical_aliases(display):
            if alias.lower() != display.lower():
                add(f"{display} tambien es conocida/o como {alias}")

    # Link observed senders to roster when the channel id matches.
    for message in session.channel_messages:
        if message.kind != "human":
            continue
        sender = (message.sender or "").strip()
        if not sender or sender.lower() in {"sistema", "system", "bot"}:
            continue
        sender_ids = [
            value.strip().lower()
            for value in [message.sender_id, *list(message.sender_aliases or [])]
            if value and str(value).strip()
        ]
        matched = next((id_to_canonical[sid] for sid in sender_ids if sid in id_to_canonical), None)
        if matched and matched.lower() != sender.lower():
            add(f"{matched} tambien es conocida/o como {sender}")
        elif matched:
            add(f"remitente observado: {sender} (roster: {matched})")
        else:
            add(f"remitente observado: {sender}")
        if len(hints) >= limit:
            break

    return hints[:limit]


_PAREN_ALIAS = re.compile(r"\(([^)]+)\)")


def _extract_parenthetical_aliases(display_name: str) -> list[str]:
    aliases: list[str] = []
    for match in _PAREN_ALIAS.finditer(display_name or ""):
        for part in re.split(r"[,;/]|alias", match.group(1), flags=re.IGNORECASE):
            cleaned = " ".join(part.split()).strip(" -_")
            if cleaned and len(cleaned) >= 2:
                aliases.append(cleaned)
    return aliases


def _collect_constraints(session: Session, limit: int) -> list[str]:
    lines: list[str] = []
    for participant in session.participants:
        if not participant.availability:
            continue
        name = _participant_label(participant)
        if not name:
            continue
        slots = sorted(
            participant.availability,
            key=lambda slot: (slot.week_offset, slot.day, slot.start, slot.end),
        )
        slot_text = ", ".join(_format_slot(slot) for slot in slots[:6])
        if len(slots) > 6:
            slot_text = f"{slot_text}, +{len(slots) - 6} mas"
        line = _clean_text(f"{name}: disponible {slot_text}", MAX_CONSTRAINT_LENGTH)
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _collect_past_decisions(session: Session, limit: int) -> list[str]:
    # decision_history is chronological; take last N then present newest first.
    recent = list(session.decision_history)[-limit:]
    recent.reverse()
    return [line for line in (_format_decision(record) for record in recent) if line]


def _format_decision(record: DecisionRecord) -> str:
    option = record.option
    day = option.day
    start = option.start
    end = option.end
    week = option.week_offset
    week_bit = f", semana+{week}" if week else ""
    date_bit = f", fecha {record.event_date}" if record.event_date else ""
    source = record.source
    # Avoid dumping long free-text summaries that may contain PII.
    text = (
        f"{day} {start}-{end}{week_bit}{date_bit} "
        f"(origen={source}, cobertura={option.coverage_percent}%)"
    )
    return _clean_text(text, MAX_DECISION_LENGTH) or text[:MAX_DECISION_LENGTH]


def _format_slot(slot: TimeSlot) -> str:
    week = f" s+{slot.week_offset}" if slot.week_offset else ""
    return f"{slot.day} {slot.start}-{slot.end}{week}"


def _participant_label(participant: Participant) -> str:
    return _clean_text(participant.name, 80) or ""


def _clean_text(value: str | None, max_length: int) -> str:
    if not value:
        return ""
    text = " ".join(str(value).split())
    text = _redact_sensitive(text)
    if len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def _redact_sensitive(value: str) -> str:
    return _SENSITIVE_TOKEN.sub("[id]", value)
