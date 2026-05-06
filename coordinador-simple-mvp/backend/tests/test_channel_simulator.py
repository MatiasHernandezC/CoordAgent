from pathlib import Path

from app.services.llm_service import llm_service
from app.services.session_service import SessionService, build_channel_reply
from app.settings import settings
from app.storage.json_repository import JsonRepository


def test_channel_invocation_structures_messages_and_replies(tmp_path: Path):
    settings.llm_provider = "mock"
    service = SessionService()
    session_module = __import__("app.services.session_service", fromlist=["repository"])
    session_module.repository = JsonRepository(tmp_path / "sessions.json")

    session = service.create("Reunion desde canal")
    session = service.configure_channel(session.id, True, "@coordina")
    session, invoked = service.add_channel_message(session.id, "Nicolas", "yo puedo lunes en la tarde")
    assert invoked is False

    session, invoked = service.add_channel_message(session.id, "Camila", "yo puedo lunes desde las 16")
    assert invoked is False

    session, invoked = service.add_channel_message(session.id, "Diego", "@coordina nos ayudas a cerrar horario?")
    assert invoked is True

    extraction, source = llm_service.extract_channel_availability(session.channel_messages)
    session = service.merge_extraction(session.id, extraction, "Invocacion desde canal simulado.", source)
    session = service.calculate(session.id)
    reply = build_channel_reply(session)
    session = service.add_agent_channel_reply(session.id, reply)

    assert [participant.name for participant in session.participants] == ["Nicolas", "Camila"]
    assert session.options[0].day == "lunes"
    assert session.channel_messages[-1].kind == "agent"
    assert "Mejor opcion sugerida" in session.channel_messages[-1].text


def test_batch_channel_messages_save_once_and_detect_invocation(tmp_path: Path):
    service = SessionService()
    session_module = __import__("app.services.session_service", fromlist=["repository"])
    session_module.repository = JsonRepository(tmp_path / "sessions.json")

    session = service.create("Batch")
    session, invoked = service.add_channel_messages(
        session.id,
        [
            ("Nicolas", "yo puedo lunes en la tarde"),
            ("Camila", "yo puedo lunes desde las 16"),
            ("Nicolas", "@coordina cerrar horario"),
        ],
    )

    assert invoked is True
    assert len(session.channel_messages) == 3
    assert session.channel_messages[-1].detected_invocation is True
