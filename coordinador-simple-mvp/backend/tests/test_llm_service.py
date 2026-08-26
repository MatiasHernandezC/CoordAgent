import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app.services.llm_service as llm_module
from app.schemas import AvailabilityRemoval, ExtractedAvailability, Participant, TimeSlot
from app.services.llm_service import (
    LlmUnavailableError,
    LlmService,
    build_channel_extraction_text,
    build_gemini_stream_url,
    current_workday_name,
    estimate_gemini_cost,
    extract_gemini_text,
    detect_slots,
    merge_extractions,
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


def test_mock_extractor_handles_abbreviated_day_and_pm_range():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Nicolas puede lun de 3 a 5 pm")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    nicolas = next(participant for participant in extraction.participants if participant.name == "Nicolas")

    assert source == "mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in nicolas.availability] == [("lunes", "15:00", "17:00")]


def test_mock_extractor_handles_colon_range_and_free_synonym():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Camila esta libre mierc 15:30-17:00")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    camila = next(participant for participant in extraction.participants if participant.name == "Camila")

    assert source == "mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in camila.availability] == [("miercoles", "15:00", "17:00")]


def test_mock_extractor_handles_strictly_free_after_hour_with_short_day():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Beto esta libre jue despues de las 4")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    beto = next(participant for participant in extraction.participants if participant.name == "Beto")

    assert source == "mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in beto.availability] == [("jueves", "17:00", "18:00")]


def test_positive_after_hour_uses_next_full_block_but_from_remains_inclusive():
    after = detect_slots(
        "Nicolas puede martes despues de las 9 am pero antes de las 8 pm",
        workday_start=9,
        workday_end=22,
    )
    from_hour = detect_slots(
        "Nicolas puede martes desde las 9 am pero antes de las 8 pm",
        workday_start=9,
        workday_end=22,
    )

    assert [(slot.start, slot.end) for slot in after] == [("10:00", "20:00")]
    assert [(slot.start, slot.end) for slot in from_hour] == [("09:00", "20:00")]


def test_mock_extractor_handles_no_me_sirve_range_as_removal():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Ana no le sirve martes de 10 a 12")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    assert source == "mock"
    assert token_usage is None
    assert extraction.removals[0].participant_name == "Ana"
    assert [(slot.day, slot.start, slot.end) for slot in extraction.removals[0].slots] == [("martes", "10:00", "12:00")]


def test_channel_rewrites_first_person_free_synonym_to_sender():
    transcript = build_channel_extraction_text(
        [
            ChannelMessage(
                sender="Nicolas",
                text="estoy libre lun de 3 a 5",
            )
        ]
    )

    assert "Nicolas puede" in transcript
    assert "estoy libre" not in transcript.lower()


def test_channel_rewrites_first_person_tambien_puedo_to_sender():
    transcript = build_channel_extraction_text(
        [
            ChannelMessage(
                sender="Nicolás",
                text="Yo también puedo ir a la reunión el martes todo el día",
            )
        ]
    )

    assert "Nicolás puede ir a la reunión el martes todo el día" in transcript
    assert "Yo también" not in transcript


def test_channel_invocation_with_noise_has_no_scheduling_signal():
    service = LlmService()

    extraction, source, token_usage = service.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                text="@coordina jajaj asdf $$$ 123",
            )
        ]
    )

    assert source == "channel_no_new_availability"
    assert token_usage is None
    assert extraction.participants == []
    assert extraction.removals == []


