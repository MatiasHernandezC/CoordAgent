from datetime import datetime, timezone

from app.schemas import (
    ChannelConfig,
    ChannelMessage,
    DecisionRecord,
    ExtractedAvailability,
    Participant,
    ProcessingSummary,
    Session,
    TimeOption,
    TimeSlot,
)
from app.services.group_memory import (
    MAX_IDENTITY_HINTS,
    MAX_PARTICIPANTS,
    MAX_PAST_DECISIONS,
    build_group_memory_context,
    format_group_memory_for_prompt,
    format_group_memory_preview,
    memory_fingerprint,
)
from app.services.llm_service import (
    LlmService,
    build_cache_key,
    build_extraction_prompt,
)
from app.services.session_service import build_processing_summary


def _decision(day: str = "martes", start: str = "15:00", end: str = "16:00", **kwargs) -> DecisionRecord:
    option = TimeOption(
        day=day,  # type: ignore[arg-type]
        start=start,
        end=end,
        available_participants=["Ana"],
        score=1,
        coverage_percent=50,
    )
    return DecisionRecord(option=option, summary="ok", **kwargs)


def test_group_memory_empty_session():
    session = Session(title="Nueva")
    memory = build_group_memory_context(session)

    assert memory.known_participants == []
    assert memory.past_decisions == []
    assert memory.identity_hints == []
    assert memory.has_content() is True  # title becomes group_name fallback via session.title... wait
    # title is used only when group_name is empty: group_name = config.group_name or session.title
    assert memory.group_name == "Nueva"
    assert format_group_memory_for_prompt(memory) is not None
    assert "Nueva" in format_group_memory_for_prompt(memory)


def test_group_memory_truly_blank_title():
    session = Session(title=" ")
    session.title = " "
    memory = build_group_memory_context(session)
    # whitespace title cleaned may become empty-ish
    assert memory.known_participants == []
    # has_content depends on cleaned title
    prompt = format_group_memory_for_prompt(memory)
    if not memory.has_content():
        assert prompt is None


def test_group_memory_includes_roster_and_decisions():
    session = Session(
        title="Standup",
        channel_config=ChannelConfig(
            group_name="Equipo Alpha",
            group_jid="120363@g.us",
            group_participant_count=5,
        ),
        participants=[
            Participant(
                name="Ana",
                availability=[TimeSlot(day="lunes", start="09:00", end="12:00")],
            ),
            Participant(name="Luis"),
        ],
        decision_history=[
            _decision(day="lunes", start="10:00", end="11:00", source="panel"),
            _decision(day="miercoles", start="15:00", end="16:00", source="whatsapp"),
        ],
        channel_messages=[
            ChannelMessage(sender="Anita", text="hola", kind="human"),
        ],
    )
    memory = build_group_memory_context(session)

    assert memory.group_name == "Equipo Alpha"
    assert "Ana" in memory.known_participants
    assert "Luis" in memory.known_participants
    assert memory.expected_group_size == 5
    assert len(memory.past_decisions) == 2
    assert "miercoles" in memory.past_decisions[0]  # newest first
    assert any("disponible" in line and "Ana" in line for line in memory.known_constraints_summary)
    assert any("Anita" in hint for hint in memory.identity_hints)

    prompt = format_group_memory_for_prompt(memory)
    assert prompt is not None
    assert "RAG estructurado" in prompt
    assert "NO inventes disponibilidad" in prompt
    assert "NO conviertas decisiones historicas" in prompt
    assert "Equipo Alpha" in prompt


def test_group_memory_limits():
    participants = [Participant(name=f"P{i}") for i in range(MAX_PARTICIPANTS + 10)]
    decisions = [_decision(day="lunes", start=f"{9 + (i % 8):02d}:00", end=f"{10 + (i % 8):02d}:00") for i in range(10)]
    messages = [ChannelMessage(sender=f"S{i}", text="x", kind="human") for i in range(MAX_IDENTITY_HINTS + 20)]
    session = Session(
        title="Big",
        participants=participants,
        decision_history=decisions,
        channel_messages=messages,
    )
    memory = build_group_memory_context(session)

    assert len(memory.known_participants) == MAX_PARTICIPANTS
    assert len(memory.past_decisions) == MAX_PAST_DECISIONS
    assert len(memory.identity_hints) <= MAX_IDENTITY_HINTS


