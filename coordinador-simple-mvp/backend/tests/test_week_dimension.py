"""Dimension semana/fecha (week_offset): el sistema debe coordinar bien sin
importar la fecha actual, distinguiendo "esta semana" de "la otra semana" y
calculando la fecha calendario correcta de cada opcion.

Regresion del caso reportado: "me detecta la otra semana cuando me referia a
esta". Aqui fijamos que 0 = esta semana / proxima ocurrencia y 1 = la proxima
semana calendario, sin colisionar entre semanas ni sobre-desplazar dias pasados.
"""
from datetime import date, datetime, timezone as dt_tz

import pytest

from app.schemas import (
    Participant,
    ScheduleEntry,
    ScheduleInterpretation,
    Session,
    TimeOption,
    TimeSlot,
)
from app.services.calendar_export import (
    date_for_day,
    event_datetimes,
    load_timezone,
    now_local,
    option_event_date,
)
from app.services.decision_engine import calculate_options
from app.services.llm_service import LlmService, detect_week_offset
from app.services.schedule_compiler import compile_interpretation
from app.services.session_service import merge_slots, subtract_slot
from app.settings import settings

# Lunes 06-07-2026 12:00 UTC: dia futuro/actual claro para asertar fechas.
MON = datetime(2026, 7, 6, 12, 0, tzinfo=dt_tz.utc)
# Miercoles 08-07-2026: el lunes/martes de esta semana ya pasaron.
WED = datetime(2026, 7, 8, 12, 0, tzinfo=dt_tz.utc)


@pytest.fixture(autouse=True)
def _utc_timezone(monkeypatch):
    # Fija UTC para que las fechas no se corran por conversion horaria.
    monkeypatch.setattr(settings, "app_timezone", "UTC")


