from app.schemas import (
    ExtractedAvailability,
    Participant,
    TimeSlot,
)
from app.services.llm_service import (
    LlmService,
    extract_json,
    normalize_llm_extraction,
    parse_extraction_payload,
)
from app.services.temporal_grounding import (
    detect_active_round_days,
    validate_extracted_temporal_grounding,
)


def _extraction_with_day(day: str, start: str = "15:00", end: str = "18:00") -> ExtractedAvailability:
    return ExtractedAvailability(
        participants=[
            Participant(
                name="Yo",
                availability=[TimeSlot(day=day, start=start, end=end)],  # type: ignore[arg-type]
            )
        ]
    )


def test_rejects_llm_day_not_present_in_message():
    # No day and no time anchor: LLM-invented day must be stripped.
    extraction = _extraction_with_day("lunes")
    result = validate_extracted_temporal_grounding(
        extraction,
        "hay que juntarse pronto",
    )
    assert result.grounding_source == "rejected_model_inference"
    assert result.rejected_days == ["lunes"]
    assert result.extraction.participants[0].availability == []
    assert "rejected_model_day_inference" in result.extraction.quality_flags


def test_accepts_day_from_explicit_active_round():
    extraction = _extraction_with_day("jueves")
    result = validate_extracted_temporal_grounding(
        extraction,
        "Yo despues de las 18.",
        active_round_context="Estamos coordinando para el jueves.",
    )
    assert result.grounding_source == "active_round"
    assert "jueves" in result.allowed_days
    assert result.extraction.participants[0].availability[0].day == "jueves"
    assert "rejected_model_day_inference" not in result.extraction.quality_flags


def test_does_not_use_past_decision_as_current_day():
    # Historical narrative must not ground "martes" (no time → no today default).
    past_only = validate_extracted_temporal_grounding(
        _extraction_with_day("martes"),
        "hay que juntarse esta semana",
        active_round_context="La reunion anterior fue el martes a las 18:00.",
    )
    assert past_only.extraction.participants[0].availability == []
    assert "martes" in past_only.rejected_days

    message_result = validate_extracted_temporal_grounding(
        ExtractedAvailability(
            participants=[
                Participant(
                    name="Yo",
                    availability=[
                        TimeSlot(day="martes", start="18:00", end="19:00"),
                        TimeSlot(day="viernes", start="17:00", end="18:00"),
                    ],
                )
            ]
        ),
        "Esta semana solo puedo el viernes a las 17.",
        active_round_context="La reunion anterior fue el martes.",
    )
    kept = {slot.day for p in message_result.extraction.participants for slot in p.availability}
    assert kept == {"viernes"}
    assert "martes" in message_result.rejected_days


def test_time_only_message_defaults_to_today_workday():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    wednesday = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo("America/Santiago"))
    extraction = _extraction_with_day("lunes", start="19:00", end="20:00")
    result = validate_extracted_temporal_grounding(
        extraction,
        "puedo a las 7",
        now=wednesday,
    )
    assert "temporal_time_defaults_today" in result.flags
    assert result.extraction.participants[0].availability
    assert result.extraction.participants[0].availability[0].day == "miercoles"
    assert result.extraction.participants[0].availability[0].start == "19:00"


def test_time_only_mock_extract_has_today_slots():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import patch

    wednesday = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo("America/Santiago"))
    with patch("app.services.llm_service._timezone_now", return_value=wednesday):
        extraction, source, _ = LlmService().extract_availability("yo puedo a las 7")
    assert "mock" in source
    days = {slot.day for p in extraction.participants for slot in p.availability}
    assert "miercoles" in days


def test_detect_active_round_days_from_coordination_phrase():
    assert "jueves" in detect_active_round_days("Estamos coordinando para el jueves")
    assert detect_active_round_days("La reunion anterior fue el martes") == set()
    assert "jueves" in detect_active_round_days("coordinar para el jueves")


def test_mock_extract_with_active_round_keeps_day():
    extraction, _, _ = LlmService().extract_availability(
        "Yo despues de las 15.",
        active_round_context="Estamos coordinando para el jueves.",
        active_round_days=["jueves"],
    )
    # Mock rules need a day in fragment for slots; active round does not invent slots from rules.
    # Grounding accepts jueves only if present in extraction. Simulate LLM inventing jueves:
    grounded = validate_extracted_temporal_grounding(
        _extraction_with_day("jueves"),
        "Yo despues de las 15.",
        active_round_days=["jueves"],
    )
    assert grounded.extraction.participants[0].availability[0].day == "jueves"
