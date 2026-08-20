"""El evento de calendario debe referenciar el grupo de origen y (para Google
Calendar conectado) recibir un color estable por grupo."""

from app.schemas import ChannelConfig, Participant, Session, TimeOption
from app.services.calendar_export import event_description, event_title


def _session(group_name: str | None, title: str = "Reunion") -> Session:
    session = Session(title=title, channel_config=ChannelConfig(group_name=group_name))
    session.participants = [Participant(name="Camila"), Participant(name="Nicolas")]
    option = TimeOption(
        day="lunes",
        start="09:00",
        end="10:00",
        available_participants=["Camila"],
        unavailable_participants=["Nicolas"],
        score=1,
        coverage_percent=50,
    )
    session.options = [option]
    session.selected_option = option
    return session


def test_event_title_references_group_name():
    session = _session(group_name="Finanzas")
    assert event_title(session) == "Reunion de Finanzas"


def test_event_title_falls_back_to_session_title_without_group():
    session = _session(group_name=None, title="Planificacion TAVI")
    assert event_title(session) == "Planificacion TAVI"


def test_event_description_mentions_group_and_attendance():
    session = _session(group_name="Finanzas")
    description = event_description(session, session.selected_option)
    assert "Finanzas" in description
    assert "Camila" in description
    assert "Nicolas" in description
    assert "50%" in description
