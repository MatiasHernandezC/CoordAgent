from fastapi import HTTPException

from app.schemas import ChannelMessage, ChatMessage, ExtractedAvailability, Participant, Session, TimeOption, TimeSlot, TokenUsage
from app.services.decision_engine import build_availability_matrix, build_insights, calculate_options, find_missing_info
from app.storage.json_repository import repository


class SessionService:
    def create(self, title: str) -> Session:
        session = Session(
            title=title,
            messages=[
                ChatMessage(
                    role="system",
                    content="Sesion creada. Escribe disponibilidad en lenguaje natural para estructurarla.",
                )
            ],
        )
        return repository.save(session)

    def get(self, session_id: str) -> Session:
        session = repository.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def merge_extraction(
        self,
        session_id: str,
        extraction: ExtractedAvailability,
        original_message: str | None = None,
        source: str | None = None,
        token_usage: TokenUsage | None = None,
    ) -> Session:
        session = self.get(session_id)

        if original_message:
            session.messages.append(ChatMessage(role="user", content=original_message))

        for incoming in extraction.participants:
            existing = next((p for p in session.participants if p.name.lower() == incoming.name.lower()), None)
            if existing:
                existing.availability = merge_slots(existing.availability, incoming.availability)
            else:
                session.participants.append(incoming)

        removed_names: list[str] = []
        for removal in extraction.removals:
            if not removal.slots:
                continue

            participant = next((p for p in session.participants if p.name.lower() == removal.participant_name.lower()), None)
            if not participant:
                participant = Participant(name=removal.participant_name.strip())
                session.participants.append(participant)

            participant.availability = remove_slots(participant.availability, removal.slots)
            removed_names.append(participant.name)

        session.missing_info = find_missing_info(session)
        session.availability_matrix = build_availability_matrix(session)
        session.insights = build_insights(session)
        extracted_names = ", ".join(participant.name for participant in extraction.participants) or "sin participantes claros"
        removal_summary = ""
        if removed_names:
            removal_summary = f" Remociones aplicadas: {', '.join(sorted(set(removed_names)))}."
        session.messages.append(
            ChatMessage(
                role="assistant",
                content=f"Extraccion completada: {extracted_names}.{removal_summary} Revisa y corrige si hace falta.",
                source=source,
                token_usage=token_usage,
            )
        )
        session.status = "draft"
        return repository.save(session)

    def add_participant(self, session_id: str, name: str) -> Session:
        session = self.get(session_id)
        existing = next((p for p in session.participants if p.name.lower() == name.lower()), None)
        if not existing:
            session.participants.append(Participant(name=name.strip()))

        session.missing_info = find_missing_info(session)
        session.availability_matrix = build_availability_matrix(session)
        session.insights = build_insights(session)
        session.messages.append(ChatMessage(role="assistant", content=f"Participante agregado: {name.strip()}."))
        session.status = "draft"
        return repository.save(session)

    def add_availability(self, session_id: str, participant_name: str, slot: TimeSlot) -> Session:
        session = self.get(session_id)
        participant = next((p for p in session.participants if p.name.lower() == participant_name.lower()), None)
        if not participant:
            participant = Participant(name=participant_name.strip())
            session.participants.append(participant)

        participant.availability = merge_slots(participant.availability, [slot])
        session.missing_info = find_missing_info(session)
        session.availability_matrix = build_availability_matrix(session)
        session.insights = build_insights(session)
        session.messages.append(
            ChatMessage(
                role="assistant",
                content=f"Disponibilidad agregada para {participant.name}: {slot.day} {slot.start}-{slot.end}.",
            )
        )
        session.status = "draft"
        return repository.save(session)

    def calculate(self, session_id: str) -> Session:
        session = self.get(session_id)
        session.options = calculate_options(session)
        session.availability_matrix = build_availability_matrix(session)
        session.missing_info = find_missing_info(session)
        session.insights = build_insights(session)
        if session.options:
            best = session.options[0]
            session.messages.append(
                ChatMessage(
                    role="assistant",
                    content=f"Calculo listo. Mejor opcion: {best.day} {best.start}-{best.end} con {best.coverage_percent}% de cobertura.",
                )
            )
        session.status = "calculated"
        return repository.save(session)

    def confirm(self, session_id: str, option_id: str) -> Session:
        session = self.get(session_id)
        option = find_option(session.options, option_id)
        if not option:
            raise HTTPException(status_code=400, detail="Option not found")

        session.selected_option = option
        session.decision_summary = build_summary(session, option)
        session.messages.append(ChatMessage(role="assistant", content=session.decision_summary))
        session.insights = build_insights(session)
        session.status = "confirmed"
        return repository.save(session)

    def configure_channel(self, session_id: str, listening_enabled: bool, trigger_word: str) -> Session:
        session = self.get(session_id)
        session.channel_config.listening_enabled = listening_enabled
        session.channel_config.trigger_word = trigger_word.strip()
        session.messages.append(
            ChatMessage(
                role="system",
                content=(
                    "Canal configurado: "
                    f"{'escucha activa' if listening_enabled else 'escucha pausada'}, "
                    f"invocacion {session.channel_config.trigger_word}."
                ),
            )
        )
        return repository.save(session)

    def add_channel_message(self, session_id: str, sender: str, text: str) -> tuple[Session, bool]:
        session = self.get(session_id)
        invoked = should_invoke(session, text)
        session.channel_messages.append(
            ChannelMessage(
                sender=sender.strip(),
                text=text.strip(),
                detected_invocation=invoked,
            )
        )
        return repository.save(session), invoked

    def add_channel_messages(self, session_id: str, messages: list[tuple[str, str]]) -> tuple[Session, bool]:
        session = self.get(session_id)
        invoked = False

        for sender, text in messages:
            message_invoked = should_invoke(session, text)
            invoked = invoked or message_invoked
            session.channel_messages.append(
                ChannelMessage(
                    sender=sender.strip(),
                    text=text.strip(),
                    detected_invocation=message_invoked,
                )
            )

        return repository.save(session), invoked

    def add_agent_channel_reply(self, session_id: str, reply: str) -> Session:
        session = self.get(session_id)
        session.last_agent_reply = reply
        session.channel_messages.append(ChannelMessage(sender="Agente", text=reply, kind="agent"))
        session.messages.append(ChatMessage(role="assistant", content=reply, source="channel_simulator"))
        return repository.save(session)

    def pending_channel_messages(self, session: Session) -> list[ChannelMessage]:
        last_agent_index = -1
        for index, message in enumerate(session.channel_messages):
            if message.kind == "agent":
                last_agent_index = index

        return [
            message
            for message in session.channel_messages[last_agent_index + 1 :]
            if message.kind == "human"
        ]


