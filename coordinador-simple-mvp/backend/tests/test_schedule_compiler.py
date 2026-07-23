"""Tests del compilador de restricciones semanticas (IR del LLM -> esquema interno)."""
from app.schemas import ScheduleEntry, ScheduleInterpretation
from app.services.schedule_compiler import compile_interpretation


def _compile(*entries: ScheduleEntry):
    return compile_interpretation(ScheduleInterpretation(entries=list(entries)))


def test_compiler_uses_configurable_workday_for_full_day():
    result = compile_interpretation(
        ScheduleInterpretation(
            entries=[ScheduleEntry(person="Ana", kind="available", days=["lunes"])]
        ),
        workday_start=7,
        workday_end=20,
    )
    assert [(slot.start, slot.end) for slot in result.participants[0].availability] == [("07:00", "20:00")]


def _slots(items):
    return [(slot.day, slot.start, slot.end) for slot in items]


def test_available_with_day_and_hours():
    result = _compile(ScheduleEntry(person="Ana", kind="available", days=["lunes"], start="10:00", end="12:00"))

    assert result.participants[0].name == "Ana"
    assert _slots(result.participants[0].availability) == [("lunes", "10:00", "12:00")]
    assert result.removals == []
    assert result.implied == []


def test_available_without_hours_is_full_workday():
    result = _compile(ScheduleEntry(person="Ana", kind="available", days=["martes"]))

    assert _slots(result.participants[0].availability) == [("martes", "09:00", "18:00")]


def test_unavailable_partial_emits_removal_and_implied_complement():
    # "Elon no puede despues de las 16 el lunes"
    result = _compile(ScheduleEntry(person="Elon", kind="unavailable", days=["lunes"], start="16:00", end="18:00"))

    assert result.participants == []
    assert result.removals[0].participant_name == "Elon"
    assert _slots(result.removals[0].slots) == [("lunes", "16:00", "18:00")]
    assert result.implied[0].participant_name == "Elon"
    assert _slots(result.implied[0].slots) == [("lunes", "09:00", "16:00")]


def test_unavailable_middle_range_implies_both_sides():
    result = _compile(ScheduleEntry(person="Ana", kind="unavailable", days=["martes"], start="12:00", end="14:00"))

    assert _slots(result.implied[0].slots) == [("martes", "09:00", "12:00"), ("martes", "14:00", "18:00")]


def test_unavailable_full_day_implies_nothing():
    result = _compile(ScheduleEntry(person="Ana", kind="unavailable", days=["martes"]))

    assert _slots(result.removals[0].slots) == [("martes", "09:00", "18:00")]
    assert result.implied == []


def test_only_excludes_rest_of_week_and_rest_of_day():
    result = _compile(ScheduleEntry(person="Yo", kind="only", days=["miercoles"], start="10:00", end="12:00"))

    assert _slots(result.participants[0].availability) == [("miercoles", "10:00", "12:00")]
    removal_slots = _slots(result.removals[0].slots)
    for day in ["lunes", "martes", "jueves", "viernes"]:
        assert (day, "09:00", "18:00") in removal_slots
    assert ("miercoles", "09:00", "10:00") in removal_slots
    assert ("miercoles", "12:00", "18:00") in removal_slots


def test_days_todos_expands_and_accents_normalize():
    result = _compile(ScheduleEntry(person="Ana", kind="available", days=["todos"]))
    assert {slot.day for slot in result.participants[0].availability} == {
        "lunes", "martes", "miercoles", "jueves", "viernes"
    }

    accented = _compile(ScheduleEntry(person="Ana", kind="available", days=["miércoles", "Sábado"]))
    assert _slots(accented.participants[0].availability) == [("miercoles", "09:00", "18:00")]


def test_minutes_round_outward_and_hours_clamp_to_workday():
    result = _compile(ScheduleEntry(person="Ana", kind="available", days=["lunes"], start="15:30", end="16:30"))
    assert _slots(result.participants[0].availability) == [("lunes", "15:00", "17:00")]

    clamped = _compile(ScheduleEntry(person="Ana", kind="available", days=["lunes"], start="07:00", end="20:00"))
    assert _slots(clamped.participants[0].availability) == [("lunes", "09:00", "18:00")]


def test_invalid_entries_are_dropped_safely():
    result = _compile(
        ScheduleEntry(person="", kind="available", days=["lunes"]),
        ScheduleEntry(person="Ana", kind="available", days=[]),
        ScheduleEntry(person="Ana", kind="available", days=["lunes"], start="18:00", end="09:00"),
        ScheduleEntry(person="Ana", kind="available", days=["lunes"], start="basura", end="12:00"),
    )

    assert result.participants == []
    assert result.removals == []
    assert result.implied == []
