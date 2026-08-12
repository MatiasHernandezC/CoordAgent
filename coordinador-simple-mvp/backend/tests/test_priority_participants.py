from app.schemas import Participant, Session, TimeSlot
from app.services.decision_engine import build_availability_matrix, options_from_matrix
from app.services.session_service import (
    confirmation_blockers,
    session_service,
)


def _slot(day: str, start: str = "09:00", end: str = "12:00") -> TimeSlot:
    return TimeSlot(day=day, start=start, end=end)


def test_regression_same_ranking_without_priorities():
    session = Session(
        title="t",
        participants=[
            Participant(name="Ana", availability=[_slot("lunes", "09:00", "12:00")]),
            Participant(name="Beto", availability=[_slot("lunes", "10:00", "12:00")]),
            Participant(name="Cata", availability=[_slot("martes", "09:00", "12:00")]),
        ],
    )
    options = options_from_matrix(build_availability_matrix(session))
    for option in options:
        assert option.weighted_score == option.score
    # Orden esperado: mejor bloque del lunes (2) antes que martes (1).
    assert options[0].day == "lunes"
    assert options[0].score == 2


def test_required_participants_filter_options():
    session = Session(
        title="t",
        participants=[
            Participant(name="Jefe", required=True, availability=[_slot("lunes", "10:00", "12:00")]),
            Participant(name="Ana", availability=[_slot("lunes", "09:00", "12:00")]),
        ],
    )
    options = options_from_matrix(build_availability_matrix(session))
    # Solo se recomiendan bloques que incluyen al Jefe (>=10:00).
    assert all(option.required_met for option in options)
    assert all("Jefe" in option.available_participants for option in options)


def test_required_fallback_when_no_option_meets_all():
    session = Session(
        title="t",
        participants=[
            Participant(name="Jefe", required=True),
            Participant(name="Ana", availability=[_slot("lunes", "09:00", "12:00")]),
        ],
    )
    options = options_from_matrix(build_availability_matrix(session))
    assert options
    assert all(not option.required_met for option in options)
    assert options[0].required_missing == ["Jefe"]


def test_priority_weight_reorders_top_option():
    session = Session(
        title="t",
        participants=[
            Participant(name="Jefe", priority=5, availability=[_slot("martes", "10:00", "11:00")]),
            Participant(name="Ana", availability=[_slot("lunes", "09:00", "12:00")]),
            Participant(name="Beto", availability=[_slot("lunes", "09:00", "12:00")]),
        ],
    )
    options = options_from_matrix(build_availability_matrix(session))
    # Martes (Jefe con peso 5) supera al lunes (Ana+Beto con peso 2).
    assert options[0].day == "martes"
    assert options[0].weighted_score == 5


def test_required_confirmation_blocker():
    session = Session(
        title="t",
        participants=[
            Participant(name="Jefe", required=True),
            Participant(name="Ana", availability=[_slot("lunes", "09:00", "12:00")]),
        ],
    )
    session.availability_matrix = build_availability_matrix(session)
    session.options = options_from_matrix(session.availability_matrix)
    session.status = "calculated"
    blockers = confirmation_blockers(session)
    assert any("requeridos" in blocker for blocker in blockers)


def test_configure_participant_requirements(monkeypatch):
    session = Session(
        title="t",
        participants=[
            Participant(name="Ana", availability=[_slot("lunes")]),
        ],
    )
    repository_calls = []

    class FakeRepo:
        def get(self, _id):
            return session

        def save(self, s):
            repository_calls.append(1)
            return s

        def list_all(self):
            return [session]

        def healthcheck(self):
            return None

    from app.services import session_service as svc

    monkeypatch.setattr(svc, "repository", FakeRepo())
    updated = svc.session_service.configure_participant_requirements(
        session.id,
        "Ana",
        required=True,
        priority=3,
    )
    assert updated.participants[0].required is True
    assert updated.participants[0].priority == 3
    assert repository_calls

    svc.session_service.configure_participant_requirements(session.id, "Ana", required=False, priority=0)
    assert session.participants[0].required is False
    assert session.participants[0].priority == 0