def find_option(options: list[TimeOption], option_id: str) -> TimeOption | None:
    return next((option for option in options if option.id == option_id), None)


def should_invoke(session: Session, text: str) -> bool:
    if not session.channel_config.listening_enabled:
        return False

    trigger = session.channel_config.trigger_word.strip().lower()
    return bool(trigger and trigger in text.lower())


def merge_slots(existing: list[TimeSlot], incoming: list[TimeSlot]) -> list[TimeSlot]:
    by_key = {(slot.day, slot.start, slot.end): slot for slot in existing}
    for slot in incoming:
        by_key[(slot.day, slot.start, slot.end)] = slot
    return list(by_key.values())


def remove_slots(existing: list[TimeSlot], removals: list[TimeSlot]) -> list[TimeSlot]:
    updated = existing
    for removal in removals:
        next_slots: list[TimeSlot] = []
        for slot in updated:
            next_slots.extend(subtract_slot(slot, removal))
        updated = next_slots
    return merge_slots([], updated)


def subtract_slot(slot: TimeSlot, removal: TimeSlot) -> list[TimeSlot]:
    if slot.day != removal.day:
        return [slot]

    slot_start = time_to_minutes(slot.start)
    slot_end = time_to_minutes(slot.end)
    removal_start = time_to_minutes(removal.start)
    removal_end = time_to_minutes(removal.end)

    overlap_start = max(slot_start, removal_start)
    overlap_end = min(slot_end, removal_end)
    if overlap_start >= overlap_end:
        return [slot]

    remaining: list[TimeSlot] = []
    if slot_start < overlap_start:
        remaining.append(TimeSlot(day=slot.day, start=minutes_to_time(slot_start), end=minutes_to_time(overlap_start)))
    if overlap_end < slot_end:
        remaining.append(TimeSlot(day=slot.day, start=minutes_to_time(overlap_end), end=minutes_to_time(slot_end)))
    return remaining


def time_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def minutes_to_time(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def build_summary(session: Session, option: TimeOption) -> str:
    available = ", ".join(option.available_participants)
    unavailable = ", ".join(option.unavailable_participants)
    summary = (
        f"{session.title}: decision confirmada para {option.day} "
        f"de {option.start} a {option.end}. Asisten: {available}."
    )
    if unavailable:
        summary += f" No calzan con este horario: {unavailable}."
    return summary


def build_channel_reply(session: Session) -> str:
    if not session.participants:
        return (
            "Me invocaron, pero todavia no detecte disponibilidades claras. "
            "Escriban mensajes como 'yo puedo lunes en la tarde' y vuelvan a invocarme."
        )

    lines = ["Coordinacion detectada:"]
    for participant in session.participants:
        if participant.availability:
            slots = ", ".join(f"{slot.day} {slot.start}-{slot.end}" for slot in participant.availability)
        else:
            slots = "sin horario claro"
        lines.append(f"- {participant.name}: {slots}")

    if session.missing_info:
        lines.append("Falta informacion:")
        lines.extend(f"- {item}" for item in session.missing_info)

    if session.options:
        best = session.options[0]
        lines.append(
            "Mejor opcion sugerida: "
            f"{best.day} {best.start}-{best.end} con {best.coverage_percent}% de cobertura."
        )
        lines.append(best.explanation)
    else:
        lines.append("Aun no hay una opcion calculable con la informacion disponible.")

    return "\n".join(lines)


session_service = SessionService()