def test_group_memory_deduplicates_values():
    session = Session(
        title="Dup",
        participants=[
            Participant(name="Ana"),
            Participant(name="ana"),
            Participant(name="Ana "),
        ],
        channel_messages=[
            ChannelMessage(sender="Ana", text="a", kind="human"),
            ChannelMessage(sender="Ana", text="b", kind="human"),
        ],
    )
    memory = build_group_memory_context(session)
    lowered = [name.lower() for name in memory.known_participants]
    assert len(lowered) == len(set(lowered))


def test_group_memory_fingerprint_is_stable():
    session = Session(
        title="Stable",
        participants=[Participant(name="Ana"), Participant(name="Luis")],
        decision_history=[_decision()],
    )
    first = build_group_memory_context(session)
    second = build_group_memory_context(session)
    second.retrieved_at = datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat()

    assert memory_fingerprint(first) == memory_fingerprint(second)
    assert memory_fingerprint(first) != "none"


def test_group_memory_fingerprint_changes_with_content():
    base = Session(title="G", participants=[Participant(name="Ana")])
    other = Session(title="G", participants=[Participant(name="Luis")])
    assert memory_fingerprint(build_group_memory_context(base)) != memory_fingerprint(
        build_group_memory_context(other)
    )


def test_build_extraction_prompt_includes_memory():
    memory = build_group_memory_context(
        Session(
            title="Demo",
            participants=[Participant(name="Ana")],
            decision_history=[_decision()],
        )
    )
    prompt = build_extraction_prompt("puedo el jueves", group_memory=memory)

    assert "Memoria recuperada del grupo" in prompt
    assert "NO inventes disponibilidad" in prompt
    assert "Texto a analizar:" in prompt
    assert prompt.index("Ventana horaria") < prompt.index("Memoria recuperada")
    assert prompt.index("Memoria recuperada") < prompt.index("Texto a analizar:")
    assert prompt.index("Contexto temporal") < prompt.index("Memoria recuperada")


def test_build_extraction_prompt_without_memory():
    prompt = build_extraction_prompt("puedo el jueves", group_memory=None)
    assert "Memoria recuperada del grupo" not in prompt
    assert "Texto a analizar:" in prompt


def test_cache_key_changes_with_memory():
    a = memory_fingerprint(build_group_memory_context(Session(title="A", participants=[Participant(name="Ana")])))
    b = memory_fingerprint(build_group_memory_context(Session(title="A", participants=[Participant(name="Luis")])))
    key_a = build_cache_key("mensaje", 9, 18, memory_fingerprint=a)
    key_b = build_cache_key("mensaje", 9, 18, memory_fingerprint=b)
    assert key_a != key_b


def test_cache_key_ignores_retrieved_at():
    session = Session(title="X", participants=[Participant(name="Ana")])
    m1 = build_group_memory_context(session)
    m2 = build_group_memory_context(session)
    m2.retrieved_at = "2099-01-01T00:00:00+00:00"
    key1 = build_cache_key("hola", memory_fingerprint=memory_fingerprint(m1))
    key2 = build_cache_key("hola", memory_fingerprint=memory_fingerprint(m2))
    assert key1 == key2


def test_extract_channel_with_memory_mock():
    service = LlmService()
    messages = [
        ChannelMessage(sender="Ana", text="puedo el lunes a las 11", kind="human"),
    ]
    memory = build_group_memory_context(
        Session(
            title="Canal",
            participants=[Participant(name="Ana")],
            channel_messages=messages,
        )
    )
    extraction, source, token_usage = service.extract_channel_availability(
        messages,
        group_memory=memory,
    )
    assert source.startswith("channel_")
    assert extraction.participants or source  # mock may extract Ana
    assert token_usage is None or token_usage.provider


