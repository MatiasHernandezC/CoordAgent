from app.schemas import Participant, Session, TimeSlot
from app.services.decision_engine import calculate_options, find_missing_info


def test_calculates_best_overlap():
    session = Session(
        title="Demo",
        participants=[
            Participant(name="Camila", availability=[TimeSlot(day="lunes", start="15:00", end="18:00")]),
            Participant(name="Diego", availability=[TimeSlot(day="lunes", start="16:00", end="18:00")]),
        ],
    )

    options = calculate_options(session)

    assert options[0].day == "lunes"
    assert options[0].start == "16:00"
    assert options[0].score == 2
    assert options[0].coverage_percent == 100
    assert options[0].unavailable_participants == []
    assert "Todos los participantes" in options[0].explanation


def test_marks_unavailable_participants_in_options():
    session = Session(
        title="Demo",
        participants=[
            Participant(name="Camila", availability=[TimeSlot(day="lunes", start="15:00", end="18:00")]),
            Participant(name="Diego", availability=[TimeSlot(day="martes", start="09:00", end="12:00")]),
        ],
    )

    options = calculate_options(session)

    assert options[0].score == 1
    assert len(options[0].unavailable_participants) == 1
    assert "Cobertura 50%" in options[0].explanation


def test_finds_missing_participant_availability():
    session = Session(
        title="Demo",
        participants=[
            Participant(name="Camila", availability=[TimeSlot(day="lunes", start="15:00", end="18:00")]),
            Participant(name="Diego"),
        ],
    )

    assert find_missing_info(session) == ["Falta disponibilidad de Diego."]


def test_options_are_diversified_across_days():
    # El lunes tiene tres bloques igual de buenos; el motor no debe ofrecer las tres
    # horas del lunes, sino a lo mas una por dia para dar alternativas reales.
    session = Session(
        title="Diversidad",
        participants=[
            Participant(
                name="Ana",
                availability=[
                    TimeSlot(day="lunes", start="15:00", end="18:00"),
                    TimeSlot(day="martes", start="09:00", end="10:00"),
                ],
            ),
            Participant(name="Beto", availability=[TimeSlot(day="lunes", start="15:00", end="18:00")]),
        ],
    )

    options = calculate_options(session)
    days = [option.day for option in options]

    assert len(days) == len(set(days))  # ningun dia se repite
    assert "martes" in days  # aparece el segundo dia en vez de otra hora del lunes
    assert options[0].day == "lunes" and options[0].score == 2  # la mejor sigue siendo el lunes
