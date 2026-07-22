import csv
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from io import StringIO
from threading import Lock, RLock

from fastapi import HTTPException

from app.schemas import (
    ChannelMessage,
    ChatMessage,
    DecisionRecord,
    ExtractedAvailability,
    Participant,
    ProcessingSummary,
    Session,
    TimeOption,
    TimeSlot,
    TokenUsage,
)
from app.services.calendar_export import (
    build_google_calendar_url,
    format_long_date,
    format_short_date,
    option_event_date,
)
from app.services.decision_engine import build_availability_matrix, build_insights, find_missing_info, options_from_matrix
from app.settings import settings

# Selector de almacenamiento por DB_BACKEND (postgres por defecto, json como respaldo
# sin dependencias externas). Solo se importa el modulo elegido.
if settings.db_backend == "json":
    from app.storage.json_repository import repository
else:
    from app.storage.postgres_repository import repository


class SessionService:
    def __init__(self) -> None:
        # FastAPI ejecuta endpoints sync en un threadpool. Cada lock protege el
        # ciclo completo read-modify-write de una sesion sin bloquear grupos
        # distintos entre si.
        self._session_locks_guard = Lock()
        self._session_locks = {}

    @contextmanager
    def session_lock(self, session_id: str):
        with self._session_locks_guard:
            lock = self._session_locks.setdefault(session_id, RLock())

        with lock:
            yield

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

    def list_sessions(self) -> list[Session]:
        return repository.list_all()

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

        session.participants = merge_duplicate_participants(session.participants)

        for incoming in extraction.participants:
            existing = find_participant(session.participants, incoming.name)
            if existing:
                existing.availability = merge_slots(existing.availability, incoming.availability)
            else:
                session.participants.append(incoming)

        # Disponibilidad implicita ("no puedo despues de las 16" => puedo antes):
        # solo se aplica en dias donde la persona no declaro nada, para nunca
        # ampliar una disponibilidad explicita mas acotada.
        for implied in extraction.implied:
            if not implied.slots:
                continue
            participant = find_participant(session.participants, implied.participant_name)
            if not participant:
                participant = Participant(name=implied.participant_name.strip())
                session.participants.append(participant)
            days_with_availability = {(slot.week_offset, slot.day) for slot in participant.availability}
            new_slots = [slot for slot in implied.slots if (slot.week_offset, slot.day) not in days_with_availability]
            if new_slots:
                participant.availability = merge_slots(participant.availability, new_slots)

        removed_names: list[str] = []
        for removal in extraction.removals:
            if not removal.slots:
                continue

            participant = find_participant(session.participants, removal.participant_name)
            if not participant:
                participant = Participant(name=removal.participant_name.strip())
                session.participants.append(participant)

            participant.availability = remove_slots(participant.availability, removal.slots)
            removed_names.append(participant.name)

        has_schedule_update = bool(
            extraction.participants
            or any(removal.slots for removal in extraction.removals)
            or any(item.slots for item in extraction.implied)
        )
        refresh_schedule_state(session, clear_decision=has_schedule_update)
        session.last_processing = build_processing_summary(source, extraction, token_usage)
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
        return repository.save(session)

    def add_participant(self, session_id: str, name: str) -> Session:
        session = self.get(session_id)
        session.participants = merge_duplicate_participants(session.participants)
        existing = find_participant(session.participants, name)
        if not existing:
            session.participants.append(Participant(name=name.strip()))

        refresh_schedule_state(session)
        session.messages.append(ChatMessage(role="assistant", content=f"Participante agregado: {name.strip()}."))
        return repository.save(session)

    def add_availability(self, session_id: str, participant_name: str, slot: TimeSlot) -> Session:
        session = self.get(session_id)
        session.participants = merge_duplicate_participants(session.participants)
        participant = find_participant(session.participants, participant_name)
        if not participant:
            participant = Participant(name=participant_name.strip())
            session.participants.append(participant)

        participant.availability = merge_slots(participant.availability, [slot])
        refresh_schedule_state(session)
        session.messages.append(
            ChatMessage(
                role="assistant",
                content=f"Disponibilidad agregada para {participant.name}: {slot.day} {slot.start}-{slot.end}.",
            )
        )
        return repository.save(session)

    def remove_participant(self, session_id: str, name: str) -> Session:
        session = self.get(session_id)
        before = len(session.participants)
        session.participants = [
            participant
            for participant in merge_duplicate_participants(session.participants)
            if participant_key(participant.name) != participant_key(name)
        ]
        if len(session.participants) == before:
            raise HTTPException(status_code=404, detail="Participant not found")

        refresh_schedule_state(session)
        session.messages.append(ChatMessage(role="assistant", content=f"Participante quitado: {name.strip()}."))
        return repository.save(session)

    def calculate(self, session_id: str) -> Session:
        session = self.get(session_id)
        matrix = build_availability_matrix(session)
        session.availability_matrix = matrix
        session.options = options_from_matrix(matrix)
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

    def confirm(
        self,
        session_id: str,
        option_id: str,
        confirmed_by: str = "admin",
        source: str = "panel",
    ) -> Session:
        session = self.get(session_id)
        option = find_option(session.options, option_id)
        if not option:
            raise HTTPException(status_code=400, detail="Option not found")

        session.selected_option = option
        session.decision_summary = build_summary(session, option)
        session.decision_history.append(
            DecisionRecord(
                option=option.model_copy(deep=True),
                summary=session.decision_summary,
                confirmed_by=confirmed_by.strip() or "admin",
                source=source if source in {"panel", "whatsapp", "api"} else "api",
            )
        )
        session.messages.append(ChatMessage(role="assistant", content=session.decision_summary))
        session.insights = build_insights(session)
        session.status = "confirmed"
        return repository.save(session)

    def cancel_decision(self, session_id: str) -> Session:
        session = self.get(session_id)
        if not session.selected_option and not session.decision_summary:
            raise HTTPException(status_code=400, detail="Session has no confirmed decision")

        session.selected_option = None
        session.decision_summary = None
        session.status = "calculated" if session.options else "draft"
        session.messages.append(ChatMessage(role="assistant", content="Decision cancelada. Puedes confirmar otra opcion."))
        return repository.save(session)

    def archive(self, session_id: str) -> Session:
        session = self.get(session_id)
        if not session.archived_at:
            session.archived_at = datetime.now(timezone.utc).isoformat()
            session.messages.append(ChatMessage(role="system", content="Sesion archivada desde el panel."))
        return repository.save(session)

    def reopen(self, session_id: str) -> Session:
        session = self.get(session_id)
        if session.archived_at:
            session.archived_at = None
            session.messages.append(ChatMessage(role="system", content="Sesion reabierta desde el panel."))
        return repository.save(session)

    def configure_channel(
        self,
        session_id: str,
        listening_enabled: bool,
        trigger_word: str,
        reply_format: str | None = None,
        group_jid: str | None = None,
        group_name: str | None = None,
        group_participant_count: int | None = None,
    ) -> Session:
        session = self.get(session_id)
        session.channel_config.listening_enabled = listening_enabled
        session.channel_config.trigger_word = trigger_word.strip()
        if reply_format is not None:
            session.channel_config.reply_format = reply_format
        if group_jid is not None:
            session.channel_config.group_jid = group_jid.strip() or None
        if group_name is not None:
            cleaned_group_name = group_name.strip()
            session.channel_config.group_name = cleaned_group_name or None
            if cleaned_group_name and (
                session.title.startswith("WhatsApp - ") or session.title == "Coordinacion WhatsApp"
            ):
                session.title = f"WhatsApp - {cleaned_group_name}"
        if group_participant_count is not None:
            session.channel_config.group_participant_count = group_participant_count
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