def test_channel_keeps_duplicate_display_names_as_distinct_whatsapp_identities(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    service = LlmService()

    extraction, source, _ = service.extract_channel_availability(
        [
            ChannelMessage(
                sender="Alex",
                sender_id="alex-one@wa",
                text="yo puedo lunes todo el dia",
            ),
            ChannelMessage(
                sender="Alex",
                sender_id="alex-two@wa",
                text="yo puedo martes todo el dia",
            ),
            ChannelMessage(sender="Admin", sender_id="admin@wa", text="@coordina"),
        ]
    )

    assert source == "channel_mock"
    assert [(item.name, item.external_id) for item in extraction.participants] == [
        ("Alex", "alex-one@wa"),
        ("Alex", "alex-two@wa"),
    ]
    assert [item.availability[0].day for item in extraction.participants] == ["lunes", "martes"]


def test_mock_extractor_handles_indirect_reported_availability():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Ana dijo que podia el jueves pero a la 11am")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    ana = next(participant for participant in extraction.participants if participant.name == "Ana")

    assert source == "mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in ana.availability] == [("jueves", "11:00", "12:00")]


def test_mock_extractor_handles_participation_with_day_hint():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Luisa va a participar pero dice que los dias jueves")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    luisa = next(participant for participant in extraction.participants if participant.name == "Luisa")

    assert source == "mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in luisa.availability] == [("jueves", "09:00", "18:00")]


def test_mock_extractor_handles_plural_names_sharing_availability():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_availability("Luisa y Ana pueden jueves a las 11")
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    slots_by_name = {
        participant.name: [(slot.day, slot.start, slot.end) for slot in participant.availability]
        for participant in extraction.participants
    }

    assert source == "mock"
    assert token_usage is None
    assert slots_by_name["Luisa"] == [("jueves", "11:00", "12:00")]
    assert slots_by_name["Ana"] == [("jueves", "11:00", "12:00")]


def test_channel_extracts_indirect_availability_from_one_sender():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_channel_availability(
            [
                ChannelMessage(
                    sender="Yuli",
                    text="@ana dijo que podia el jueves pero a la 11am",
                    mentioned_jids=["56911111111@s.whatsapp.net"],
                ),
                ChannelMessage(sender="Yuli", text="@coordina"),
            ]
        )
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    ana = next(participant for participant in extraction.participants if participant.name == "Ana")

    assert source == "channel_mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in ana.availability] == [("jueves", "11:00", "12:00")]


def test_channel_extracts_participant_without_time_from_participation_phrase():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_channel_availability(
            [
                ChannelMessage(
                    sender="Yuli",
                    text="@ana tambien va a participar",
                    mentioned_jids=["56911111111@s.whatsapp.net"],
                ),
                ChannelMessage(sender="Yuli", text="@coordina"),
            ]
        )
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    ana = next(participant for participant in extraction.participants if participant.name == "Ana")

    assert source == "channel_mock"
    assert token_usage is None
    assert ana.availability == []


def test_current_workday_name_never_raises_without_mock(monkeypatch):
    # Regresion: ejercita el ZoneInfo REAL (sin monkeypatch). En Windows sin el
    # paquete tzdata esto lanzaba ZoneInfoNotFoundError y rompia mensajes con "hoy".
    from datetime import datetime, timezone as dt_timezone

    monday_utc = datetime(2026, 7, 6, 15, 0, tzinfo=dt_timezone.utc)
    assert current_workday_name(monday_utc) in {"lunes", "martes", "miercoles", "jueves", "viernes", None}

    # Y con una zona configurada invalida, la cadena de fallback tampoco lanza.
    monkeypatch.setattr(settings, "app_timezone", "Zona/Inexistente")
    assert current_workday_name(monday_utc) in {"lunes", "martes", "miercoles", "jueves", "viernes", None}


def test_channel_uses_today_context_for_following_time_only_message(monkeypatch):
    import app.services.llm_service as llm_service

    monkeypatch.setattr(llm_service, "current_workday_name", lambda now=None: "martes")

    transcript = build_channel_extraction_text(
        [
            ChannelMessage(sender="Nicolas", text="Podriamos juntarnos hoy"),
            ChannelMessage(
                sender="Nicolas",
                text="@Daniel puede a las 2",
                mentioned_jids=["56922222222@s.whatsapp.net"],
            ),
            ChannelMessage(sender="Nicolas", text="@coordina"),
        ]
    )

    assert "Podriamos juntarnos martes" in transcript
    assert "puede a las 2 martes" in transcript


def test_channel_extracts_time_only_availability_with_today_context(monkeypatch):
    import app.services.llm_service as llm_service

    monkeypatch.setattr(llm_service, "current_workday_name", lambda now=None: "martes")
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_channel_availability(
            [
                ChannelMessage(sender="Nicolas", text="Podriamos juntarnos hoy"),
                ChannelMessage(
                    sender="Nicolas",
                    text="@Daniel puede a las 2",
                    mentioned_jids=["56922222222@s.whatsapp.net"],
                ),
                ChannelMessage(sender="Nicolas", text="@coordina"),
            ]
        )
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    daniel = next(participant for participant in extraction.participants if participant.name == "Daniel")

    assert source == "channel_mock"
    assert token_usage is None
    assert [(slot.day, slot.start, slot.end) for slot in daniel.availability] == [("martes", "14:00", "15:00")]


def test_channel_keeps_sender_names_with_digits_for_ambiguous_unavailability():
    original_provider = settings.llm_provider
    original_cache = settings.llm_cache_enabled
    try:
        settings.llm_provider = "mock"
        settings.llm_cache_enabled = False
        service = LlmService()

        extraction, source, token_usage = service.extract_channel_availability(
            [
                ChannelMessage(sender="chech0", text="yo no puedo ir antes de las 18:00"),
                ChannelMessage(sender="chech0", text="@coordina"),
            ]
        )
    finally:
        settings.llm_provider = original_provider
        settings.llm_cache_enabled = original_cache

    assert source == "channel_mock"
    assert token_usage is None
    assert [participant.name for participant in extraction.participants] == ["Chech0"]
    assert extraction.participants[0].availability == []


def test_merge_extractions_keeps_model_slots_and_adds_rule_slots():
    model_extraction = ExtractedAvailability(
        participants=[
            Participant(
                name="Luisa",
                availability=[TimeSlot(day="viernes", start="15:00", end="17:00")],
            ),
            Participant(
                name="Luisa",
                availability=[TimeSlot(day="viernes", start="15:00", end="17:00")],
            ),
        ]
    )
    rule_extraction = ExtractedAvailability(
        participants=[
            Participant(
                name="Luisa",
                availability=[
                    TimeSlot(day="jueves", start="09:00", end="18:00"),
                    TimeSlot(day="viernes", start="15:00", end="17:00"),
                ],
            ),
            Participant(name="Ana", availability=[]),
        ]
    )

    merged = merge_extractions(model_extraction, rule_extraction)
    slots_by_name = {
        participant.name: [(slot.day, slot.start, slot.end) for slot in participant.availability]
        for participant in merged.participants
    }

    assert slots_by_name["Luisa"] == [
        ("viernes", "15:00", "17:00"),
        ("jueves", "09:00", "18:00"),
    ]
    assert slots_by_name["Ana"] == []


def test_normalize_llm_extraction_title_cases_lowercase_names():
    extraction = normalize_llm_extraction(
        ExtractedAvailability(participants=[Participant(name="ana", availability=[])]),
        "ana tambien va a participar",
    )

    assert extraction.participants[0].name == "Ana"


def test_normalize_llm_extraction_discards_days_not_grounded_in_message():
    extraction = normalize_llm_extraction(
        ExtractedAvailability(
            participants=[
                Participant(
                    name="Nicolas",
                    availability=[
                        TimeSlot(day="lunes", start="14:00", end="15:00"),
                        TimeSlot(day="martes", start="14:00", end="15:00"),
                    ],
                )
            ]
        ),
        "Nicolas puede a las 2 martes",
    )

    assert [(slot.day, slot.start, slot.end) for slot in extraction.participants[0].availability] == [
        ("martes", "14:00", "15:00")
    ]


def test_normalize_llm_extraction_rejects_nos_as_participant_name():
    extraction = normalize_llm_extraction(
        ExtractedAvailability(participants=[Participant(name="Nos", availability=[])]),
        "nos ayudas a cerrar un horario?",
    )

    assert extraction.participants == []


def test_normalize_llm_extraction_rejects_no_as_participant_name():
    extraction = normalize_llm_extraction(
        ExtractedAvailability(participants=[Participant(name="No", availability=[])]),
        "chech0 no puede ir antes de las 18:00",
    )

    assert extraction.participants == []


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


def test_gemini_stream_measures_real_first_content_chunk(monkeypatch):
    response_text = json.dumps(
        {
            "participants": [
                {
                    "name": "Ana",
                    "availability": [{"day": "lunes", "start": "09:00", "end": "10:00"}],
                }
            ]
        },
        ensure_ascii=False,
    )
    split_at = len(response_text) // 2
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": response_text[:split_at]}]}}]},
        {
            "candidates": [{"content": {"parts": [{"text": response_text[split_at:]}]}}],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 5,
                "totalTokenCount": 17,
            },
        },
    ]
    lines = []
    for chunk in chunks:
        lines.extend([f"data: {json.dumps(chunk, ensure_ascii=False)}\n".encode(), b"\n"])

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(lines)

    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append((request.full_url, timeout))
        return FakeResponse()

    timer = iter([10.0, 10.245])
    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "perf_counter", lambda: next(timer))
    monkeypatch.setattr(settings, "gemini_streaming_enabled", True)
    monkeypatch.setattr(settings, "gemini_url", "https://example.test/models/{model}:generateContent")

    extraction, usage = LlmService()._extract_with_gemini(
        "Ana puede el lunes de 09:00 a 10:00",
        9,
        18,
        api_key="AIza-test-only",
    )

    assert extraction.participants[0].name == "Ana"
    assert usage.total_tokens == 17
    assert usage.time_to_first_token_ms == 245
    assert requested_urls == [
        (f"https://example.test/models/{settings.gemini_model}:streamGenerateContent?alt=sse", settings.gemini_timeout_seconds)
    ]


