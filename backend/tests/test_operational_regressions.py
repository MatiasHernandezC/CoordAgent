"""Regresiones de produccion: readiness, confirmacion estable y calendario RFC."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import pytest
from fastapi.testclient import TestClient

import app.services.session_service as session_module
import app.bff.routes as routes_module
from app.main import app
from app.schemas import DecisionRecord, Session, TimeOption
from app.services.calendar_export import build_calendar_ics, confirmed_event_date
from app.services.llm_service import llm_service
from app.settings import settings
from app.storage.json_repository import JsonRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    llm_service._cache.clear()
    session_module.repository = JsonRepository(tmp_path / "sessions.json")
    return TestClient(app)


def create_calculated_channel_session(client: TestClient) -> str:
    session_id = client.post("/api/sessions", json={"title": "Regresion operativa"}).json()["session"]["id"]
    response = client.post(
        f"/api/sessions/{session_id}/channel/batch",
        json={
            "messages": [
                {"sender": "Nicolas", "text": "yo puedo lunes en la tarde"},
                {"sender": "Camila", "text": "yo puedo lunes desde las 16"},
                {"sender": "Nicolas", "text": "@coordina"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["session"]["options"]
    return session_id


def confirm_first_option(client: TestClient, session_id: str) -> dict:
    session = client.get(f"/api/sessions/{session_id}").json()["session"]
    response = client.post(
        f"/api/sessions/{session_id}/confirm",
        json={"option_id": session["options"][0]["id"]},
    )
    assert response.status_code == 200
    return response.json()["session"]


def test_readiness_checks_active_repository(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "coordinador-simple-mvp",
        "storage": settings.db_backend,
    }


def test_readiness_returns_503_when_storage_fails(client, monkeypatch):
    def fail_healthcheck():
        raise RuntimeError("storage offline")

    monkeypatch.setattr(session_module.repository, "healthcheck", fail_healthcheck)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Storage unavailable"}


def test_operational_status_exposes_real_gateway_state(client, monkeypatch):
    monkeypatch.setattr(
        routes_module,
        "fetch_gateway_status",
        lambda: {
            "ok": True,
            "connected": True,
            "state": "connected",
            "known_groups": 3,
            "checked_at": "2026-07-14T00:00:00+00:00",
        },
    )

    response = client.get("/api/ops/status")
    assert response.status_code == 200
    assert response.json()["gateway"]["connected"] is True
    assert response.json()["gateway"]["known_groups"] == 3


def test_group_resolve_is_idempotent_after_lost_response(client):
    payload = {
        "group_jid": "120363000000000000@g.us",
        "group_name": "Equipo TAVI",
        "group_participant_count": 8,
        "trigger_word": "@coordina",
    }

    first = client.post("/api/channel/groups/resolve", json=payload).json()["session"]
    second = client.post(
        "/api/channel/groups/resolve",
        json={**payload, "group_name": "Equipo TAVI actualizado", "group_participant_count": 9},
    ).json()["session"]
    sessions = client.get("/api/sessions").json()["sessions"]

    assert first["id"] == second["id"]
    assert second["title"] == "WhatsApp - Equipo TAVI actualizado"
    assert second["channel_config"]["group_participant_count"] == 9
    assert len([item for item in sessions if item["channel_config"]["group_jid"] == payload["group_jid"]]) == 1


def test_group_resolve_preserves_a_concurrent_session_message(client, monkeypatch):
    payload = {
        "group_jid": "120363000000000001@g.us",
        "group_name": "Equipo concurrente",
        "group_participant_count": 4,
        "trigger_word": "@coordina",
    }
    session_id = client.post("/api/channel/groups/resolve", json=payload).json()["session"]["id"]

    snapshot_taken = Event()
    release_resolve = Event()
    original_list_all = session_module.repository.list_all

    def delayed_list_all():
        sessions = original_list_all()
        snapshot_taken.set()
        assert release_resolve.wait(timeout=3)
        return sessions

    monkeypatch.setattr(session_module.repository, "list_all", delayed_list_all)
    resolve_client = TestClient(app)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            resolve_client.post,
            "/api/channel/groups/resolve",
            json={**payload, "group_name": "Equipo concurrente actualizado"},
        )
        try:
            assert snapshot_taken.wait(timeout=3)
            message = client.post(
                f"/api/sessions/{session_id}/channel/messages",
                json={
                    "sender": "Ana",
                    "text": "yo puedo lunes en la tarde",
                    "message_id": "wa-concurrent-resolve-001",
                },
            )
            assert message.status_code == 200
        finally:
            release_resolve.set()
        resolved = future.result(timeout=3)

    assert resolved.status_code == 200
    final_session = client.get(f"/api/sessions/{session_id}").json()["session"]
    external_ids = {
        item["external_id"]
        for item in final_session["channel_messages"]
        if item["kind"] == "human"
    }
    assert "wa-concurrent-resolve-001" in external_ids
    assert final_session["channel_config"]["group_name"] == "Equipo concurrente actualizado"


def test_duplicate_non_invoking_message_is_stored_once(client):
    session_id = client.post("/api/sessions", json={"title": "Idempotencia"}).json()["session"]["id"]
    payload = {"sender": "Ana", "text": "yo puedo lunes en la tarde", "message_id": "wa-group:ana:001"}

    first = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()
    second = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()

    humans = [message for message in second["session"]["channel_messages"] if message["kind"] == "human"]
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(humans) == 1
    assert humans[0]["external_id"] == payload["message_id"]


def test_duplicate_confirm_replays_reply_and_calendar_without_reconfirming(client):
    session_id = create_calculated_channel_session(client)
    payload = {"sender": "Nicolas", "text": "@coordina confirma 1", "message_id": "wa-confirm-001"}

    first = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()
    first_message_count = len(first["session"]["channel_messages"])
    second = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()

    assert first["session"]["status"] == "confirmed"
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["llm_source"] == "idempotent_replay"
    assert second["agent_reply"] == first["agent_reply"]
    assert second["agent_reply_document"] == first["agent_reply_document"]
    assert len(second["session"]["decision_history"]) == 1
    assert len(second["session"]["channel_messages"]) == first_message_count


@pytest.mark.parametrize("command", ["resumen", "faltan"])
def test_read_only_commands_preserve_confirmed_status(client, command):
    session_id = create_calculated_channel_session(client)
    confirmed = confirm_first_option(client, session_id)
    selected_id = confirmed["selected_option"]["id"]
    history_count = len(confirmed["decision_history"])

    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Nicolas", "text": f"@coordina {command}"},
    )
    session = response.json()["session"]

    assert response.status_code == 200
    assert session["status"] == "confirmed"
    assert session["selected_option"]["id"] == selected_id
    assert len(session["decision_history"]) == history_count
    if command == "resumen":
        assert "Decision confirmada" in (response.json()["agent_reply"] or "")


@pytest.mark.parametrize(
    ("index", "label"),
    [("0", "0"), ("4", "4"), ("9", "9"), ("12", "12"), ("-1", "-1"), ("+4", "+4"), ("1.5", "1.5"), ("4ta opcion", "4ta")],
)
def test_invalid_confirm_index_never_falls_back_to_option_one(client, index, label):
    session_id = create_calculated_channel_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Nicolas", "text": f"@coordina confirma {index}"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["session"]["status"] == "calculated"
    assert body["session"]["selected_option"] is None
    assert body["session"]["decision_history"] == []
    assert body["agent_reply_document"] is None
    assert f"No encuentro la opcion {label}" in (body["agent_reply"] or "")


def test_confirmed_calendar_date_does_not_move_after_event(client):
    session_id = create_calculated_channel_session(client)
    confirmed_payload = confirm_first_option(client, session_id)
    session = Session.model_validate(confirmed_payload)

    assert session.selected_event_date
    first = build_calendar_ics(session, now=datetime(2026, 7, 1, tzinfo=timezone.utc))
    much_later = build_calendar_ics(session, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    expected = session.selected_event_date.replace("-", "")

    assert f"DTSTART;TZID=America/Santiago:{expected}" in first
    assert _line_starting(first, "DTSTART") == _line_starting(much_later, "DTSTART")
    assert _line_starting(first, "DTEND") == _line_starting(much_later, "DTEND")
    assert _line_starting(first, "UID") == _line_starting(much_later, "UID")


def test_confirmed_calendar_snapshot_survives_timezone_change(client, monkeypatch):
    session_id = create_calculated_channel_session(client)
    session = Session.model_validate(confirm_first_option(client, session_id))
    original = build_calendar_ics(session)

    monkeypatch.setattr(settings, "app_timezone", "UTC")
    replayed = build_calendar_ics(session, now=datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert _line_starting(original, "DTSTART") == _line_starting(replayed, "DTSTART")
    assert _line_starting(original, "DTEND") == _line_starting(replayed, "DTEND")
    assert _line_starting(original, "DTSTAMP") == _line_starting(replayed, "DTSTAMP")
    assert _line_starting(original, "UID") == _line_starting(replayed, "UID")
    assert "TZID=America/Santiago" in replayed


def test_confirm_crash_replays_atomic_receipt_without_second_decision(client, monkeypatch):
    session_id = create_calculated_channel_session(client)
    original_add_reply = session_module.session_service.add_agent_channel_reply

    def crash_after_decision(*args, **kwargs):
        raise RuntimeError("crash after receipt")

    monkeypatch.setattr(session_module.session_service, "add_agent_channel_reply", crash_after_decision)
    safe_client = TestClient(app, raise_server_exceptions=False)
    payload = {"sender": "Nicolas", "text": "@coordina confirma 1", "message_id": "wa-crash-confirm"}
    first = safe_client.post(f"/api/sessions/{session_id}/channel/messages", json=payload)
    assert first.status_code == 500

    after_crash = session_module.session_service.get(session_id)
    assert len(after_crash.decision_history) == 1
    assert len(after_crash.channel_command_receipts) == 1
    monkeypatch.setattr(session_module.session_service, "add_agent_channel_reply", original_add_reply)

    replay = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()
    assert replay["duplicate"] is True
    assert replay["llm_source"] == "idempotent_replay"
    assert replay["agent_reply_document"]
    assert len(replay["session"]["decision_history"]) == 1


def test_cancel_crash_replays_receipt_instead_of_wrong_no_decision_reply(client, monkeypatch):
    session_id = create_calculated_channel_session(client)
    confirm_first_option(client, session_id)
    original_add_reply = session_module.session_service.add_agent_channel_reply
    monkeypatch.setattr(
        session_module.session_service,
        "add_agent_channel_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after cancel")),
    )
    safe_client = TestClient(app, raise_server_exceptions=False)
    payload = {"sender": "Nicolas", "text": "@coordina cancela", "message_id": "wa-crash-cancel"}
    assert safe_client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).status_code == 500
    monkeypatch.setattr(session_module.session_service, "add_agent_channel_reply", original_add_reply)

    replay = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()
    assert replay["duplicate"] is True
    assert "Decision cancelada" in replay["agent_reply"]
    assert replay["session"]["selected_option"] is None
    assert len(replay["session"]["channel_command_receipts"]) == 1


def test_remove_crash_replays_receipt_without_removing_twice(client, monkeypatch):
    session_id = create_calculated_channel_session(client)
    original_add_reply = session_module.session_service.add_agent_channel_reply
    monkeypatch.setattr(
        session_module.session_service,
        "add_agent_channel_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after remove")),
    )
    safe_client = TestClient(app, raise_server_exceptions=False)
    payload = {"sender": "Nicolas", "text": "@coordina quitar Camila", "message_id": "wa-crash-remove"}
    assert safe_client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).status_code == 500
    monkeypatch.setattr(session_module.session_service, "add_agent_channel_reply", original_add_reply)

    replay = client.post(f"/api/sessions/{session_id}/channel/messages", json=payload).json()
    names = {participant["name"] for participant in replay["session"]["participants"]}
    assert replay["duplicate"] is True
    assert "quite a camila" in replay["agent_reply"].lower()
    assert "Camila" not in names
    assert len(replay["session"]["channel_command_receipts"]) == 1


def test_confirmed_summary_includes_the_calendar_it_promises(client):
    session_id = create_calculated_channel_session(client)
    confirm_first_option(client, session_id)
    response = client.post(
        f"/api/sessions/{session_id}/channel/messages",
        json={"sender": "Nicolas", "text": "@coordina resumen"},
    ).json()

    assert "adjunto" in response["agent_reply"].lower()
    assert response["agent_reply_document"]
    assert response["agent_reply_document_mimetype"] == "text/calendar"


def test_legacy_decision_uses_confirmation_timestamp_as_calendar_anchor():
    option = TimeOption(
        day="lunes",
        start="16:00",
        end="17:00",
        available_participants=["Ana"],
        score=1,
        coverage_percent=100,
    )
    session = Session(
        title="Decision antigua",
        selected_option=option,
        decision_summary="confirmada",
        status="confirmed",
        decision_history=[
            DecisionRecord(
                option=option,
                summary="confirmada",
                created_at="2026-07-08T14:00:00+00:00",
            )
        ],
    )

    assert session.selected_event_date is None
    assert confirmed_event_date(session, datetime(2030, 1, 1, tzinfo=timezone.utc)).isoformat() == "2026-07-13"


def test_ics_uses_single_newline_escape_and_folds_utf8_by_octets():
    option = TimeOption(
        day="miercoles",
        start="10:00",
        end="11:00",
        available_participants=["Iñaki", "María José"],
        unavailable_participants=["Muñoz"],
        score=2,
        coverage_percent=67,
    )
    session = Session(
        title="Reunión Ñandú " + "áéíóú" * 18,
        selected_option=option,
        selected_event_date="2026-07-15",
        decision_summary="confirmada",
        status="confirmed",
    )

    ics = build_calendar_ics(session)
    physical_lines = [line for line in ics.split("\r\n") if line]
    assert all(len(line.encode("utf-8")) <= 75 for line in physical_lines)
    assert "\n" not in ics.replace("\r\n", "")

    unfolded = ics.replace("\r\n ", "")
    description = _line_starting(unfolded, "DESCRIPTION")
    assert "\\nAsisten:" in description
    assert "\\\\n" not in description


def _line_starting(ics: str, prefix: str) -> str:
    unfolded = ics.replace("\r\n ", "")
    return next(line for line in unfolded.split("\r\n") if line.startswith(prefix))
