"""Referencias temporales relativas: "hoy", "manana", "pasado manana" deben
resolverse a un dia habil concreto, sin expandir a toda la semana. Regresion del
caso real del grupo Torneo ("manana puedo a las 5 pm" -> un solo dia)."""
from datetime import datetime, timezone as dt_tz

import pytest

from app.schemas import ChannelMessage
from app.services.llm_service import (
    LlmService,
    build_channel_extraction_text,
    build_temporal_context,
    resolve_relative_days,
)
from app.settings import settings

# Miercoles 08-07-2026, 14:00 UTC (11:00 en Santiago) -> hoy=miercoles.
WED = datetime(2026, 7, 8, 14, 0, tzinfo=dt_tz.utc)
# Viernes 10-07-2026 -> manana=sabado -> debe rodar a lunes.
FRI = datetime(2026, 7, 10, 14, 0, tzinfo=dt_tz.utc)


def test_resolve_today_tomorrow_day_after():
    assert resolve_relative_days("puedo hoy a las 5", WED) == "puedo miercoles a las 5"
    assert resolve_relative_days("manana puedo a las 5", WED) == "jueves puedo a las 5"
    assert resolve_relative_days("pasado manana libre", WED) == "viernes libre"


def test_resolve_keeps_morning_phrase_intact():
    # "en la manana" es franja horaria, NO el dia siguiente.
    assert resolve_relative_days("el lunes en la manana", WED) == "el lunes en la manana"
    assert resolve_relative_days("manana en la manana", WED) == "jueves en la manana"


def test_relative_day_rolls_weekend_to_monday():
    assert resolve_relative_days("manana juego", FRI) == "lunes juego"


def test_temporal_context_names_today_and_relatives():
    context = build_temporal_context(WED)
    assert "miercoles" in context
    assert "jueves" in context   # manana
    assert "viernes" in context  # pasado manana
    assert "todos" in context    # advertencia anti-todos


@pytest.fixture()
def mock_service(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    return LlmService()


def test_torneo_case_manana_is_single_day_not_whole_week(monkeypatch, mock_service):
    # Fija "hoy" a miercoles para que "manana"=jueves de forma determinista.
    import app.services.llm_service as llm_service
    monkeypatch.setattr(llm_service, "current_workday_name", lambda now=None: "miercoles")

    messages = [
        ChannelMessage(sender="Nicolas", text="manana puedo juntarme a las 5 pm"),
        ChannelMessage(
            sender="Nicolas",
            text="@gabo puede manana despues de las 3pm",
            mentioned_jids=["56933333333@s.whatsapp.net"],
        ),
        ChannelMessage(sender="Nicolas", text="@coordina"),
    ]
    extraction, source, _ = mock_service.extract_channel_availability(messages)

    by_name = {p.name: [(s.day, s.start, s.end) for s in p.availability] for p in extraction.participants}
    # Un solo dia (jueves), no los 5.
    assert by_name.get("Nicolas") == [("jueves", "17:00", "18:00")]
    assert by_name.get("Gabo") == [("jueves", "15:00", "18:00")]


def test_channel_transcript_resolves_relative_day(monkeypatch):
    import app.services.llm_service as llm_service
    monkeypatch.setattr(llm_service, "current_workday_name", lambda now=None: "miercoles")

    transcript = build_channel_extraction_text(
        [ChannelMessage(sender="Nico", text="manana puedo a las 5 pm")]
    )
    assert "jueves" in transcript
    assert "manana" not in transcript.lower()


def test_torneo_cross_day_range_expands_each_workday(monkeypatch, mock_service):
    import app.services.llm_service as llm_service

    monkeypatch.setattr(llm_service, "current_workday_name", lambda now=None: "lunes")
    messages = [
        ChannelMessage(
            sender="Nicolas",
            text="@gabo puede desde mañana a las 3 de la tarde hasta el jueves antes de las 12",
            mentioned_jids=["56933333333@s.whatsapp.net"],
        ),
        ChannelMessage(sender="Nicolas", text="@coordina"),
    ]

    extraction, source, _ = mock_service.extract_channel_availability(messages)
    gabo = next(participant for participant in extraction.participants if participant.name == "Gabo")

    assert source == "channel_mock"
    assert [(slot.day, slot.start, slot.end) for slot in gabo.availability] == [
        ("martes", "15:00", "18:00"),
        ("miercoles", "09:00", "18:00"),
        ("jueves", "09:00", "12:00"),
    ]
    assert "cross_day_range_normalized" in extraction.quality_flags
