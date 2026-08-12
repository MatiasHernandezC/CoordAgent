from app.schemas import DecisionRecord, Session, TimeOption
from app.services.habitual_time import (
    anchor_habitual_phrase,
    compute_habitual_slot,
    detect_habitual_phrase,
    habitual_slot_label,
)
from app.settings import settings


def _decision(day: str, start: str, end: str) -> DecisionRecord:
    return DecisionRecord(
        option=TimeOption(
            day=day,
            start=start,
            end=end,
            available_participants=["Ana"],
            score=1,
            coverage_percent=100,
        ),
        summary="test",
    )


def test_detect_habitual_phrase():
    assert detect_habitual_phrase("a la hora de siempre")
    assert detect_habitual_phrase("Yo no puedo A LA HORA DE SIEMPRE")
    assert detect_habitual_phrase("coordinemos a la misma hora")
    assert detect_habitual_phrase("como siempre me acomoda")
    assert not detect_habitual_phrase("hola buenas tardes")
    assert not detect_habitual_phrase("yo puedo el lunes de 10 a 11")
    assert not detect_habitual_phrase("")


def test_compute_habitual_slot_modal_and_tie_breaks_to_recent():
    session = Session(title="t")
    session.decision_history = [
        _decision("lunes", "10:00", "11:00"),
        _decision("martes", "15:00", "16:00"),
        _decision("lunes", "10:00", "11:00"),
    ]
    slot = compute_habitual_slot(session)
    assert slot.day == "lunes"
    assert slot.start == "10:00"
    assert slot.end == "11:00"


def test_compute_habitual_slot_tie_prefers_most_recent():
    session = Session(title="t")
    session.decision_history = [
        _decision("lunes", "10:00", "11:00"),
        _decision("martes", "15:00", "16:00"),
    ]
    slot = compute_habitual_slot(session)
    assert slot.day == "martes"
    assert slot.start == "15:00"


def test_compute_habitual_slot_empty_history():
    assert compute_habitual_slot(Session(title="t")) is None


def test_compute_habitual_slot_min_decisions(monkeypatch):
    session = Session(title="t")
    session.decision_history = [_decision("lunes", "10:00", "11:00")]
    monkeypatch.setattr(settings, "habitual_min_decisions", 2)
    assert compute_habitual_slot(session) is None


def test_habitual_history_index_bounds_round_after_reset():
    session = Session(title="t")
    session.decision_history = [
        _decision("lunes", "10:00", "11:00"),
        _decision("lunes", "10:00", "11:00"),
    ]
    # Simula un reset: la ronda actual empieza DESPUES del historial previo,
    # de modo que las dos decisiones antiguas (aunque repetidas) no cuentan.
    session.habitual_history_index = 2
    assert compute_habitual_slot(session) is None

    session.decision_history.append(_decision("miercoles", "09:00", "10:00"))
    slot = compute_habitual_slot(session)
    assert slot.day == "miercoles"


def test_anchor_habitual_phrase_rewrites_to_literal_slot():
    slot = TimeOption(
        day="martes", start="10:00", end="11:00", available_participants=["Ana"], score=1, coverage_percent=100
    )
    assert anchor_habitual_phrase("yo no puedo a la hora de siempre", slot) == (
        "yo no puedo el martes de 10:00 a 11:00"
    )
    assert anchor_habitual_phrase("a la hora de siempre coordinamos", slot) == (
        "el martes de 10:00 a 11:00 coordinamos"
    )


def test_anchor_habitual_phrase_without_slot_is_noop():
    assert anchor_habitual_phrase("yo no puedo a la hora de siempre", None) == (
        "yo no puedo a la hora de siempre"
    )
    assert anchor_habitual_phrase("yo puedo el lunes", None) == "yo puedo el lunes"


def test_habitual_slot_label():
    slot = TimeOption(
        day="jueves", start="16:00", end="17:00", available_participants=["Ana"], score=1, coverage_percent=100
    )
    assert habitual_slot_label(slot) == "jueves 16:00-17:00"
    assert habitual_slot_label(None) is None
