from pathlib import Path

from app.schemas import AvailabilityRemoval, ExtractedAvailability
from app.services.session_service import SessionService
from app.storage.json_repository import JsonRepository


def test_confirm_builds_human_summary(tmp_path: Path):
    service = SessionService()
    original_repository = __import__("app.services.session_service", fromlist=["repository"])
    original_repository.repository = JsonRepository(tmp_path / "sessions.json")

    session = service.create("Reunion demo")
    session = service.add_availability(session.id, "Camila", slot_from("lunes", "15:00", "18:00"))
    session = service.add_availability(session.id, "Diego", slot_from("lunes", "16:00", "18:00"))
    session = service.calculate(session.id)
    session = service.confirm(session.id, session.options[0].id)

    assert session.status == "confirmed"
    assert session.decision_summary is not None
    assert "decision confirmada" in session.decision_summary


def test_merge_extraction_removes_overlapping_availability(tmp_path: Path):
    service = SessionService()
    original_repository = __import__("app.services.session_service", fromlist=["repository"])
    original_repository.repository = JsonRepository(tmp_path / "sessions.json")

    session = service.create("Remocion demo")
    session = service.add_availability(session.id, "Nicolas", slot_from("lunes", "15:00", "18:00"))
    session = service.merge_extraction(
        session.id,
        ExtractedAvailability(
            removals=[
                AvailabilityRemoval(
                    participant_name="Nicolas",
                    slots=[slot_from("lunes", "16:00", "18:00")],
                )
            ]
        ),
        "Nicolas ya no puede lunes desde las 16",
        "mock",
    )

    nicolas = next(participant for participant in session.participants if participant.name == "Nicolas")

    assert [(slot.day, slot.start, slot.end) for slot in nicolas.availability] == [("lunes", "15:00", "16:00")]


def test_full_day_removal_keeps_participant_as_missing_info(tmp_path: Path):
    service = SessionService()
    original_repository = __import__("app.services.session_service", fromlist=["repository"])
    original_repository.repository = JsonRepository(tmp_path / "sessions.json")

    session = service.create("Remocion total")
    session = service.add_availability(session.id, "Nicolas", slot_from("lunes", "15:00", "18:00"))
    session = service.merge_extraction(
        session.id,
        ExtractedAvailability(
            removals=[
                AvailabilityRemoval(
                    participant_name="Nicolas",
                    slots=[slot_from("lunes", "09:00", "18:00")],
                )
            ]
        ),
        "Nicolas ya no puede lunes a ninguna hora",
        "mock",
    )

    nicolas = next(participant for participant in session.participants if participant.name == "Nicolas")

    assert nicolas.availability == []
    assert "Falta disponibilidad de Nicolas." in session.missing_info


def test_removal_without_slots_does_not_clear_existing_availability(tmp_path: Path):
    service = SessionService()
    original_repository = __import__("app.services.session_service", fromlist=["repository"])
    original_repository.repository = JsonRepository(tmp_path / "sessions.json")

    session = service.create("Remocion ambigua")
    session = service.add_availability(session.id, "Camila", slot_from("lunes", "16:00", "18:00"))
    session = service.merge_extraction(
        session.id,
        ExtractedAvailability(
            removals=[
                AvailabilityRemoval(
                    participant_name="Camila",
                    slots=[],
                )
            ]
        ),
        "Camila no puede",
        "mock",
    )

    camila = next(participant for participant in session.participants if participant.name == "Camila")

    assert [(slot.day, slot.start, slot.end) for slot in camila.availability] == [("lunes", "16:00", "18:00")]


def test_session_without_schema_version_defaults_to_1():
    # Retrocompatibilidad: una sesion persistida antes de versionar el esquema
    # (sin el campo schema_version) debe seguir validando con el default.
    from app.schemas import Session

    session = Session.model_validate({"title": "Sesion antigua sin version"})

    assert session.schema_version == 1


def slot_from(day: str, start: str, end: str):
    from app.schemas import TimeSlot

    return TimeSlot(day=day, start=start, end=end)  # type: ignore[arg-type]
