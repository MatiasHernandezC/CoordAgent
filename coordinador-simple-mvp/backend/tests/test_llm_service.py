import pytest

from app.schemas import AvailabilityRemoval, ExtractedAvailability
from app.services.llm_service import (
    LlmUnavailableError,
    LlmService,
    build_channel_extraction_text,
    estimate_gemini_cost,
    extract_gemini_text,
    normalize_llm_extraction,
    parse_retry_delay_seconds,
)
from app.settings import settings
from app.schemas import ChannelMessage


def test_mock_extractor_expands_all_days_availability():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability(
            "Yo puedo lunes en la tarde, Camila puede lunes desde las 16 y Diego puede martes en la manana, Pedro puede a cualquier hora todos los dias"
        )
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    pedro = next(participant for participant in extraction.participants if participant.name == "Pedro")

    assert source == "mock"
    assert token_usage is None
    assert [slot.day for slot in pedro.availability] == ["lunes", "martes", "miercoles", "jueves", "viernes"]
    assert all(slot.start == "09:00" and slot.end == "18:00" for slot in pedro.availability)


def test_llm_cache_reuses_repeated_extractions():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = True
        service = LlmService()
        _, first_source, first_usage = service.extract_availability("Camila puede lunes en la tarde")
        _, second_source, second_usage = service.extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    assert first_source == "mock"
    assert second_source == "mock_cache"
    assert first_usage is None
    assert second_usage is None


def test_mock_extractor_detects_availability_removal():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Nicolas ya no puede lunes a ninguna hora")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    assert source == "mock"
    assert token_usage is None
    assert extraction.removals[0].participant_name == "Nicolas"
    assert extraction.removals[0].slots[0].day == "lunes"
    assert extraction.removals[0].slots[0].start == "09:00"
    assert extraction.removals[0].slots[0].end == "18:00"


def test_channel_rewrites_first_person_future_negative_to_sender():
    transcript = build_channel_extraction_text(
        [
            ChannelMessage(
                sender="Nicolas",
                text="yo no podre nigun dia a las finales se me enfermo el gato",
            )
        ]
    )

    assert "Nicolas no puede" in transcript
    assert "yo no podre" not in transcript.lower()


def test_channel_rewrites_first_person_future_positive_without_yo_to_sender():
    transcript = build_channel_extraction_text(
        [
            ChannelMessage(
                sender="Nicolas",
                text="podre el lunes a las 5 pm",
            )
        ]
    )

    assert "Nicolas puede el lunes a las 5 pm" in transcript
    assert "podre el lunes" not in transcript.lower()


def test_mock_extractor_treats_exact_hour_as_one_hour_slot():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Elon puede el martes a las 11")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    elon = next(participant for participant in extraction.participants if participant.name == "Elon")

    assert source == "mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in elon.availability] == [("martes", "11:00", "12:00")]


def test_local_provider_falls_back_to_mock_when_script_is_missing(tmp_path):
    original_provider = settings.llm_provider
    original_script = settings.local_llm_script
    original_fallback = settings.llm_fallback_enabled
    try:
        settings.llm_provider = "local"
        settings.local_llm_script = tmp_path / "missing_local_llm.py"
        settings.llm_fallback_enabled = True
        extraction, source, token_usage = LlmService().extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.local_llm_script = original_script
        settings.llm_fallback_enabled = original_fallback

    assert source == "mock_fallback_local_qwen"
    assert token_usage is None
    assert extraction.participants[0].name == "Camila"


def test_gemini_text_extraction_reads_candidate_parts():
    raw = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": '{"participants":[{"name":"Camila","availability":[{"day":"lunes","start":"15:00","end":"18:00"}]}]}'
                        }
                    ]
                }
            }
        ]
    }

    assert '"participants"' in extract_gemini_text(raw)


def test_gemini_provider_falls_back_without_api_key():
    original_provider = settings.llm_provider
    original_key = settings.gemini_api_key
    original_fallback = settings.llm_fallback_enabled
    try:
        settings.llm_provider = "gemini"
        settings.gemini_api_key = ""
        settings.llm_fallback_enabled = True
        extraction, source, token_usage = LlmService().extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.gemini_api_key = original_key
        settings.llm_fallback_enabled = original_fallback

    assert source == f"mock_fallback_gemini_{settings.gemini_model}"
    assert token_usage is None
    assert extraction.participants[0].name == "Camila"


def test_gemini_cooldown_without_fallback_raises_controlled_error():
    original_provider = settings.llm_provider
    original_fallback = settings.llm_fallback_enabled
    try:
        settings.llm_provider = "gemini"
        settings.llm_fallback_enabled = False
        service = LlmService()
        service._block_gemini(2, "test")

        with pytest.raises(LlmUnavailableError) as error:
            service.extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.llm_fallback_enabled = original_fallback

    assert error.value.status_code == 429
    assert error.value.retry_after_seconds is not None
    assert "Gemini esta temporalmente pausado" in error.value.message


def test_gemini_cost_estimation_uses_configured_prices():
    original_input_price = settings.gemini_input_price_per_million
    original_output_price = settings.gemini_output_price_per_million
    try:
        settings.gemini_input_price_per_million = 0.10
        settings.gemini_output_price_per_million = 0.40
        assert estimate_gemini_cost(1000, 500) == 0.0003
    finally:
        settings.gemini_input_price_per_million = original_input_price
        settings.gemini_output_price_per_million = original_output_price


def test_retry_delay_is_read_from_gemini_quota_error():
    detail = '{"error":{"details":[{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"3s"}]}}'

    assert parse_retry_delay_seconds(detail) == 3


def test_first_person_llm_output_with_invalid_name_is_normalized():
    extraction = ExtractedAvailability(
        removals=[
            AvailabilityRemoval(
                participant_name="el",
                slots=[
                    {
                        "day": "lunes",
                        "start": "09:00",
                        "end": "21:00",
                    }
                ],
            )
        ]
    )

    normalized = normalize_llm_extraction(
        extraction,
        "el lunes tendre que estar fuera de mi casa asi que no creo pueda al menos hasta las 9",
    )

    assert normalized.removals[0].participant_name == "Yo"
