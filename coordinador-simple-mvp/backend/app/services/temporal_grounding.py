"""Deterministic temporal grounding for availability extraction.

Principle:
  LLM may interpret. Python validates. Ambiguity stays ambiguous.

A day is accepted only when grounded in:
  - the current message (explicit weekday, relative day, or cross-day range)
  - an explicit active-round coordination day (conversation context)

Never accepted as grounding:
  - past decisions from RAG / decision_history
  - model invention without textual support
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from app.schemas import (
    AvailabilityRemoval,
    ExtractedAvailability,
    ImpliedAvailability,
    Participant,
    TimeSlot,
)

# Local imports deferred where needed to avoid circular import at module load.
WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]

GroundingSource = str  # message | active_round | none | rejected_model_inference

# Only explicit active-round coordination language — never past-meeting narrative.
_COORDINATION_DAY_PATTERNS = (
    r"\bcoordin(?:ando|ar|acion|aci[oó]n|amos|emos)\b[^.]{0,48}\b"
    r"(?:para\s+(?:el\s+|este\s+)?|el\s+|este\s+)?(?P<day>lunes|martes|miercoles|mi[eé]rcoles|jueves|viernes)\b",
    r"\bpara\s+(?:el\s+|este\s+)?(?P<day>lunes|martes|miercoles|mi[eé]rcoles|jueves|viernes)\b",
    r"\bestamos\s+coordinando\s+(?:para\s+)?(?:el\s+|este\s+)?"
    r"(?P<day>lunes|martes|miercoles|mi[eé]rcoles|jueves|viernes)\b",
)


@dataclass
class TemporalGroundingResult:
    extraction: ExtractedAvailability
    allowed_days: set[str] = field(default_factory=set)
    grounding_source: GroundingSource = "none"
    rejected_days: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def detect_active_round_days(context_text: str | None) -> set[str]:
    """Days explicitly fixed by active-round coordination language."""
    if not context_text or not context_text.strip():
        return set()
    from app.services.llm_service import normalize

    normalized = normalize(context_text)
    found: set[str] = set()
    for pattern in _COORDINATION_DAY_PATTERNS:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            day = match.group("day")
            day = day.replace("é", "e").replace("ó", "o")
            if day.startswith("mierc"):
                found.add("miercoles")
            elif day in WEEKDAYS:
                found.add(day)
    return found


def collect_message_grounded_days(
    message: str,
    *,
    now: datetime | None = None,
) -> tuple[set[str], bool]:
    """Weekdays supported by the current user text (including relatives).

    Returns ``(days, time_only_defaulted)``. When the text only has hours
    (e.g. \"puedo a las 7\") and no weekday, Python anchors to the current
    workday — this is deterministic, not LLM invention.
    """
    from app.services.llm_service import (
        detect_days,
        find_cross_day_ranges,
        resolve_relative_days,
        _days_in_cross_day_range,
        has_extractable_scheduling_signal,
        normalize,
    )

    rewritten = resolve_relative_days(message, now=now)
    allowed = set(detect_days(rewritten))
    for day_range in find_cross_day_ranges(rewritten):
        allowed.update(_days_in_cross_day_range(day_range))

    time_only_defaulted = False
    if not allowed and _message_has_time_anchor(normalize(rewritten)):
        default_day = _current_or_next_workday(now)
        if default_day:
            allowed.add(default_day)
            time_only_defaulted = True
    return allowed, time_only_defaulted


def _message_has_time_anchor(normalized: str) -> bool:
    if re.search(r"\b\d{1,2}(?::\d{2})?\b", normalized):
        return True
    if re.search(
        r"\b(a\s+las?|desde\s+las?|despues\s+de\s+las?|hasta\s+las?|tipo\s+las?)\b",
        normalized,
    ):
        return True
    return False


def _current_or_next_workday(now: datetime | None = None) -> str | None:
    from app.services.llm_service import _timezone_now, WEEKDAYS as LLM_WEEKDAYS

    current = _timezone_now(now)
    # Monday=0 .. Sunday=6
    weekday = current.weekday()
    if weekday < len(LLM_WEEKDAYS):
        return LLM_WEEKDAYS[weekday]
    # Weekend: roll to Monday
    return "lunes"


def _remap_extraction_days(extraction: ExtractedAvailability, day: str) -> ExtractedAvailability:
    def remap_slots(slots: list[TimeSlot]) -> list[TimeSlot]:
        return [slot.model_copy(update={"day": day}) for slot in slots]  # type: ignore[arg-type]

    participants = [
        p.model_copy(update={"availability": remap_slots(p.availability)})
        for p in extraction.participants
    ]
    removals = [
        r.model_copy(update={"slots": remap_slots(r.slots)}) for r in extraction.removals
    ]
    implied = [
        item.model_copy(update={"slots": remap_slots(item.slots)}) for item in extraction.implied
    ]
    return extraction.model_copy(
        update={"participants": participants, "removals": removals, "implied": implied}
    )


def validate_extracted_temporal_grounding(
    extraction: ExtractedAvailability,
    source_message: str,
    *,
    active_round_context: str | None = None,
    active_round_days: Iterable[str] | None = None,
    now: datetime | None = None,
) -> TemporalGroundingResult:
    """Filter slots whose day is not grounded in message or active round.

    Past decisions are intentionally NOT accepted as grounding.
    Time-only messages are anchored to the current workday by Python.
    """
    from app.services.llm_service import has_exclusive_week_constraint, normalize

    message_days, time_only_defaulted = collect_message_grounded_days(source_message, now=now)
    round_days = set(active_round_days or ())
    round_days.update(detect_active_round_days(active_round_context))

    # Explicit coordination day beats clock-default "today" from bare times.
    if time_only_defaulted and round_days:
        message_days = set()
        time_only_defaulted = False

    allowed = set(message_days) | set(round_days)
    if time_only_defaulted and len(allowed) == 1:
        grounding_source: GroundingSource = "message"
        extraction = _remap_extraction_days(extraction, next(iter(allowed)))
    elif message_days and not time_only_defaulted:
        grounding_source = "message"
    elif round_days:
        grounding_source = "active_round"
        if len(round_days) == 1 and _extraction_days(extraction) - round_days:
            # Remap invented days onto the single active-round day when the
            # message only had times (or ambiguous days already filtered out).
            if time_only_defaulted or not (message_days - round_days):
                extraction = _remap_extraction_days(extraction, next(iter(round_days)))
    elif message_days:
        grounding_source = "message"
    else:
        grounding_source = "none"

    flags = list(extraction.quality_flags)
    if time_only_defaulted and "temporal_time_defaults_today" not in flags:
        flags.append("temporal_time_defaults_today")

    observed_days = _extraction_days(extraction)
    rejected = sorted(day for day in observed_days if day not in allowed) if allowed or observed_days else []

    # Exclusive week constraints ("solo puedo jueves") still need a day in message.
    preserve_exclusive_removals = has_exclusive_week_constraint(source_message) and bool(message_days)

    if not allowed:
        # Fully ungrounded (no day, no time, no active round): strip invented days.
        stripped = _strip_all_day_slots(extraction)
        if observed_days:
            if "rejected_model_day_inference" not in flags:
                flags.append("rejected_model_day_inference")
            if "temporal_day_ungrounded" not in flags:
                flags.append("temporal_day_ungrounded")
            grounding_source = "rejected_model_inference"
        stripped = stripped.model_copy(update={"quality_flags": flags})
        return TemporalGroundingResult(
            extraction=stripped,
            allowed_days=set(),
            grounding_source=grounding_source,
            rejected_days=sorted(observed_days),
            flags=flags,
        )

    exclusive_participants = {
        normalize(participant.name)
        for participant in extraction.participants
        if any(slot.day in allowed for slot in participant.availability)
    }

    participants: list[Participant] = []
    for participant in extraction.participants:
        grounded_slots = [slot for slot in participant.availability if slot.day in allowed]
        if grounded_slots or not participant.availability:
            participants.append(participant.model_copy(update={"availability": grounded_slots}))

    removals: list[AvailabilityRemoval] = []
    for removal in extraction.removals:
        participant_key = normalize(removal.participant_name)
        keep_week_removals = preserve_exclusive_removals and (
            not exclusive_participants or participant_key in exclusive_participants
        )
        grounded_slots = [
            slot
            for slot in removal.slots
            if slot.day in allowed or keep_week_removals
        ]
        if grounded_slots:
            removals.append(removal.model_copy(update={"slots": grounded_slots}))

    implied: list[ImpliedAvailability] = []
    for item in extraction.implied:
        grounded_slots = [slot for slot in item.slots if slot.day in allowed]
        if grounded_slots:
            implied.append(item.model_copy(update={"slots": grounded_slots}))

    if rejected:
        if "rejected_model_day_inference" not in flags:
            flags.append("rejected_model_day_inference")

    filtered = extraction.model_copy(
        update={
            "participants": participants,
            "removals": removals,
            "implied": implied,
            "quality_flags": flags,
        }
    )
    return TemporalGroundingResult(
        extraction=filtered,
        allowed_days=set(allowed),
        grounding_source=grounding_source,
        rejected_days=rejected,
        flags=flags,
    )


def _extraction_days(extraction: ExtractedAvailability) -> set[str]:
    days: set[str] = set()
    for participant in extraction.participants:
        days.update(slot.day for slot in participant.availability)
    for removal in extraction.removals:
        days.update(slot.day for slot in removal.slots)
    for item in extraction.implied:
        days.update(slot.day for slot in item.slots)
    return days


def _strip_all_day_slots(extraction: ExtractedAvailability) -> ExtractedAvailability:
    """Keep person mentions if needed, but remove day-bound invented slots."""
    participants = [
        participant.model_copy(update={"availability": []})
        for participant in extraction.participants
        if participant.name.strip()
    ]
    # Drop ungrounded removals/implied entirely (they require a day).
    return extraction.model_copy(
        update={
            "participants": participants,
            "removals": [],
            "implied": [],
        }
    )