@pytest.fixture()
def mock_service(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    return LlmService()


def _compile(*entries: ScheduleEntry):
    return compile_interpretation(ScheduleInterpretation(entries=list(entries)))


# --- Compilador: week_offset se propaga y se acota -------------------------

def test_available_carries_week_offset():
    result = _compile(ScheduleEntry(person="Ana", kind="available", days=["lunes"], week_offset=1))
    slot = result.participants[0].availability[0]
    assert (slot.day, slot.week_offset) == ("lunes", 1)


def test_unavailable_and_implied_carry_week_offset():
    result = _compile(
        ScheduleEntry(person="Elon", kind="unavailable", days=["lunes"], start="16:00", end="18:00", week_offset=1)
    )
    assert result.removals[0].slots[0].week_offset == 1
    assert result.implied[0].slots[0].week_offset == 1


def test_only_removes_other_days_in_same_week():
    result = _compile(ScheduleEntry(person="Ana", kind="only", days=["miercoles"], week_offset=1))
    assert result.participants[0].availability[0].week_offset == 1
    # Todas las remociones (resto de la semana) quedan en la misma semana.
    assert result.removals[0].slots
    assert all(slot.week_offset == 1 for slot in result.removals[0].slots)


def test_week_offset_is_clamped():
    high = _compile(ScheduleEntry(person="Ana", kind="available", days=["lunes"], week_offset=99))
    assert high.participants[0].availability[0].week_offset == 8
    low = _compile(ScheduleEntry(person="Ana", kind="available", days=["lunes"], week_offset=-4))
    assert low.participants[0].availability[0].week_offset == 0


# --- Motor de decision: las semanas no colisionan --------------------------

def test_same_day_hour_different_weeks_do_not_collide():
    session = Session(
        title="Semanas",
        participants=[
            Participant(name="Ana", availability=[TimeSlot(day="lunes", start="15:00", end="16:00", week_offset=0)]),
            Participant(name="Beto", availability=[TimeSlot(day="lunes", start="15:00", end="16:00", week_offset=1)]),
        ],
    )
    options = calculate_options(session)

    # Dos opciones distintas (una por semana); los scores NO se suman entre semanas.
    weeks = sorted(option.week_offset for option in options)
    assert weeks == [0, 1]
    assert all(option.score == 1 for option in options)


def test_nearer_week_ranked_first_on_tie():
    session = Session(
        title="Orden",
        participants=[
            Participant(
                name="Ana",
                availability=[
                    TimeSlot(day="lunes", start="15:00", end="16:00", week_offset=1),
                    TimeSlot(day="lunes", start="15:00", end="16:00", week_offset=0),
                ],
            )
        ],
    )
    options = calculate_options(session)
    assert options[0].week_offset == 0  # a igual score, la semana mas cercana primero


# --- Session service: merge/subtract respetan la semana --------------------

def test_merge_slots_keeps_weeks_separate():
    existing = [TimeSlot(day="lunes", start="09:00", end="10:00", week_offset=0)]
    incoming = [TimeSlot(day="lunes", start="09:00", end="10:00", week_offset=1)]
    assert len(merge_slots(existing, incoming)) == 2


def test_subtract_slot_only_affects_same_week():
    slot = TimeSlot(day="lunes", start="09:00", end="18:00", week_offset=0)
    other_week = TimeSlot(day="lunes", start="09:00", end="18:00", week_offset=1)
    assert subtract_slot(slot, other_week) == [slot]  # otra semana -> intacto


# --- Calendar export: fechas correctas por semana --------------------------

def test_date_for_day_adds_calendar_weeks():
    assert date_for_day("lunes", 0, MON) == date(2026, 7, 6)
    assert date_for_day("lunes", 1, MON) == date(2026, 7, 13)
    assert date_for_day("viernes", 0, MON) == date(2026, 7, 10)
    assert date_for_day("viernes", 1, MON) == date(2026, 7, 17)


def test_passed_day_this_week_rolls_to_next_occurrence():
    # Hoy miercoles: el lunes de esta semana ya paso -> offset 0 rueda al proximo.
    assert date_for_day("lunes", 0, WED) == date(2026, 7, 13)


def test_otra_semana_does_not_overshoot_passed_day():
    # Hoy miercoles, martes ya paso: "el martes de la otra semana" = 14/07 (la
    # proxima semana calendario), no 21/07.
    assert date_for_day("martes", 1, WED) == date(2026, 7, 14)


def test_event_datetimes_uses_requested_week():
    option = TimeOption(
        day="miercoles",
        start="15:00",
        end="16:00",
        week_offset=1,
        available_participants=["Ana"],
        score=1,
    )
    start_at, end_at = event_datetimes(option, now_local(MON), load_timezone("UTC"))
    assert start_at.date() == date(2026, 7, 15)  # miercoles de la proxima semana
    assert end_at.date() == date(2026, 7, 15)


def test_option_event_date_matches_week():
    option = TimeOption(
        day="lunes",
        start="15:00",
        end="16:00",
        week_offset=1,
        available_participants=["Ana"],
        score=1,
    )
    assert option_event_date(option, MON) == date(2026, 7, 13)


# --- Mock: deteccion de la semana en lenguaje natural ----------------------

def test_detect_week_offset_variants():
    assert detect_week_offset("el lunes puedo") == 0
    assert detect_week_offset("puedo la otra semana el lunes") == 1
    assert detect_week_offset("nos juntamos la proxima semana") == 1
    assert detect_week_offset("la semana que viene el martes") == 1
    assert detect_week_offset("en dos semanas el jueves") == 2
    assert detect_week_offset("en 3 semanas") == 3


def test_mock_pipeline_sets_week_offset(mock_service):
    extraction, _, _ = mock_service.extract_availability(
        "Ana puede la otra semana el lunes en la tarde"
    )
    ana = next(participant for participant in extraction.participants if participant.name == "Ana")
    assert ana.availability
    assert all(slot.week_offset == 1 for slot in ana.availability)


def test_mock_this_week_stays_offset_zero(mock_service):
    extraction, _, _ = mock_service.extract_availability("Ana puede el lunes en la tarde")
    ana = next(participant for participant in extraction.participants if participant.name == "Ana")
    assert ana.availability
    assert all(slot.week_offset == 0 for slot in ana.availability)