def refresh_schedule_state(session: Session, *, clear_decision: bool = True) -> None:
    session.participants = merge_duplicate_participants(session.participants)
    session.missing_info = find_missing_info(session)
    session.availability_matrix = build_availability_matrix(session)
    session.options = options_from_matrix(session.availability_matrix)
    if clear_decision:
        clear_stale_decision(session)
    session.insights = build_insights(session)
    if clear_decision or session.status != "confirmed":
        session.status = "calculated" if session.options else "draft"


def clear_stale_decision(session: Session) -> None:
    if not session.selected_option and not session.decision_summary:
        return

    session.selected_option = None
    session.decision_summary = None
    session.messages.append(
        ChatMessage(
            role="assistant",
            content="Decision anterior pendiente de reconfirmacion porque cambio la disponibilidad.",
        )
    )


def build_processing_summary(
    source: str | None,
    extraction: ExtractedAvailability,
    token_usage: TokenUsage | None,
) -> ProcessingSummary:
    source_value = source or "unknown"
    participants_detected = len(extraction.participants)
    removals_detected = sum(1 for removal in extraction.removals if removal.slots)
    cached = "_cache" in source_value or bool(token_usage and token_usage.cached)
    fallback_used = "fallback" in source_value
    has_new_data = participants_detected > 0 or removals_detected > 0

    if "no_new_availability" in source_value:
        confidence = "low"
        label = "Baja"
        detail = "No se detectaron datos nuevos de disponibilidad en los mensajes pendientes."
    elif has_new_data and not fallback_used:
        confidence = "high"
        label = "Alta"
        detail = "Se detectaron nombres y horarios con el extractor principal mas reglas de control."
    elif has_new_data:
        confidence = "medium"
        label = "Media"
        detail = "Se detectaron datos usando el respaldo por reglas; conviene revisar si el mensaje era ambiguo."
    else:
        confidence = "low"
        label = "Baja"
        detail = "La invocacion no trajo nombres u horarios claros."

    return ProcessingSummary(
        source=source_value,
        confidence=confidence,
        confidence_label=label,
        detail=detail,
        participants_detected=participants_detected,
        removals_detected=removals_detected,
        cached=cached,
        fallback_used=fallback_used,
    )


