"""Pruebas de arquitectura del pipeline semantico.

No miden el modelo: inyectan IR semantica como si viniera del LLM y validan que
Python compile, normalice, fusione y calcule de forma determinista en casos
dificiles. Esta suite protege la arquitectura nueva.
"""
from pathlib import Path

import pytest

import app.services.session_service as session_module
from app.schemas import ExtractedAvailability, ScheduleEntry, ScheduleInterpretation
from app.services.llm_service import parse_extraction_payload
from app.services.schedule_compiler import compile_interpretation
from app.services.session_service import SessionService
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def service(tmp_path: Path):
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return SessionService()


def slots_for(session, name: str):
    participant = next(participant for participant in session.participants if participant.name == name)
    return sorted((slot.day, slot.start, slot.end) for slot in participant.availability)


def merge_payload(service: SessionService, session_id: str, payload: dict, message: str):
    extraction = parse_extraction_payload(payload, message)
    return service.merge_extraction(session_id, extraction, message, "architecture_test")


def test_all_days_except_one_is_compiled_then_removed_in_session(service):
    session = service.create("Arquitectura - excepciones")
    payload = {
        "entries": [
            {"person": "Pedro", "kind": "available", "days": ["todos"], "start": None, "end": None},
            {"person": "Pedro", "kind": "unavailable", "days": ["viernes"], "start": None, "end": None},
        ]
    }

    session = merge_payload(service, session.id, payload, "Pedro puede cualquier dia menos el viernes")

    assert slots_for(session, "Pedro") == [
        ("jueves", "09:00", "18:00"),
        ("lunes", "09:00", "18:00"),
        ("martes", "09:00", "18:00"),
        ("miercoles", "09:00", "18:00"),
    ]
    assert "viernes" not in {day for day, _, _ in slots_for(session, "Pedro")}


def test_unavailable_middle_range_creates_available_edges(service):
    session = service.create("Arquitectura - hueco")
    payload = {
        "entries": [
            {"person": "Ana", "kind": "unavailable", "days": ["jueves"], "start": "13:00", "end": "14:00"}
        ]
    }

    session = merge_payload(service, session.id, payload, "Ana almuerza jueves de 1 a 2, fuera de eso puede")

    assert slots_for(session, "Ana") == [
        ("jueves", "09:00", "13:00"),
        ("jueves", "14:00", "18:00"),
    ]


def test_implied_never_expands_existing_explicit_day(service):
    session = service.create("Arquitectura - no expandir explicito")
    session = service.add_availability(session.id, "Elon", slot("lunes", "10:00", "12:00"))
    payload = {
        "entries": [
            {"person": "Elon", "kind": "unavailable", "days": ["lunes"], "start": "16:00", "end": "18:00"}
        ]
    }

    session = merge_payload(service, session.id, payload, "Elon no puede despues de las 4 el lunes")

    assert slots_for(session, "Elon") == [("lunes", "10:00", "12:00")]


def test_only_replaces_prior_week_availability(service):
    session = service.create("Arquitectura - only")
    session = service.add_availability(session.id, "Luisa", slot("lunes", "09:00", "18:00"))
    session = service.add_availability(session.id, "Luisa", slot("martes", "09:00", "18:00"))
    payload = {
        "entries": [
            {"person": "Luisa", "kind": "only", "days": ["viernes"], "start": "15:00", "end": "18:00"}
        ]
    }

    session = merge_payload(service, session.id, payload, "Luisa solo puede viernes despues de las 3")

    assert slots_for(session, "Luisa") == [("viernes", "15:00", "18:00")]


def test_stale_decision_is_cleared_when_pipeline_changes_availability(service):
    session = service.create("Arquitectura - decision obsoleta")
    session = service.add_availability(session.id, "Ana", slot("lunes", "10:00", "12:00"))
    session = service.add_availability(session.id, "Luis", slot("lunes", "10:00", "12:00"))
    session = service.calculate(session.id)
    session = service.confirm(session.id, session.options[0].id)
    assert session.status == "confirmed"

    payload = {
        "entries": [
            {"person": "Luis", "kind": "only", "days": ["martes"], "start": "10:00", "end": "12:00"}
        ]
    }
    session = merge_payload(service, session.id, payload, "Luis solo puede martes de 10 a 12")

    assert session.status == "calculated"
    assert session.selected_option is None
    assert session.decision_summary is None
    assert any("reconfirmacion" in message.content for message in session.messages)


def test_empty_extraction_does_not_clear_confirmed_decision(service):
    session = service.create("Arquitectura - mensaje sin datos")
    session = service.add_availability(session.id, "Ana", slot("lunes", "10:00", "12:00"))
    session = service.add_availability(session.id, "Luis", slot("lunes", "10:00", "12:00"))
    session = service.calculate(session.id)
    session = service.confirm(session.id, session.options[0].id)

    session = service.merge_extraction(
        session.id,
        ExtractedAvailability(),
        "gracias, perfecto",
        "architecture_test_no_new_availability",
    )

    assert session.status == "confirmed"
    assert session.selected_option is not None
    assert session.decision_summary is not None


def test_compiler_drops_invalid_or_out_of_domain_entries():
    result = compile_interpretation(
        ScheduleInterpretation(
            entries=[
                ScheduleEntry(person="Fantasma", kind="available", days=["sabado"], start="10:00", end="12:00"),
                ScheduleEntry(person="MalaHora", kind="available", days=["lunes"], start="18:00", end="09:00"),
                ScheduleEntry(person="", kind="available", days=["lunes"], start="10:00", end="12:00"),
            ]
        )
    )

    assert result.participants == []
    assert result.removals == []
    assert result.implied == []
    assert result.quality_flags == ["invalid_interval_discarded"]


def test_decision_engine_reflects_compiled_semantics(service):
    session = service.create("Arquitectura - decision final")
    payloads = [
        (
            "Ana puede cualquier dia menos viernes",
            {
                "entries": [
                    {"person": "Ana", "kind": "available", "days": ["todos"], "start": None, "end": None},
                    {"person": "Ana", "kind": "unavailable", "days": ["viernes"], "start": None, "end": None},
                ]
            },
        ),
        (
            "Luis no puede despues de las 4 el lunes",
            {
                "entries": [
                    {"person": "Luis", "kind": "unavailable", "days": ["lunes"], "start": "16:00", "end": "18:00"}
                ]
            },
        ),
        (
            "Camila sale de turno a las 14 el lunes",
            {
                "entries": [
                    {"person": "Camila", "kind": "available", "days": ["lunes"], "start": "14:00", "end": "18:00"}
                ]
            },
        ),
    ]

    for message, payload in payloads:
        session = merge_payload(service, session.id, payload, message)
    session = service.calculate(session.id)

    best = session.options[0]
    assert (best.day, best.start, best.end) == ("lunes", "14:00", "15:00")
    assert best.available_participants == ["Ana", "Camila", "Luis"]
    assert best.coverage_percent == 100


def slot(day: str, start: str, end: str):
    from app.schemas import TimeSlot

    return TimeSlot(day=day, start=start, end=end)  # type: ignore[arg-type]
