"""Compila la interpretacion semantica del LLM (ScheduleEntry) al esquema interno.

El LLM razona el lenguaje (negaciones, topes, rangos, jerga) y emite restricciones
tipadas; este modulo hace la matematica de intervalos de forma determinista:

- available    -> disponibilidad explicita
- unavailable  -> remocion + disponibilidad implicita en el complemento del dia
                  (pragmatica: "no puedo despues de las 16" => puedo antes,
                  salvo que ya exista informacion para ese dia)
- only         -> disponibilidad exclusiva: remueve el resto de la semana
                  (y el resto del dia si la restriccion trae horas)
"""
import math
import re
import unicodedata

from app.schemas import (
    AvailabilityRemoval,
    ExtractedAvailability,
    ImpliedAvailability,
    Participant,
    ScheduleEntry,
    ScheduleInterpretation,
    TimeSlot,
)

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
WORKDAY_START = 9
WORKDAY_END = 18

_DAY_ALIASES = {
    "lunes": "lunes",
    "lun": "lunes",
    "martes": "martes",
    "mar": "martes",
    "miercoles": "miercoles",
    "mierc": "miercoles",
    "mier": "miercoles",
    "mie": "miercoles",
    "jueves": "jueves",
    "jue": "jueves",
    "viernes": "viernes",
    "vier": "viernes",
    "vie": "viernes",
}
_ALL_DAY_TOKENS = {"todos", "all", "todos los dias", "cualquier dia", "cualquiera"}


def compile_interpretation(interpretation: ScheduleInterpretation) -> ExtractedAvailability:
    participants: dict[str, Participant] = {}
    removals: dict[str, AvailabilityRemoval] = {}
    implied: dict[str, ImpliedAvailability] = {}

    for entry in interpretation.entries:
        person = entry.person.strip()
        if not person:
            continue

        days = _normalize_days(entry.days)
        if not days:
            continue

        hours = _normalize_hours(entry.start, entry.end)
        if hours is None:
            continue
        start_hour, end_hour = hours
        has_explicit_hours = _has_explicit_hours(entry.start, entry.end)
        week = _normalize_week_offset(entry.week_offset)

        if entry.kind == "available":
            _add_slots(_participant_for(participants, person).availability, days, start_hour, end_hour, week)

        elif entry.kind == "unavailable":
            _add_slots(_removal_for(removals, person).slots, days, start_hour, end_hour, week)
            # Complemento pragmatico solo si la no-disponibilidad es parcial.
            if has_explicit_hours and (start_hour > WORKDAY_START or end_hour < WORKDAY_END):
                target = _implied_for(implied, person)
                if start_hour > WORKDAY_START:
                    _add_slots(target.slots, days, WORKDAY_START, start_hour, week)
                if end_hour < WORKDAY_END:
                    _add_slots(target.slots, days, end_hour, WORKDAY_END, week)

        elif entry.kind == "only":
            _add_slots(_participant_for(participants, person).availability, days, start_hour, end_hour, week)
            removal = _removal_for(removals, person)
            other_days = [day for day in WEEKDAYS if day not in days]
            _add_slots(removal.slots, other_days, WORKDAY_START, WORKDAY_END, week)
            if has_explicit_hours:
                if start_hour > WORKDAY_START:
                    _add_slots(removal.slots, days, WORKDAY_START, start_hour, week)
                if end_hour < WORKDAY_END:
                    _add_slots(removal.slots, days, end_hour, WORKDAY_END, week)

    return ExtractedAvailability(
        participants=list(participants.values()),
        removals=[removal for removal in removals.values() if removal.slots],
        implied=[item for item in implied.values() if item.slots],
    )


def _participant_for(participants: dict[str, Participant], person: str) -> Participant:
    key = person.lower()
    if key not in participants:
        participants[key] = Participant(name=person)
    return participants[key]


def _removal_for(removals: dict[str, AvailabilityRemoval], person: str) -> AvailabilityRemoval:
    key = person.lower()
    if key not in removals:
        removals[key] = AvailabilityRemoval(participant_name=person)
    return removals[key]


def _implied_for(implied: dict[str, ImpliedAvailability], person: str) -> ImpliedAvailability:
    key = person.lower()
    if key not in implied:
        implied[key] = ImpliedAvailability(participant_name=person)
    return implied[key]


def _add_slots(target: list[TimeSlot], days: list[str], start_hour: int, end_hour: int, week_offset: int = 0) -> None:
    known = {(slot.week_offset, slot.day, slot.start, slot.end) for slot in target}
    for day in days:
        slot = TimeSlot(day=day, start=f"{start_hour:02d}:00", end=f"{end_hour:02d}:00", week_offset=week_offset)  # type: ignore[arg-type]
        key = (slot.week_offset, slot.day, slot.start, slot.end)
        if key not in known:
            target.append(slot)
            known.add(key)


def _normalize_week_offset(value: object) -> int:
    try:
        offset = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(offset, 8))  # techo defensivo: hasta 8 semanas adelante


def _normalize_days(raw_days: list[str]) -> list[str]:
    days: list[str] = []
    for raw in raw_days:
        token = _strip_accents(str(raw)).strip().rstrip(".")
        if not token:
            continue
        if token in _ALL_DAY_TOKENS:
            return list(WEEKDAYS)
        day = _DAY_ALIASES.get(token)
        if day and day not in days:
            days.append(day)
    return days


def _normalize_hours(start: str | None, end: str | None) -> tuple[int, int] | None:
    """Redondea a bloques de hora hacia afuera y recorta al horario laboral."""
    start_hour = _parse_hour(start, default=WORKDAY_START, round_up=False)
    end_hour = _parse_hour(end, default=WORKDAY_END, round_up=True)
    if start_hour is None or end_hour is None:
        return None

    start_hour = max(WORKDAY_START, min(start_hour, WORKDAY_END))
    end_hour = max(WORKDAY_START, min(end_hour, WORKDAY_END))
    if end_hour <= start_hour:
        return None
    return start_hour, end_hour


def _parse_hour(value: str | None, default: int, round_up: bool) -> int | None:
    if value is None or not str(value).strip():
        return default

    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", str(value))
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None

    exact = hour + minute / 60
    return math.ceil(exact) if round_up else math.floor(exact)


def _has_explicit_hours(start: str | None, end: str | None) -> bool:
    return bool((start and str(start).strip()) or (end and str(end).strip()))


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
