import json

import pytest

from app.schemas import ExtractedAvailability, Participant, TimeSlot
from app.services.llm_service import LlmService, extract_json, parse_extraction_payload


def test_json_fenced_response_is_parsed():
    content = """```json
{"entries":[{"person":"Ana","kind":"available","days":["jueves"],"start":"15:00","end":"18:00","week_offset":0}]}
```"""
    raw = extract_json(content)
    parsed = json.loads(raw)
    extraction = parse_extraction_payload(parsed, "Ana puede el jueves de 15 a 18")
    assert extraction.participants
    assert extraction.participants[0].name == "Ana"
    assert extraction.participants[0].availability


def test_invalid_json_uses_single_retry(monkeypatch):
    service = LlmService()
    calls = {"n": 0}

    def fake_raw(prompt_text, api_key, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"candidates": [{"content": {"parts": [{"text": "no es json"}]}}], "usageMetadata": {}}, 10
        good = {
            "entries": [
                {
                    "person": "Ana",
                    "kind": "available",
                    "days": ["jueves"],
                    "start": "15:00",
                    "end": "16:00",
                    "week_offset": 0,
                }
            ]
        }
        return {
            "candidates": [{"content": {"parts": [{"text": json.dumps(good)}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
        }, 20

    monkeypatch.setattr(service, "_call_gemini_raw", fake_raw)
    extraction, usage = service._extract_with_gemini(
        "Ana puede el jueves a las 15",
        9,
        18,
        api_key="AIza-test-only-key",
    )
    assert calls["n"] == 2
    assert extraction.participants
    assert usage.total_tokens == 2


def test_invalid_json_falls_back_safely(monkeypatch):
    service = LlmService()

    def always_bad(prompt_text, api_key, model=None):
        return {"candidates": [{"content": {"parts": [{"text": "{{{not-json"}]}}], "usageMetadata": {}}, 5

    monkeypatch.setattr(service, "_call_gemini_raw", always_bad)
    with pytest.raises(ValueError, match="JSON invalido tras reintento"):
        service._extract_with_gemini(
            "Ana puede el jueves a las 15",
            9,
            18,
            api_key="AIza-test-only-key",
        )


def test_parser_does_not_accept_semantically_invalid_payload():
    with pytest.raises(Exception):
        parse_extraction_payload({"entries": "not-a-list"}, "Ana puede el jueves")


def test_extract_json_rejects_missing_object():
    with pytest.raises(ValueError):
        extract_json("sin objeto")


def test_model_fallback_used_after_primary_404(monkeypatch):
    from app.services.llm_service import GeminiApiError

    service = LlmService()
    models_seen: list[str] = []

    def fake_raw(prompt_text, api_key, model=None):
        models_seen.append(model or "default")
        if (model or "").endswith("flash-lite") and "latest" not in (model or ""):
            raise GeminiApiError(
                404,
                '{"error":{"message":"This model is no longer available to new users","status":"NOT_FOUND"}}',
            )
        good = {
            "entries": [
                {
                    "person": "Ana",
                    "kind": "available",
                    "days": ["jueves"],
                    "start": "15:00",
                    "end": "16:00",
                    "week_offset": 0,
                }
            ]
        }
        return {
            "candidates": [{"content": {"parts": [{"text": json.dumps(good)}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
        }, 12

    monkeypatch.setattr(service, "_call_gemini_raw", fake_raw)
    monkeypatch.setattr("app.services.llm_service.settings.gemini_model", "gemini-2.5-flash-lite")
    monkeypatch.setattr(
        "app.services.llm_service.settings.gemini_model_fallback",
        "gemini-flash-lite-latest",
    )
    extraction, usage = service._extract_with_gemini(
        "Ana puede el jueves a las 15",
        9,
        18,
        api_key="AIza-test-only-key",
    )
    assert "gemini-2.5-flash-lite" in models_seen
    assert "gemini-flash-lite-latest" in models_seen
    assert extraction.participants
    assert usage.provider == "gemini:gemini-flash-lite-latest"
