from app.services.llm_service import LlmService, extract_gemini_text
from app.settings import settings


def test_mock_extractor_expands_all_days_availability():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source = service.extract_availability(
            "Yo puedo lunes en la tarde, Camila puede lunes desde las 16 y Diego puede martes en la manana, Pedro puede a cualquier hora todos los dias"
        )
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    pedro = next(participant for participant in extraction.participants if participant.name == "Pedro")

    assert source == "mock"
    assert [slot.day for slot in pedro.availability] == ["lunes", "martes", "miercoles", "jueves", "viernes"]
    assert all(slot.start == "09:00" and slot.end == "18:00" for slot in pedro.availability)


def test_llm_cache_reuses_repeated_extractions():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = True
        service = LlmService()
        _, first_source = service.extract_availability("Camila puede lunes en la tarde")
        _, second_source = service.extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    assert first_source == "mock"
    assert second_source == "mock_cache"


def test_local_provider_falls_back_to_mock_when_script_is_missing(tmp_path):
    original_provider = settings.llm_provider
    original_script = settings.local_llm_script
    try:
        settings.llm_provider = "local"
        settings.local_llm_script = tmp_path / "missing_local_llm.py"
        extraction, source = LlmService().extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.local_llm_script = original_script

    assert source == "mock_fallback_local_qwen"
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
    try:
        settings.llm_provider = "gemini"
        settings.gemini_api_key = ""
        extraction, source = LlmService().extract_availability("Camila puede lunes en la tarde")
    finally:
        settings.llm_provider = original_provider
        settings.gemini_api_key = original_key

    assert source == f"mock_fallback_gemini_{settings.gemini_model}"
    assert extraction.participants[0].name == "Camila"