def participant_key(name: str) -> str:
    decomposed = unicodedata.normalize("NFD", name.strip().lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(without_accents.split())


def find_participant(participants: list[Participant], name: str) -> Participant | None:
    key = participant_key(name)
    return next((participant for participant in participants if participant_key(participant.name) == key), None)


def merge_duplicate_participants(participants: list[Participant]) -> list[Participant]:
    merged: list[Participant] = []
    by_key: dict[str, Participant] = {}

    for participant in participants:
        key = participant_key(participant.name)
        existing = by_key.get(key)
        if existing:
            existing.availability = merge_slots(existing.availability, participant.availability)
            continue

        by_key[key] = participant
        merged.append(participant)

    return merged


def should_invoke(session: Session, text: str) -> bool:
    if not session.channel_config.listening_enabled:
        return False

    trigger = session.channel_config.trigger_word.strip().lower()
    return bool(trigger and trigger in text.lower())


def merge_slots(existing: list[TimeSlot], incoming: list[TimeSlot]) -> list[TimeSlot]:
    by_key = {(slot.week_offset, slot.day, slot.start, slot.end): slot for slot in existing}
    for slot in incoming:
        by_key[(slot.week_offset, slot.day, slot.start, slot.end)] = slot
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
    # Solo se restan bloques del mismo dia Y de la misma semana.
    if slot.day != removal.day or slot.week_offset != removal.week_offset:
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
        remaining.append(TimeSlot(day=slot.day, start=minutes_to_time(slot_start), end=minutes_to_time(overlap_start), week_offset=slot.week_offset))
    if overlap_end < slot_end:
        remaining.append(TimeSlot(day=slot.day, start=minutes_to_time(overlap_end), end=minutes_to_time(slot_end), week_offset=slot.week_offset))
    return remaining


def time_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def minutes_to_time(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def build_summary(session: Session, option: TimeOption) -> str:
    available = ", ".join(option.available_participants) or "sin participantes confirmados"
    unavailable = ", ".join(option.unavailable_participants)
    summary = (
        f"{session.title}: decision confirmada para {option.day} "
        f"de {option.start} a {option.end}. Asisten: {available}."
    )
    if unavailable:
        summary += f" No calzan con este horario: {unavailable}."
    return summary


def _channel_header(session: Session) -> str:
    name = session.channel_config.group_name
    return f"*Coordina - {name}*" if name else "*Coordina*"


def _option_line(option: TimeOption, index: int, now: datetime | None = None, detailed: bool = False) -> str:
    event_date = format_short_date(option_event_date(option, now))
    suffix = f"{option.coverage_percent}% de cobertura" if detailed else f"{option.coverage_percent}%"
    return f"{index}. {option.day.capitalize()} {event_date}, {option.start}-{option.end} - {suffix}"


def build_channel_reply(session: Session, now: datetime | None = None) -> str:
    """Mensaje profesional para WhatsApp (usa *negrita* nativa de WhatsApp)."""
    header = _channel_header(session)
    trigger = session.channel_config.trigger_word

    if not session.participants:
        return (
            f"{header}\n\n"
            "Aun no detecto disponibilidades claras.\n"
            "Escriban por ejemplo _yo puedo lunes en la tarde_ y vuelvan a invocarme con "
            f"{trigger}."
        )

    count = session.channel_config.group_participant_count or len(session.participants)
    lines = [header]
    if count:
        lines.append(f"_{count} participante(s)_")
    lines.append("")

    if session.options:
        best = session.options[0]
        lines.append("*Mejor opcion*")
        lines.append(_option_line(best, 1, now, detailed=True))
        if best.available_participants:
            lines.append(f"Asisten: {', '.join(best.available_participants)}")
        if best.unavailable_participants:
            lines.append(f"No calzan: {', '.join(best.unavailable_participants)}")

        others = session.options[1:]
        if others:
            lines.append("")
            lines.append("*Otras opciones*")
            for index, option in enumerate(others, start=2):
                lines.append(_option_line(option, index, now))
        lines.append("")
        lines.append(f"Para cerrar: escribe *{trigger} confirmar 1*.")
    else:
        lines.append("Aun no hay una opcion calculable con la informacion disponible.")

    if session.missing_info:
        lines.append("")
        lines.append("*Falta informacion*")
        lines.extend(f"- {item}" for item in session.missing_info)

    lines.append("")
    lines.append(f"Comandos: *{trigger} faltan*, *{trigger} exportar*, *{trigger} ayuda*.")

    return "\n".join(lines)


def build_confirmed_channel_reply(session: Session, now: datetime | None = None) -> str:
    header = _channel_header(session)
    if not session.selected_option:
        return f"{header}\n\nNo hay una opcion confirmada todavia."

    option = session.selected_option
    event_date = option_event_date(option, now)
    lines = [
        header,
        "",
        "*Decision confirmada*",
        f"{option.day.capitalize()} {format_long_date(event_date)}, {option.start} a {option.end}",
    ]
    if option.available_participants:
        lines.append(f"Asisten: {', '.join(option.available_participants)}")
    if option.unavailable_participants:
        lines.append(f"No calzan: {', '.join(option.unavailable_participants)}")

    google_url = build_google_calendar_url(session, now)
    if google_url:
        lines.append("")
        lines.append("Agregar a tu calendario:")
        lines.append(google_url)
        lines.append("_Tambien adjunto el evento (.ics) para Outlook/Apple._")

    lines.append("")
    lines.append(f"Si alguien cambia su disponibilidad, escriban el cambio y luego *{session.channel_config.trigger_word}*.")
    return "\n".join(lines)


def build_help_reply(session: Session) -> str:
    trigger = session.channel_config.trigger_word
    header = _channel_header(session)
    return "\n".join(
        [
            header,
            "",
            "Coordino horarios de reunion leyendo lo que escriben en el grupo.",
            "",
            "*Como declarar disponibilidad*",
            "- _yo puedo lunes en la tarde_",
            "- _estoy libre mierc de 15:30 a 17:00_",
            "- _no me va bien martes de 10 a 12_",
            "- _hoy despues de las 4_",
            "",
            "*Comandos*",
            f"- *{trigger}*  ·  propone los mejores horarios",
            f"- *{trigger} confirma* (o _confirma 2_)  ·  cierra la decision y envia el evento al calendario",
            f"- *{trigger} cancela*  ·  deshace la decision confirmada",
            f"- *{trigger} resumen*  ·  estado actual y opciones",
            f"- *{trigger} faltan*  ·  quienes no han dado su horario",
            f"- *{trigger} exportar*  ·  reporte de la sesion",
            f"- *{trigger} quita a <nombre>*  ·  elimina un participante",
            "",
            "*Formato de respuesta*",
            f"- *{trigger} con imagen*  ·  incluye el calendario grafico",
            f"- *{trigger} solo texto*  ·  sin imagen",
        ]
    )


def build_cancelled_channel_reply(session: Session) -> str:
    header = _channel_header(session)
    lines = [header, "", "*Decision cancelada*"]
    if session.options:
        best = session.options[0]
        lines.append(
            f"Las opciones siguen disponibles. La mejor era {best.day.capitalize()} "
            f"{best.start}-{best.end} ({best.coverage_percent}%)."
        )
        lines.append(f"Para cerrar de nuevo: *{session.channel_config.trigger_word} confirma*.")
    else:
        lines.append("Escriban disponibilidades y vuelvan a invocarme para proponer horarios.")
    return "\n".join(lines)


def build_missing_info_reply(session: Session) -> str:
    header = _channel_header(session)
    if not session.missing_info:
        return f"{header}\n\nNo tengo pendientes criticos. Ya hay datos suficientes para proponer horario."

    lines = [header, "", "*Pendientes*"]
    lines.extend(f"- {item}" for item in session.missing_info[:12])
    if len(session.missing_info) > 12:
        lines.append(f"- Y {len(session.missing_info) - 12} pendiente(s) mas.")
    return "\n".join(lines)


def build_participant_summary(session: Session) -> str:
    if not session.participants:
        return "Sin participantes detectados."

    lines: list[str] = []
    for participant in sorted(session.participants, key=lambda item: participant_key(item.name)):
        if participant.availability:
            slots = ", ".join(f"{slot.day} {slot.start}-{slot.end}" for slot in participant.availability)
            lines.append(f"{participant.name}: {slots}")
        else:
            lines.append(f"{participant.name}: sin disponibilidad")
    return "\n".join(lines)


def build_export_text(session: Session) -> str:
    group_name = session.channel_config.group_name or session.title
    lines = [
        "Coordina - reporte de sesion",
        f"Grupo: {group_name}",
        f"Estado: {session.status}",
        f"Participantes detectados: {len(session.participants)}",
        "",
        "Disponibilidades por persona:",
        build_participant_summary(session),
        "",
        "Opciones calculadas:",
    ]

    if session.options:
        for index, option in enumerate(session.options, start=1):
            lines.append(
                f"{index}. {option.day.capitalize()} {option.start}-{option.end} "
                f"({option.coverage_percent}%): {', '.join(option.available_participants) or 'sin asistentes'}"
            )
            if option.unavailable_participants:
                lines.append(f"   No calzan: {', '.join(option.unavailable_participants)}")
    else:
        lines.append("Sin opciones calculadas.")

    if session.missing_info:
        lines.extend(["", "Pendientes:"])
        lines.extend(f"- {item}" for item in session.missing_info)

    if session.decision_summary:
        lines.extend(["", "Decision:", session.decision_summary])

    if session.last_processing:
        lines.extend(
            [
                "",
                "Procesamiento:",
                (
                    f"{session.last_processing.confidence_label} - "
                    f"{session.last_processing.source} - {session.last_processing.detail}"
                ),
            ]
        )

    return "\n".join(lines)


def build_export_csv(session: Session) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "tipo",
            "grupo",
            "estado",
            "nombre",
            "dia",
            "inicio",
            "fin",
            "cobertura_pct",
            "asisten",
            "no_calzan",
        ]
    )

    group_name = session.channel_config.group_name or session.title
    for participant in sorted(session.participants, key=lambda item: participant_key(item.name)):
        if not participant.availability:
            writer.writerow(["disponibilidad", group_name, session.status, participant.name, "", "", "", "", "", ""])
            continue

        for slot in participant.availability:
            writer.writerow(
                [
                    "disponibilidad",
                    group_name,
                    session.status,
                    participant.name,
                    slot.day,
                    slot.start,
                    slot.end,
                    "",
                    "",
                    "",
                ]
            )

    for index, option in enumerate(session.options, start=1):
        writer.writerow(
            [
                f"opcion_{index}",
                group_name,
                session.status,
                "",
                option.day,
                option.start,
                option.end,
                option.coverage_percent,
                ", ".join(option.available_participants),
                ", ".join(option.unavailable_participants),
            ]
        )

    if session.selected_option:
        option = session.selected_option
        writer.writerow(
            [
                "decision",
                group_name,
                session.status,
                "",
                option.day,
                option.start,
                option.end,
                option.coverage_percent,
                ", ".join(option.available_participants),
                ", ".join(option.unavailable_participants),
            ]
        )

    return output.getvalue()


