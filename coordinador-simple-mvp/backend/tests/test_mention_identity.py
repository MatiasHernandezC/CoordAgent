"""Regresiones de identidad para disponibilidad recibida desde WhatsApp.

La autoridad es el JID transportado por el gateway. Un nombre escrito en texto
plano no basta para atribuir disponibilidad a otra persona: debe existir una
mencion real en la metadata del mismo mensaje.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.services.session_service as session_module
from app.main import app
from app.schemas import ChannelMessage, ExtractedAvailability, Participant, TimeSlot
from app.services.llm_service import LlmService, llm_service
from app.services.session_service import SessionService
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def mock_llm(monkeypatch) -> LlmService:
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    return LlmService()


@pytest.fixture()
def sessions(tmp_path: Path, monkeypatch) -> SessionService:
    monkeypatch.setattr(session_module, "repository", JsonRepository(tmp_path / "sessions.json"))
    return SessionService()


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    llm_service._cache.clear()
    monkeypatch.setattr(session_module, "repository", JsonRepository(tmp_path / "api-sessions.json"))
    return TestClient(app)


def test_channel_message_preserves_gateway_identity_metadata():
    message = ChannelMessage(
        sender="Nicolas",
        sender_id="123456789012345@lid",
        sender_aliases=["123456789012345@lid", "56922222222@s.whatsapp.net"],
        mentioned_jids=["56911111111@s.whatsapp.net"],
        text="@Gabo puede lunes todo el dia",
    )

    assert message.sender_aliases == ["123456789012345@lid", "56922222222@s.whatsapp.net"]
    assert message.mentioned_jids == ["56911111111@s.whatsapp.net"]


def test_channel_endpoint_persists_gateway_identity_metadata(api_client: TestClient):
    session_id = api_client.post("/api/sessions", json={"title": "Metadata gateway"}).json()["session"]["id"]
    payload = {
        "sender": "Nicolas",
        "sender_id": "123456789012345@lid",
        "sender_aliases": ["123456789012345@lid", "56922222222@s.whatsapp.net"],
        "mentioned_jids": ["56911111111@s.whatsapp.net"],
        "text": "@Gabo revisa esto por favor",
        "message_id": "metadata-001",
    }

    response = api_client.post(f"/api/sessions/{session_id}/channel/messages", json=payload)
    assert response.status_code == 200
    saved = response.json()["session"]["channel_messages"][-1]
    assert saved["sender_aliases"] == payload["sender_aliases"]
    assert saved["mentioned_jids"] == payload["mentioned_jids"]


def test_self_report_is_attributed_to_sender_jid_without_requiring_mention(mock_llm: LlmService):
    extraction, source, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Gabriel",
                sender_id="56911111111@s.whatsapp.net",
                sender_aliases=["56911111111@s.whatsapp.net"],
                text="yo puedo lunes todo el dia",
            )
        ]
    )

    assert source == "channel_mock"
    assert [(participant.name, participant.external_id) for participant in extraction.participants] == [
        ("Gabriel", "56911111111@s.whatsapp.net")
    ]


def test_plain_third_party_report_is_discarded_and_requests_a_real_mention(mock_llm: LlmService):
    extraction, source, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                sender_aliases=["56922222222@s.whatsapp.net"],
                text="Gabo puede lunes todo el dia",
            )
        ]
    )

    assert source.startswith("channel_")
    assert extraction.participants == []
    assert "third_party_requires_mention" in extraction.quality_flags


def test_channel_reply_explains_that_plain_third_party_report_needs_mention(api_client: TestClient):
    session_id = api_client.post("/api/sessions", json={"title": "Explicar mencion"}).json()["session"]["id"]
    response = api_client.post(
        f"/api/sessions/{session_id}/channel/batch",
        json={
            "messages": [
                {
                    "sender": "Nicolas",
                    "sender_id": "56922222222@s.whatsapp.net",
                    "sender_aliases": ["56922222222@s.whatsapp.net"],
                    "mentioned_jids": [],
                    "text": "Gabo puede lunes todo el dia",
                },
                {
                    "sender": "Nicolas",
                    "sender_id": "56922222222@s.whatsapp.net",
                    "sender_aliases": ["56922222222@s.whatsapp.net"],
                    "mentioned_jids": [],
                    "text": "@coordina",
                },
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["participants"] == []
    assert "third_party_requires_mention" in body["session"]["last_processing"]["quality_flags"]
    assert "menci" in (body["agent_reply"] or "").lower()


def test_real_mention_attributes_third_party_report_to_mentioned_jid(mock_llm: LlmService):
    mentioned_jid = "56911111111@s.whatsapp.net"
    extraction, source, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                sender_aliases=["56922222222@s.whatsapp.net"],
                mentioned_jids=[mentioned_jid],
                text="@Gabo puede lunes todo el dia",
            )
        ]
    )

    assert source == "channel_mock"
    assert [(participant.name, participant.external_id) for participant in extraction.participants] == [
        ("Gabo", mentioned_jid)
    ]
    assert "third_party_requires_mention" not in extraction.quality_flags


def test_mention_metadata_cannot_leak_into_a_later_plain_text_message(mock_llm: LlmService):
    extraction, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                mentioned_jids=["56911111111@s.whatsapp.net"],
                text="@Gabo revisa esto por favor",
            ),
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                text="Gabo puede lunes todo el dia",
            ),
        ]
    )

    assert extraction.participants == []
    assert "third_party_requires_mention" in extraction.quality_flags


def test_later_self_report_does_not_authorize_earlier_plain_third_party_slots(mock_llm: LlmService):
    target_jid = "56911111111@s.whatsapp.net"
    extraction, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                text="Gabriel puede lunes todo el dia",
            ),
            ChannelMessage(
                sender="Gabriel",
                sender_id=target_jid,
                sender_aliases=[target_jid],
                text="yo puedo martes todo el dia",
            ),
        ]
    )

    assert len(extraction.participants) == 1
    participant = extraction.participants[0]
    assert participant.external_id == target_jid
    assert [(slot.day, slot.start, slot.end) for slot in participant.availability] == [
        ("martes", "09:00", "18:00")
    ]
    assert "third_party_requires_mention" in extraction.quality_flags


def test_multiple_mentions_are_not_assigned_to_one_name_by_position(mock_llm: LlmService):
    extraction, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                mentioned_jids=[
                    "56911111111@s.whatsapp.net",
                    "56933333333@s.whatsapp.net",
                ],
                text="@Gabo puede lunes todo el dia",
            )
        ]
    )

    assert extraction.participants == []
    assert "ambiguous_mentioned_identity" in extraction.quality_flags


def test_third_party_removal_also_requires_a_real_mention(mock_llm: LlmService):
    target_jid = "56911111111@s.whatsapp.net"
    plain, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                text="Gabo ya no puede lunes a ninguna hora",
            )
        ]
    )
    mentioned, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                mentioned_jids=[target_jid],
                text="@Gabo ya no puede lunes a ninguna hora",
            )
        ]
    )

    assert plain.removals == []
    assert "third_party_requires_mention" in plain.quality_flags
    assert [(item.participant_name, item.external_id) for item in mentioned.removals] == [
        ("Gabo", target_jid)
    ]


def test_mention_and_later_self_report_merge_by_jid_not_display_name(
    mock_llm: LlmService,
    sessions: SessionService,
):
    mentioned_jid = "56911111111@s.whatsapp.net"
    session = sessions.create("Apodo y nombre publico")

    mentioned, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                sender_aliases=["56922222222@s.whatsapp.net"],
                mentioned_jids=[mentioned_jid],
                text="@Gabo puede lunes todo el dia",
            )
        ]
    )
    session = sessions.merge_extraction(session.id, mentioned, source="channel_mock")

    self_report, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Gabriel",
                sender_id=mentioned_jid,
                sender_aliases=[mentioned_jid],
                text="yo puedo martes todo el dia",
            )
        ]
    )
    session = sessions.merge_extraction(session.id, self_report, source="channel_mock")

    assert len(session.participants) == 1
    participant = session.participants[0]
    assert participant.external_id == mentioned_jid
    assert {(slot.day, slot.start, slot.end) for slot in participant.availability} == {
        ("lunes", "09:00", "18:00"),
        ("martes", "09:00", "18:00"),
    }


def test_two_people_with_same_display_name_are_not_merged_by_name(sessions: SessionService):
    session = sessions.create("Dos Alex")
    session = sessions.merge_extraction(
        session.id,
        ExtractedAvailability(
            participants=[
                Participant(
                    name="Alex",
                    external_id="56911111111@s.whatsapp.net",
                    availability=[_slot("lunes")],
                )
            ]
        ),
        source="channel_mock",
    )
    session = sessions.merge_extraction(
        session.id,
        ExtractedAvailability(
            participants=[
                Participant(
                    name="Alex",
                    external_id="56922222222@s.whatsapp.net",
                    availability=[_slot("martes")],
                )
            ]
        ),
        source="channel_mock",
    )

    assert len(session.participants) == 2
    assert {participant.external_id for participant in session.participants} == {
        "56911111111@s.whatsapp.net",
        "56922222222@s.whatsapp.net",
    }
    assert {
        (participant.external_id, participant.availability[0].day)
        for participant in session.participants
    } == {
        ("56911111111@s.whatsapp.net", "lunes"),
        ("56922222222@s.whatsapp.net", "martes"),
    }


def test_self_removal_updates_only_matching_jid_when_names_are_equal(
    mock_llm: LlmService,
    sessions: SessionService,
):
    first_jid = "56911111111@s.whatsapp.net"
    second_jid = "56922222222@s.whatsapp.net"
    session = sessions.create("Dos Alex corrigen")
    session = sessions.merge_extraction(
        session.id,
        ExtractedAvailability(
            participants=[
                Participant(name="Alex", external_id=first_jid, availability=[_slot("lunes")]),
                Participant(name="Alex", external_id=second_jid, availability=[_slot("lunes")]),
            ]
        ),
        source="channel_mock",
    )

    removal, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Alex",
                sender_id=second_jid,
                sender_aliases=[second_jid],
                text="ya no puedo lunes a ninguna hora",
            )
        ]
    )
    session = sessions.merge_extraction(session.id, removal, source="channel_mock")
    by_jid = {participant.external_id: participant for participant in session.participants}

    assert [(slot.day, slot.start, slot.end) for slot in by_jid[first_jid].availability] == [
        ("lunes", "09:00", "18:00")
    ]
    assert by_jid[second_jid].availability == []


def test_sender_pn_and_lid_aliases_resolve_to_one_identity(
    mock_llm: LlmService,
    sessions: SessionService,
):
    pn = "56911111111@s.whatsapp.net"
    lid = "123456789012345@lid"
    session = sessions.create("Alias tecnico PN LID")

    first, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Gabriel",
                sender_id=lid,
                sender_aliases=[lid, pn],
                text="yo puedo lunes todo el dia",
            )
        ]
    )
    session = sessions.merge_extraction(session.id, first, source="channel_mock")

    second, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Gabriel",
                sender_id=pn,
                sender_aliases=[pn, lid],
                text="yo puedo martes todo el dia",
            )
        ]
    )
    session = sessions.merge_extraction(session.id, second, source="channel_mock")

    assert len(session.participants) == 1
    participant = session.participants[0]
    assert participant.external_id == pn
    assert set(participant.external_ids) == {pn, lid}
    assert {(slot.day, slot.start, slot.end) for slot in participant.availability} == {
        ("lunes", "09:00", "18:00"),
        ("martes", "09:00", "18:00"),
    }


def test_remove_command_requires_mention_then_removes_by_jid_idempotently(api_client: TestClient):
    created = api_client.post("/api/sessions", json={"title": "Quitar por identidad"}).json()["session"]
    session_id = created["id"]
    target_jid = "56911111111@s.whatsapp.net"
    seeded = session_module.session_service.merge_extraction(
        session_id,
        ExtractedAvailability(
            participants=[
                Participant(
                    name="Camila",
                    external_id=target_jid,
                    availability=[_slot("lunes")],
                )
            ]
        ),
        source="channel_mock",
    )
    revision_before = seeded.proposal_revision

    plain_payload = {
        "sender": "Nicolas",
        "sender_id": "56922222222@s.whatsapp.net",
        "sender_aliases": ["56922222222@s.whatsapp.net"],
        "mentioned_jids": [],
        "text": "@coordina quitar Camila",
        "message_id": "remove-without-mention-001",
    }
    plain_response = api_client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json=plain_payload,
    )
    plain = plain_response.json()

    assert plain_response.status_code == 200
    assert plain["llm_source"] == "channel_command"
    assert "menci" in (plain["agent_reply"] or "").lower()
    assert [(item["name"], item["external_id"]) for item in plain["session"]["participants"]] == [
        ("Camila", target_jid)
    ]
    assert plain["session"]["proposal_revision"] == revision_before

    mentioned_payload = {
        **plain_payload,
        "mentioned_jids": [target_jid],
        "text": "@coordina quitar @Camila",
        "message_id": "remove-with-mention-001",
    }
    removed_response = api_client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json=mentioned_payload,
    )
    removed = removed_response.json()

    assert removed_response.status_code == 200
    assert removed["llm_source"] == "channel_command"
    assert removed["session"]["participants"] == []

    retried_response = api_client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json=mentioned_payload,
    )
    retried = retried_response.json()

    assert retried_response.status_code == 200
    assert retried["duplicate"] is True
    assert retried["agent_reply"] == removed["agent_reply"]
    assert retried["session"]["participants"] == []


def test_numeric_mention_uses_safe_placeholder_until_target_speaks(
    mock_llm: LlmService,
    sessions: SessionService,
):
    target_jid = "56911111111@s.whatsapp.net"
    session = sessions.create("Mencion numerica")
    mentioned, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                mentioned_jids=[target_jid],
                text="@56911111111 puede lunes todo el dia",
            )
        ]
    )
    assert mentioned.participants[0].name == "Contacto mencionado"
    assert "5000" not in mentioned.participants[0].name
    session = sessions.merge_extraction(session.id, mentioned, source="channel_mock")

    self_report, _, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Gabriel",
                sender_id=target_jid,
                sender_aliases=[target_jid],
                text="yo puedo martes todo el dia",
            )
        ]
    )
    session = sessions.merge_extraction(session.id, self_report, source="channel_mock")

    assert len(session.participants) == 1
    assert session.participants[0].name == "Gabriel"
    assert {slot.day for slot in session.participants[0].availability} == {"lunes", "martes"}


def test_old_numeric_contact_label_is_migrated_without_changing_identity(sessions: SessionService):
    session = sessions.create("Migracion de etiqueta")
    session.participants = [
        Participant(
            name="Contacto 5000",
            external_id="276514323067118@lid",
            availability=[],
        )
    ]
    session.missing_info = ["Falta disponibilidad de Contacto 5000."]
    session.last_agent_reply = "Falta disponibilidad de Contacto 5000."
    session_module.repository.save(session)

    migrated = sessions.get(session.id)

    assert migrated.participants[0].name == "Contacto mencionado"
    assert migrated.participants[0].external_id == "276514323067118@lid"
    assert migrated.missing_info == ["Falta disponibilidad de Contacto mencionado."]
    assert migrated.last_agent_reply == "Falta disponibilidad de Contacto mencionado."


def test_mixed_self_and_plain_third_party_message_is_discarded_entirely(mock_llm: LlmService):
    extraction, source, _ = mock_llm.extract_channel_availability(
        [
            ChannelMessage(
                sender="Nicolas",
                sender_id="56922222222@s.whatsapp.net",
                text="yo puedo lunes todo el dia y Gabriel puede martes todo el dia",
            )
        ]
    )

    assert source == "channel_identity_guard"
    assert extraction.participants == []
    assert "third_party_requires_mention" in extraction.quality_flags


def _slot(day: str) -> TimeSlot:
    return TimeSlot(day=day, start="09:00", end="18:00")  # type: ignore[arg-type]
