"""Tests del mecanismo semantico: parser dual, merge por persona, pragmatica de
topes horarios ("no puede despues de las X", "hasta las X") en mock y en merge."""
from pathlib import Path

import pytest

from app.schemas import AvailabilityRemoval, ExtractedAvailability, ImpliedAvailability, Participant, TimeSlot
from app.services.llm_service import (
    LlmService,
    merge_missing_participants,
    parse_extraction_payload,
)
from app.services.session_service import SessionService
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def mock_service(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    return LlmService()


def _availability(extraction, name):
    participant = next(p for p in extraction.participants if p.name == name)
    return [(slot.day, slot.start, slot.end) for slot in participant.availability]


# --- Parser dual (esquema semantico nuevo + legacy) --------------------------

def test_parse_new_schema_compiles_upper_bound_case():
    # Lo que Gemini deberia responder para "Elon no puede despues de las 4 el lunes"
    payload = {
        "entries": [
            {"person": "Elon", "kind": "unavailable", "days": ["lunes"], "start": "16:00", "end": "18:00"}
        ]
    }

    extraction = parse_extraction_payload(payload, "Elon no puede despues de las 4 el lunes")

    assert [(s.day, s.start, s.end) for s in extraction.removals[0].slots] == [("lunes", "16:00", "18:00")]
    assert extraction.implied[0].participant_name == "Elon"
    assert [(s.day, s.start, s.end) for s in extraction.implied[0].slots] == [("lunes", "09:00", "16:00")]


def test_parse_legacy_schema_still_works():
    payload = {
        "participants": [
            {"name": "Camila", "availability": [{"day": "lunes", "start": "15:00", "end": "18:00"}]}
        ]
    }

    extraction = parse_extraction_payload(payload, "Camila puede lunes en la tarde")

    assert _availability(extraction, "Camila") == [("lunes", "15:00", "18:00")]


def test_parse_new_schema_grounds_hallucinated_people():
    payload = {
        "entries": [
            {"person": "Elon", "kind": "available", "days": ["lunes"]},
            {"person": "Fantasma", "kind": "available", "days": ["lunes"]},
        ]
    }

    extraction = parse_extraction_payload(payload, "Elon puede el lunes")

    assert [p.name for p in extraction.participants] == ["Elon"]


def test_parse_keeps_first_person_me_acomoda_and_applies_exception():
    payload = {
        "entries": [
            {"person": "Yo", "kind": "available", "days": ["todos"], "start": None, "end": None},
            {"person": "Yo", "kind": "unavailable", "days": ["viernes"], "start": None, "end": None},
        ]
    }

    extraction = parse_extraction_payload(payload, "me acomoda cualquier dia menos el viernes")

    yo = next(participant for participant in extraction.participants if participant.name == "Yo")
    friday_removal = next(removal for removal in extraction.removals if removal.participant_name == "Yo")
    assert sorted({slot.day for slot in yo.availability}) == ["jueves", "lunes", "martes", "miercoles", "viernes"]
    assert [(slot.day, slot.start, slot.end) for slot in friday_removal.slots] == [("viernes", "09:00", "18:00")]


def test_exclusive_override_uses_fragment_owner_after_y_connector():
    payload = {
        "entries": [
            {"person": "Ana", "kind": "available", "days": ["jueves"], "start": "09:00", "end": "12:00"},
            {"person": "Luisa", "kind": "available", "days": ["viernes"], "start": "15:00", "end": "18:00"},
        ]
    }

    extraction = parse_extraction_payload(
        payload,
        "Ana dijo que puede jueves en la manana y Luisa solo puede viernes despues de las 3",
    )

    assert _availability(extraction, "Ana") == [("jueves", "09:00", "12:00")]
    assert _availability(extraction, "Luisa") == [("viernes", "15:00", "18:00")]


def test_grounded_override_corrects_return_home_phrase():
    # Regresion local: Qwen 4B a veces interpretaba "de ahi en adelante libre"
    # como no-disponibilidad. El texto original es mas fuerte que el JSON malo.
    payload = {
        "entries": [
            {"person": "Yo", "kind": "unavailable", "days": ["miercoles"], "start": "11:00", "end": "18:00"}
        ]
    }

    extraction = parse_extraction_payload(
        payload,
        "vuelvo de la clinica tipo 11 el miercoles, de ahi en adelante libre",
    )

    assert _availability(extraction, "Yo") == [("miercoles", "11:00", "18:00")]


def test_grounded_override_corrects_third_person_return_phrase():
    payload = {
        "entries": [
            {"person": "Pedro", "kind": "unavailable", "days": ["miercoles"], "start": "11:00", "end": "18:00"}
        ]
    }

    extraction = parse_extraction_payload(
        payload,
        "Pedro vuelve de la clinica tipo 11 el miercoles, de ahi en adelante libre",
    )

    assert _availability(extraction, "Pedro") == [("miercoles", "11:00", "18:00")]


# --- Merge por persona (el mock nunca pisa la interpretacion del LLM) ---------

def test_merge_missing_participants_never_expands_llm_bounds():
    llm = ExtractedAvailability(
        participants=[Participant(name="Elon", availability=[TimeSlot(day="lunes", start="09:00", end="16:00")])]
    )
    mock = ExtractedAvailability(
        participants=[
            Participant(name="Elon", availability=[TimeSlot(day="lunes", start="16:00", end="18:00")]),
            Participant(name="Pedro", availability=[TimeSlot(day="martes", start="09:00", end="18:00")]),
        ]
    )

    merged = merge_missing_participants(llm, mock)

    # Elon conserva el tope del LLM; Pedro (omitido por el LLM) se agrega.
    assert _availability(merged, "Elon") == [("lunes", "09:00", "16:00")]
    assert _availability(merged, "Pedro") == [("martes", "09:00", "18:00")]


def test_merge_missing_participants_respects_llm_removals_identity():
    llm = ExtractedAvailability(removals=[AvailabilityRemoval(participant_name="Elon", slots=[TimeSlot(day="lunes", start="16:00", end="18:00")])])
    mock = ExtractedAvailability(
        participants=[Participant(name="Elon", availability=[TimeSlot(day="lunes", start="16:00", end="18:00")])]
    )

    merged = merge_missing_participants(llm, mock)

    assert merged.participants == []  # el mock no agrega a Elon: el LLM ya lo interpreto


# --- Mock: topes horarios ------------------------------------------------------

def test_mock_understands_positive_until_bound(mock_service):
    extraction, source, _ = mock_service.extract_availability("Elon esta libre el lunes hasta las 4")

    assert source == "mock"
    assert _availability(extraction, "Elon") == [("lunes", "09:00", "16:00")]


def test_mock_understands_pero_no_despues_bound(mock_service):
    extraction, _, _ = mock_service.extract_availability("Elon puede el lunes pero no despues de las 4")

    assert _availability(extraction, "Elon") == [("lunes", "09:00", "16:00")]


def test_mock_negative_after_hour_emits_removal_plus_implied(mock_service):
    extraction, _, _ = mock_service.extract_availability("Elon no puede despues de las 4 el lunes")

    assert [(s.day, s.start, s.end) for s in extraction.removals[0].slots] == [("lunes", "16:00", "18:00")]
    assert [(s.day, s.start, s.end) for s in extraction.implied[0].slots] == [("lunes", "09:00", "16:00")]


# --- Cache: los fallbacks no se cachean ----------------------------------------

def test_fallback_results_are_not_cached(monkeypatch):
    # Regresion: un 503 pasajero de Gemini dejaba pegada la interpretacion del
    # mock en cache; el proximo intento debe volver a intentar con el LLM real.
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    service = LlmService()

    _, first_source, _ = service.extract_availability("Camila puede lunes en la tarde")
    _, second_source, _ = service.extract_availability("Camila puede lunes en la tarde")

    assert first_source.startswith("mock_fallback_gemini")
    assert not second_source.endswith("_cache")
    assert service._cache == {}


# --- Merge de sesion: la pragmatica nunca amplia lo explicito ------------------

def _service(tmp_path: Path) -> SessionService:
    import app.services.session_service as session_module

    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return SessionService()


def test_implied_fills_empty_day(tmp_path):
    service = _service(tmp_path)
    session = service.create("implied")

    extraction = ExtractedAvailability(
        removals=[AvailabilityRemoval(participant_name="Elon", slots=[TimeSlot(day="lunes", start="16:00", end="18:00")])],
        implied=[ImpliedAvailability(participant_name="Elon", slots=[TimeSlot(day="lunes", start="09:00", end="16:00")])],
    )
    session = service.merge_extraction(session.id, extraction, "Elon no puede despues de las 4 el lunes", "mock")

    elon = next(p for p in session.participants if p.name == "Elon")
    assert [(s.day, s.start, s.end) for s in elon.availability] == [("lunes", "09:00", "16:00")]


def test_implied_does_not_expand_explicit_availability(tmp_path):
    service = _service(tmp_path)
    session = service.create("implied-existente")
    session = service.add_availability(session.id, "Elon", TimeSlot(day="lunes", start="10:00", end="12:00"))

    extraction = ExtractedAvailability(
        removals=[AvailabilityRemoval(participant_name="Elon", slots=[TimeSlot(day="lunes", start="16:00", end="18:00")])],
        implied=[ImpliedAvailability(participant_name="Elon", slots=[TimeSlot(day="lunes", start="09:00", end="16:00")])],
    )
    session = service.merge_extraction(session.id, extraction, "Elon no puede despues de las 4 el lunes", "mock")

    elon = next(p for p in session.participants if p.name == "Elon")
    # Mantiene su 10-12 explicito: la pragmatica no lo convierte en 09-16.
    assert [(s.day, s.start, s.end) for s in elon.availability] == [("lunes", "10:00", "12:00")]