def build_channel_caption(session: Session) -> str:
    """Pie de foto breve para cuando se envia solo la imagen del calendario."""
    header = _channel_header(session)
    if session.options:
        best = session.options[0]
        event_date = format_short_date(option_event_date(best))
        return (
            f"{header}\n"
            f"Mejor opcion: {best.day.capitalize()} {event_date}, {best.start}-{best.end} ({best.coverage_percent}%)"
        )
    return f"{header}\nCalendario de disponibilidad del grupo."


def last_invoking_text(session: Session) -> str:
    for message in reversed(session.channel_messages):
        if message.kind == "human" and message.detected_invocation:
            return message.text
    return ""


def last_invoking_sender(session: Session) -> str:
    for message in reversed(session.channel_messages):
        if message.kind == "human" and message.detected_invocation:
            return message.sender
    return "admin"


def classify_channel_command(session: Session) -> dict | None:
    raw = last_invoking_text(session)
    if not raw:
        return None

    text = _strip_accents(raw)
    trigger = _strip_accents(session.channel_config.trigger_word)
    command_text = text.replace(trigger, "", 1).strip() if trigger else text

    if not command_text:
        return None

    if re.search(r"\b(ayuda|help|comandos|instrucciones)\b", command_text):
        return {"name": "help"}

    number_match = re.search(r"\b(?:opcion|alternativa)?\s*([1-3])\b", command_text)
    if re.search(r"\b(confirmar|confirma|confirmo)\b", command_text) or (
        re.search(r"\b(cerrar|cierra)\b", command_text) and number_match
    ):
        option_index = int(number_match.group(1)) if number_match else 1
        return {"name": "confirm", "option_index": option_index}

    if re.search(r"\b(cancelar|cancela|cancelen|anular|anula|deshacer|deshaz)\b", command_text):
        return {"name": "cancel"}

    if re.search(r"\b(faltan|pendientes|quien falta|quienes faltan|datos faltantes)\b", command_text):
        return {"name": "missing"}

    if re.search(r"\b(exportar|informe|reporte|respaldo)\b", command_text):
        return {"name": "export"}

    if re.search(r"\b(resumen|estado|status|opciones actuales)\b", command_text):
        return {"name": "summary"}

    remove_match = re.search(
        r"\b(?:quita|quitar|elimina|eliminar|saca|sacar|olvida|olvidar)\s+(?:a\s+)?([a-z0-9_-]{2,80})\b",
        command_text,
    )
    if remove_match:
        return {"name": "remove_participant", "participant_name": remove_match.group(1)}

    return None


def resolve_reply_format(session: Session) -> str:
    """Formato efectivo: el mensaje que invoca puede pedirlo explicitamente
    (ej. 'con imagen', 'solo el grafico', 'solo texto'); si no, usa el default del canal."""
    default = session.channel_config.reply_format
    invoking = _strip_accents(last_invoking_text(session))
    if not invoking:
        return default

    if any(phrase in invoking for phrase in ("solo texto", "sin imagen", "sin grafico", "no quiero imagen")):
        return "text"

    mentions_image = any(word in invoking for word in ("imagen", "calendario", "grafico"))
    if mentions_image:
        wants_only = "solo" in invoking or "unicamente" in invoking
        return "image" if wants_only else "both"

    return default


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


session_service = SessionService()
