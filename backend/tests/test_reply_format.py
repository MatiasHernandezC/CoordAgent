"""Tests de la respuesta del coordinador: texto profesional, seleccion de formato
y generacion de la imagen del calendario."""
import io

from PIL import Image

from app.schemas import ChannelMessage, Participant, Session, TimeSlot
from app.services.decision_engine import build_availability_matrix, options_from_matrix
from app.services.image_render import (
    hours_for_session,
    layout_for_hours,
    render_availability_png,
)
from app.services.session_service import (
    build_channel_reply,
    resolve_reply_format,
)


def _session_with_options() -> Session:
    session = Session(
        title="Demo",
        participants=[
            Participant(name="Ana", availability=[TimeSlot(day="lunes", start="15:00", end="18:00")]),
            Participant(name="Beto", availability=[TimeSlot(day="lunes", start="16:00", end="18:00")]),
        ],
    )
    session.availability_matrix = build_availability_matrix(session)
    session.options = options_from_matrix(session.availability_matrix)
    return session


def test_professional_reply_uses_whatsapp_formatting():
    reply = build_channel_reply(_session_with_options())

    assert reply.startswith("*Coordina")
    assert "*Mejor opcion*" in reply
    assert "lunes" in reply.lower()
    assert "% de cobertura" in reply


def test_reply_without_participants_invites_to_write():
    reply = build_channel_reply(Session(title="vacia"))

    assert "*Coordina*" in reply
    assert "disponibilidades" in reply.lower()


def test_render_availability_png_is_a_valid_image():
    session = _session_with_options()
    png = render_availability_png(session)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    hours = hours_for_session(session)
    expected_h = layout_for_hours(len(hours))["height"]
    assert image.size == (1125, expected_h)
    assert 500 <= image.size[1] <= 1600
    colors = image.convert("RGB").getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) > 20
    assert len(png) < 400_000


def test_render_expands_to_cover_evening_hours():
    """Si hay slots de noche, la grilla debe incluirlos (no quedarse en 09-17)."""
    session = Session(
        title="Noche",
        participants=[
            Participant(name="Ana", availability=[TimeSlot(day="lunes", start="19:00", end="21:00")]),
            Participant(name="Beto", availability=[TimeSlot(day="lunes", start="20:00", end="22:00")]),
        ],
    )
    session.channel_config.workday_start_hour = 9
    session.channel_config.workday_end_hour = 22
    session.availability_matrix = build_availability_matrix(session)
    session.options = options_from_matrix(session.availability_matrix)

    hours = hours_for_session(session)
    assert 9 in hours
    assert 20 in hours
    assert 21 in hours
    assert hours[-1] >= 20

    png = render_availability_png(session)
    image = Image.open(io.BytesIO(png))
    assert image.size[0] == 1125
    # Mas filas => mas alto que el layout de 9 horas tipicas, sin pasar el tope.
    assert image.size[1] > layout_for_hours(9)["height"]
    assert image.size[1] <= 1600
    assert len(png) < 500_000


def test_replies_include_concrete_dates_and_calendar_link():
    from datetime import datetime, timezone as dt_tz

    from app.services.session_service import build_confirmed_channel_reply

    # Miercoles 08-07-2026 10:00 en Santiago -> proximo lunes es el 13/07.
    now = datetime(2026, 7, 8, 14, 0, tzinfo=dt_tz.utc)
    session = _session_with_options()

    reply = build_channel_reply(session, now=now)
    assert "Lunes 13/07" in reply

    session.selected_option = session.options[0]
    confirmed = build_confirmed_channel_reply(session, now=now)
    assert "13 de julio" in confirmed
    assert "calendar.google.com/calendar/render" in confirmed
    assert "20260713T160000%2F20260713T170000" in confirmed or "20260713T160000/20260713T170000" in confirmed


def test_option_event_date_skips_to_next_week_when_hour_passed():
    from datetime import datetime, timezone as dt_tz

    from app.services.calendar_export import option_event_date

    session = _session_with_options()
    best = session.options[0]  # lunes 16:00
    # Lunes 13-07-2026 17:30 en Santiago (21:30 UTC): la hora ya paso -> siguiente lunes.
    now = datetime(2026, 7, 13, 21, 30, tzinfo=dt_tz.utc)

    assert option_event_date(best, now).isoformat() == "2026-07-20"


def _session_with_invocation(text: str, default: str = "both") -> Session:
    session = Session(title="fmt")
    session.channel_config.reply_format = default
    session.channel_messages = [
        ChannelMessage(sender="Nico", text=text, kind="human", detected_invocation=True)
    ]
    return session


def test_reply_format_defaults_to_channel_config():
    assert resolve_reply_format(_session_with_invocation("@coordina", default="both")) == "both"
    assert resolve_reply_format(_session_with_invocation("@coordina", default="text")) == "text"


def test_reply_format_keyword_overrides_default():
    assert resolve_reply_format(_session_with_invocation("@coordina solo texto")) == "text"
    assert resolve_reply_format(_session_with_invocation("@coordina sin imagen")) == "text"
    assert resolve_reply_format(_session_with_invocation("@coordina con imagen porfa")) == "both"
    assert resolve_reply_format(_session_with_invocation("@coordina mandame el calendario")) == "both"
    # Con tildes tambien (grafico / gráfico)
    assert resolve_reply_format(_session_with_invocation("@coordina solo el gráfico")) == "image"
