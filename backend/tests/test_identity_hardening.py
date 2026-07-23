from app.schemas import (
    ChannelMessage,
    ExtractedAvailability,
    Participant,
    Session,
    TimeSlot,
)
from app.services.group_memory import (
    apply_memory_identity,
    build_alias_lookup,
    build_group_memory_context,
    resolve_display_name,
)
from app.services.llm_service import normalize_channel_extraction


def test_alias_resolution_from_parenthetical_and_at_sign():
    memory = build_group_memory_context(
        Session(title="G", participants=[Participant(name="Julissa (Yuli)")])
    )
    extraction = ExtractedAvailability(
        participants=[
            Participant(name="Yuli", availability=[TimeSlot(day="jueves", start="15:00", end="16:00")]),
            Participant(name="@yuli", availability=[TimeSlot(day="jueves", start="16:00", end="17:00")]),
        ]
    )
    resolved = apply_memory_identity(extraction, memory)
    assert all(p.name == "Julissa (Yuli)" for p in resolved.participants)


def test_unknown_alias_remains_unresolved():
    memory = build_group_memory_context(
        Session(title="G", participants=[Participant(name="Nicolas"), Participant(name="Matias")])
    )
    extraction = ExtractedAvailability(
        participants=[
            Participant(name="Cata", availability=[TimeSlot(day="jueves", start="19:00", end="20:00")])
        ]
    )
    resolved = apply_memory_identity(extraction, memory)
    assert resolved.participants[0].name == "Cata"


def test_duplicate_alias_remains_ambiguous():
    memory = build_group_memory_context(
        Session(
            title="G",
            participants=[
                Participant(name="Ana (Cata)"),
                Participant(name="Catalina (Cata)"),
            ],
        )
    )
    lookup = build_alias_lookup(memory)
    assert resolve_display_name("Cata", lookup) == "Cata"
    extraction = ExtractedAvailability(participants=[Participant(name="Cata")])
    resolved = apply_memory_identity(extraction, memory)
    assert resolved.participants[0].name == "Cata"
    assert "ambiguous_alias_identity" in resolved.quality_flags


def test_similar_names_do_not_match_partially():
    memory = build_group_memory_context(
        Session(
            title="G",
            participants=[
                Participant(name="Nicolas"),
                Participant(name="Nicole"),
            ],
        )
    )
    extraction = ExtractedAvailability(participants=[Participant(name="Nicol")])
    resolved = apply_memory_identity(extraction, memory)
    assert resolved.participants[0].name == "Nicol"


def test_identity_channel_id_overrides_llm_guess():
    memory = build_group_memory_context(
        Session(
            title="G",
            participants=[Participant(name="Julissa (Yuli)", external_id="user-j", external_ids=["user-j"])],
        )
    )
    extraction = ExtractedAvailability(
        participants=[
            Participant(
                name="OtraPersona",
                external_id="user-j",
                external_ids=["user-j"],
                availability=[TimeSlot(day="jueves", start="15:00", end="16:00")],
            )
        ]
    )
    # apply_memory_identity must not rename channel-bound rows.
    resolved = apply_memory_identity(extraction, memory)
    assert resolved.participants[0].name == "OtraPersona"
    assert resolved.participants[0].external_id == "user-j"


def test_identity_normalizes_case_and_accents_safely():
    memory = build_group_memory_context(
        Session(title="G", participants=[Participant(name="Julissa (Yuli)")])
    )
    extraction = ExtractedAvailability(
        participants=[
            Participant(name="yuli"),
            Participant(name="@YULI"),
            Participant(name="JULISSA"),
        ]
    )
    resolved = apply_memory_identity(extraction, memory)
    names = [p.name for p in resolved.participants]
    assert names.count("Julissa (Yuli)") >= 2