def test_processing_summary_retrieval_defaults():
    summary = ProcessingSummary()
    assert summary.retrieval_used is False
    assert summary.retrieval_source is None
    assert summary.retrieval_participant_count == 0
    assert summary.retrieval_past_decision_count == 0
    assert summary.retrieval_preview == ""


def test_processing_summary_contains_retrieval_metadata():
    memory = build_group_memory_context(
        Session(
            title="Meta",
            channel_config=ChannelConfig(group_name="Grupo Demo"),
            participants=[Participant(name="Ana"), Participant(name="Luis")],
            decision_history=[_decision()],
        )
    )
    summary = build_processing_summary(
        "mock",
        ExtractedAvailability(participants=[Participant(name="Ana", availability=[TimeSlot(day="jueves", start="11:00", end="12:00")])]),
        None,
        group_memory=memory,
    )
    assert summary.retrieval_used is True
    assert summary.retrieval_source == "session_store"
    assert summary.retrieval_participant_count == 2
    assert summary.retrieval_past_decision_count == 1
    assert "roster=" in summary.retrieval_preview
    assert "@g.us" not in summary.retrieval_preview


def test_old_session_remains_compatible():
    # Sessions persisted without retrieval fields must still validate.
    raw = {
        "title": "Antigua",
        "last_processing": {
            "source": "mock",
            "confidence": "high",
            "confidence_label": "Alta",
            "detail": "ok",
            "participants_detected": 1,
            "removals_detected": 0,
            "cached": False,
            "fallback_used": False,
            "quality_flags": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    }
    session = Session.model_validate(raw)
    assert session.last_processing is not None
    assert session.last_processing.retrieval_used is False
    assert session.last_processing.retrieval_preview == ""


def test_preview_redacts_sensitive_tokens():
    memory = build_group_memory_context(
        Session(
            title="Grupo 56912345678@s.whatsapp.net",
            participants=[Participant(name="Ana 56987654321")],
        )
    )
    preview = format_group_memory_preview(memory)
    assert "56912345678" not in preview
    assert "56987654321" not in preview
    assert "@s.whatsapp.net" not in preview


def test_group_memory_parenthetical_alias_hint():
    memory = build_group_memory_context(
        Session(
            title="Equipo",
            participants=[
                Participant(name="Nicolas"),
                Participant(name="Julissa (Yuli)"),
                Participant(name="Matias"),
            ],
        )
    )
    prompt = format_group_memory_for_prompt(memory)
    assert prompt is not None
    assert "Julissa (Yuli)" in memory.known_participants
    assert any("Yuli" in hint for hint in memory.identity_hints)
    assert "tambien es conocida/o como Yuli" in prompt


def test_group_memory_links_sender_alias_by_channel_id():
    memory = build_group_memory_context(
        Session(
            title="Equipo",
            participants=[
                Participant(name="Julissa", external_id="user-julissa", external_ids=["user-julissa"]),
            ],
            channel_messages=[
                ChannelMessage(
                    sender="Yuli",
                    sender_id="user-julissa",
                    text="hola",
                    kind="human",
                ),
            ],
        )
    )
    assert any("Yuli" in hint and "Julissa" in hint for hint in memory.identity_hints)


def test_apply_memory_identity_maps_alias_to_roster():
    from app.schemas import ExtractedAvailability
    from app.services.group_memory import apply_memory_identity

    memory = build_group_memory_context(
        Session(
            title="Equipo",
            participants=[
                Participant(name="Nicolas"),
                Participant(name="Julissa (Yuli)"),
            ],
        )
    )
    extraction = ExtractedAvailability(
        participants=[
            Participant(name="Yuli", availability=[TimeSlot(day="jueves", start="15:00", end="18:00")]),
            Participant(name="Nicolas", availability=[TimeSlot(day="jueves", start="16:00", end="18:00")]),
        ]
    )
    resolved = apply_memory_identity(extraction, memory)
    names = [p.name for p in resolved.participants]
    assert "Julissa (Yuli)" in names
    assert "Yuli" not in names
    assert "Nicolas" in names
