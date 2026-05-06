from pathlib import Path

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


def slot_from(day: str, start: str, end: str):
    from app.schemas import TimeSlot

    return TimeSlot(day=day, start=start, end=end)  # type: ignore[arg-type]