def test_gemini_stream_url_preserves_existing_query():
    assert build_gemini_stream_url("https://example.test/model:generateContent?key=value") == (
        "https://example.test/model:streamGenerateContent?key=value&alt=sse"
    )


def test_gemini_stream_over_real_http_finishes_after_ttft(monkeypatch):
    response_text = json.dumps(
        {
            "participants": [
                {
                    "name": "Ana",
                    "availability": [{"day": "lunes", "start": "09:00", "end": "10:00"}],
                }
            ]
        },
        ensure_ascii=False,
    )
    split_at = len(response_text) // 2

    class StreamingHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            assert ":streamGenerateContent" in self.path
            assert "alt=sse" in self.path
            assert self.headers.get("x-goog-api-key") == "AIza-local-stream-test"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            first = {"candidates": [{"content": {"parts": [{"text": response_text[:split_at]}]}}]}
            self.wfile.write(f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.15)
            final = {
                "candidates": [{"content": {"parts": [{"text": response_text[split_at:]}]}}],
                "usageMetadata": {
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 17,
                },
            }
            self.wfile.write(f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "gemini_streaming_enabled", True)
    monkeypatch.setattr(
        settings,
        "gemini_url",
        f"http://127.0.0.1:{server.server_port}/v1beta/models/{{model}}:generateContent",
    )
    monkeypatch.setattr(settings, "gemini_timeout_seconds", 2)

    try:
        started = time.perf_counter()
        extraction, usage = LlmService()._extract_with_gemini(
            "Ana puede el lunes de 09:00 a 10:00",
            9,
            18,
            api_key="AIza-local-stream-test",
        )
        total_ms = round((time.perf_counter() - started) * 1000)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert extraction.participants[0].name == "Ana"
    assert usage.time_to_first_token_ms is not None
    assert total_ms - usage.time_to_first_token_ms >= 100


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
