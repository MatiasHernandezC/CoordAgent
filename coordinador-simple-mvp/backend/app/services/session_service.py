import csv
import re
import secrets
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from io import StringIO
from threading import Lock, RLock

from fastapi import HTTPException

from app.schemas import (
    ChannelMessage,
    ChannelCommandReceipt,
    ChannelConfig,
    ChatMessage,
    DecisionRecord,
    ExtractedAvailability,
    Participant,
    ParticipantRosterEntry,
    ProcessingSummary,
    Session,
    TimeOption,
    TimeSlot,
    TokenUsage,
)
from app.services.calendar_export import (
    build_calendar_ics,
    build_google_calendar_url,
    confirmed_event_date,
    create_calendar_event_snapshot,
    format_long_date,
    format_short_date,
    option_event_date,
)
from app.services.decision_engine import build_availability_matrix, build_insights, find_missing_info, options_from_matrix
from app.services.habitual_time import compute_habitual_slot
from app.services.llm_service import (
    build_channel_identity_display_names,
    is_usable_channel_display_name,
    resolve_display_name_for_identities,
)
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
        self._group_locks_guard = Lock()
        self._group_locks = {}

    @contextmanager
    def session_lock(self, session_id: str):
        with self._session_locks_guard:
            lock = self._session_locks.setdefault(session_id, RLock())

        with lock:
            yield

    @contextmanager
    def group_lock(self, group_jid: str):
        with self._group_locks_guard:
            lock = self._group_locks.setdefault(group_jid, RLock())
        with lock:
            yield

    def create(
        self,
        title: str,
        *,
        owner_username: str | None = None,
        prepare_whatsapp: bool = False,
    ) -> Session:
        clean_owner = owner_username.strip().lower() if owner_username else None
        session = Session(
            title=title,
            owner_username=clean_owner,
            assigned_usernames=[clean_owner] if clean_owner else [],
            link_code=self._new_link_code() if prepare_whatsapp else None,
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "Grupo creado por su propietario. Agrega el bot a WhatsApp y usa el codigo de vinculacion."
                        if prepare_whatsapp
                        else "Sesion creada. Escribe disponibilidad en lenguaje natural para estructurarla."
                    ),
                )
            ],
        )
        return repository.save(session)

    def _new_link_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        existing = {session.link_code for session in repository.list_all() if session.link_code}
        for _ in range(20):
            code = "".join(secrets.choice(alphabet) for _ in range(10))
            if code not in existing:
                return code
        raise HTTPException(status_code=503, detail="No fue posible generar un codigo de vinculacion.")

    def list_sessions(self) -> list[Session]:
        sessions = repository.list_all()
        for session in sessions:
            if normalize_generated_contact_labels(session):
                repository.save(session)
        return sessions

    def assign_users(self, session_id: str, usernames: list[str]) -> Session:
        session = self.get(session_id)
        session.assigned_usernames = sorted({value.strip().lower() for value in usernames if value.strip()})
        session.messages.append(
            ChatMessage(
                role="system",
                content=(
                    "Acceso del panel actualizado: "
                    + (", ".join(session.assigned_usernames) if session.assigned_usernames else "sin usuarios asignados")
                    + "."
                ),
            )
        )
        return repository.save(session)

    def transfer_owner(self, session_id: str, username: str) -> Session:
        session = self.get(session_id)
        clean_username = username.strip().lower()
        previous_owner = session.owner_username
        assigned = set(session.assigned_usernames)
        if previous_owner:
            assigned.discard(previous_owner)
        assigned.add(clean_username)
        session.owner_username = clean_username
        session.assigned_usernames = sorted(assigned)
        session.messages.append(
            ChatMessage(
                role="system",
                content=(
                    f"Propiedad del grupo transferida de @{previous_owner} a @{clean_username}."
                    if previous_owner
                    else f"@{clean_username} fue asignado como propietario del grupo."
                ),
            )
        )
        return repository.save(session)

    def assign_chief(self, session_id: str, participant_id: str) -> Session:
        session = self.get(session_id)
        participant = next((item for item in session.participants if item.id == participant_id), None)
        if not participant:
            raise HTTPException(status_code=404, detail="La persona seleccionada no pertenece a este grupo.")
        session.chief_participant_id = participant.id
        # El jefe se refleja tambien en el motor existente de prioridades, sin
        # borrar una marca 'required' que el administrador haya configurado.
        for item in session.participants:
            item.priority = 10 if item.id == participant.id else min(item.priority, 9)
        refresh_schedule_state(session)
        session.messages.append(
            ChatMessage(role="system", content=f"{participant.name} fue asignado como jefe del grupo.")
        )
        return repository.save(session)

    def resolve_channel_group(
        self,
        group_jid: str,
        group_name: str,
        group_participant_count: int | None,
        group_participant_ids: list[str],
        coordinator_ids: list[str],
        trigger_word: str,
        channel_label: str = "WhatsApp",
        create_if_missing: bool = True,
        participant_roster: list[ParticipantRosterEntry] | None = None,
    ) -> Session:
        """Create-or-get atomico por JID; un retry nunca crea otra sesion."""
        clean_jid = group_jid.strip()
        clean_name = group_name.strip() or f"grupo de {channel_label}"
        existing = next(
            (
                session
                for session in repository.list_all()
                if session.channel_config.group_jid == clean_jid
            ),
            None,
        )
        if existing:
            # La ruta ya mantiene group_lock. Anidamos siempre group -> session
            # y releemos dentro del segundo lock: la copia obtenida por list_all
            # puede haber quedado obsoleta mientras entraba un mensaje del grupo.
            with self.session_lock(existing.id):
                current = self.get(existing.id)
                if current.channel_config.group_jid == clean_jid:
                    previous_roster = (
                        current.channel_config.group_participant_count,
                        tuple(current.channel_config.group_participant_ids),
                    )
                    current.channel_config.trigger_word = trigger_word.strip()
                    current.channel_config.group_name = clean_name
                    current.channel_config.group_participant_count = group_participant_count
                    current.channel_config.group_participant_ids = unique_ids(group_participant_ids)
                    current.channel_config.coordinator_ids = unique_ids(coordinator_ids)
                    current.title = f"{channel_label} - {clean_name}"
                    roster_hydrated = hydrate_participant_roster(
                        current,
                        participant_roster,
                        current.channel_config.group_participant_ids,
                    )
                    if roster_hydrated or previous_roster != (
                        current.channel_config.group_participant_count,
                        tuple(current.channel_config.group_participant_ids),
                    ):
                        refresh_schedule_state(current)
                    return repository.save(current)

        if not create_if_missing:
            raise HTTPException(status_code=404, detail="Este grupo todavia no esta vinculado a una cuenta.")

        session = Session(
            title=f"{channel_label} - {clean_name}",
            channel_config=ChannelConfig(
                listening_enabled=True,
                trigger_word=trigger_word.strip(),
                group_jid=clean_jid,
                group_name=clean_name,
                group_participant_count=group_participant_count,
                group_participant_ids=unique_ids(group_participant_ids),
                coordinator_ids=unique_ids(coordinator_ids),
            ),
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        f"Sesion creada para {clean_name}. "
                        f"Canal {channel_label} vinculado con invocacion {trigger_word.strip()}."
                    ),
                )
            ],
        )
        hydrate_participant_roster(session, participant_roster, session.channel_config.group_participant_ids)
        refresh_schedule_state(session)
        return repository.save(session)

    def link_channel_group(
        self,
        link_code: str,
        group_jid: str,
        group_name: str,
        group_participant_count: int | None,
        group_participant_ids: list[str],
        coordinator_ids: list[str],
        trigger_word: str,
        participant_roster: list[ParticipantRosterEntry] | None = None,
    ) -> Session:
        clean_code = re.sub(r"[^A-Z0-9]", "", link_code.upper())
        clean_jid = group_jid.strip()
        sessions = repository.list_all()
        target = next(
            (
                session
                for session in sessions
                if session.link_code and secrets.compare_digest(session.link_code, clean_code)
            ),
            None,
        )
        if not target:
            raise HTTPException(status_code=404, detail="Codigo de vinculacion invalido o ya utilizado.")
        if target.archived_at:
            raise HTTPException(status_code=409, detail="El grupo pendiente esta archivado.")

        occupied = next(
            (
                session
                for session in sessions
                if session.id != target.id and session.channel_config.group_jid == clean_jid
            ),
            None,
        )
        if occupied:
            raise HTTPException(status_code=409, detail="Este grupo de WhatsApp ya esta vinculado.")

        with self.session_lock(target.id):
            session = self.get(target.id)
            if not session.link_code or not secrets.compare_digest(session.link_code, clean_code):
                raise HTTPException(status_code=409, detail="El codigo ya fue utilizado.")
            session.channel_config.trigger_word = trigger_word.strip()
            session.channel_config.group_jid = clean_jid
            session.channel_config.group_name = group_name.strip() or session.title
            session.channel_config.group_participant_count = group_participant_count
            session.channel_config.group_participant_ids = sorted(unique_ids(group_participant_ids))
            session.channel_config.coordinator_ids = sorted(unique_ids(coordinator_ids))
            hydrate_participant_roster(session, participant_roster, session.channel_config.group_participant_ids)
            refresh_schedule_state(session)
            session.title = f"WhatsApp - {session.channel_config.group_name}"
            session.linked_at = datetime.now(timezone.utc).isoformat()
            session.link_code = None
            session.messages.append(
                ChatMessage(
                    role="system",
                    content=f"Grupo de WhatsApp vinculado correctamente: {session.channel_config.group_name}.",
                )
            )
            return repository.save(session)

    def get(self, session_id: str) -> Session:
        session = repository.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        changed = normalize_generated_contact_labels(session)
        identity_changed = normalize_roster_display_names(session)
        identity_changed = merge_channel_identity_evidence(session) or identity_changed
        normalized_participants = merge_duplicate_participants(session.participants)
        if normalized_participants != session.participants:
            session.participants = normalized_participants
            identity_changed = True
        changed = identity_changed or changed
        changed = resolve_generated_participant_names(session) or changed
        labels_changed = refresh_option_participant_labels(session)
        changed = labels_changed or changed
        if identity_changed or labels_changed:
            next_missing_info = find_missing_info(session)
            if next_missing_info != session.missing_info:
                session.missing_info = next_missing_info
                changed = True
            if session.status == "confirmed" and session.selected_option:
                rebuilt = build_confirmed_channel_reply(session)
                if rebuilt != session.last_agent_reply:
                    session.last_agent_reply = rebuilt
                    changed = True
            elif session.last_agent_reply:
                rebuilt = build_channel_reply(session)
                if rebuilt != session.last_agent_reply:
                    session.last_agent_reply = rebuilt
                    changed = True
        if changed:
            repository.save(session)
        return session

    def healthcheck(self) -> None:
        repository.healthcheck()

    def merge_extraction(
        self,
        session_id: str,
        extraction: ExtractedAvailability,
        original_message: str | None = None,
        source: str | None = None,
        token_usage: TokenUsage | None = None,
        group_memory=None,
        habitual_used: bool = False,
    ) -> Session:
        session = self.get(session_id)

        if original_message:
            session.messages.append(ChatMessage(role="user", content=original_message))

        session.participants = merge_duplicate_participants(session.participants)
        replacement_keys = {participant_key(name) for name in extraction.replacements}

        for incoming in extraction.participants:
            normalize_participant_identity(incoming)
            existing = find_participant(
                session.participants,
                incoming.name,
                incoming.external_id,
                incoming.external_ids,
            )
            if existing:
                merge_participant_identity(existing, participant_identity_ids(incoming))
                # La persona acaba de escribir: ya no es solo un registro del
                # padron, y si el nombre que tenia era un placeholder (del
                # padron o de una mencion sin resolver), se reemplaza por el
                # nombre real que trajo el mensaje.
                existing.roster_only = False
                was_placeholder = is_generated_contact_name(existing.name) or is_roster_placeholder_name(existing.name)
                if was_placeholder and not is_generated_contact_name(incoming.name):
                    existing.name = incoming.name
                # Si el existente ya tiene nombre humano y la extraccion trae
                # solo el placeholder de mencion numerica, se conserva el nombre.
                if participant_key(incoming.name) in replacement_keys:
                    existing.availability = merge_slots([], incoming.availability)
                else:
                    existing.availability = merge_slots(existing.availability, incoming.availability)
            else:
                session.participants.append(incoming)

        # La identidad tecnica puede haber agregado un PN/LID al participante
        # despues de la primera normalizacion. Repetimos la fusion para evitar
        # que quede un registro historico separado (por ejemplo "Gabriel" y
        # "+569...") en la misma sesion.
        session.participants = merge_duplicate_participants(session.participants)
        resolve_generated_participant_names(session)

        # Disponibilidad implicita ("no puedo despues de las 16" => puedo antes):
        # solo se aplica en dias donde la persona no declaro nada, para nunca
        # ampliar una disponibilidad explicita mas acotada.
        for implied in extraction.implied:
            if not implied.slots:
                continue
            participant = find_participant(
                session.participants,
                implied.participant_name,
                implied.external_id,
                implied.external_ids,
            )
            if not participant:
                participant = Participant(
                    name=implied.participant_name.strip(),
                    external_id=implied.external_id,
                    external_ids=implied.external_ids,
                )
                normalize_participant_identity(participant)
                session.participants.append(participant)
            days_with_availability = {(slot.week_offset, slot.day) for slot in participant.availability}
            new_slots = [slot for slot in implied.slots if (slot.week_offset, slot.day) not in days_with_availability]
            if new_slots:
                participant.availability = merge_slots(participant.availability, new_slots)

        removed_names: list[str] = []
        for removal in extraction.removals:
            if not removal.slots:
                continue

            participant = find_participant(
                session.participants,
                removal.participant_name,
                removal.external_id,
                removal.external_ids,
            )
            if not participant:
                participant = Participant(
                    name=removal.participant_name.strip(),
                    external_id=removal.external_id,
                    external_ids=removal.external_ids,
                )
                normalize_participant_identity(participant)
                session.participants.append(participant)

            participant.availability = remove_slots(participant.availability, removal.slots)
            removed_names.append(participant.name)

        has_schedule_update = bool(
            extraction.participants
            or any(removal.slots for removal in extraction.removals)
            or any(item.slots for item in extraction.implied)
            or extraction.replacements
        )
        refresh_schedule_state(session, clear_decision=has_schedule_update)
        session.last_processing = build_processing_summary(
            source,
            extraction,
            token_usage,
            group_memory=group_memory,
            habitual_used=habitual_used,
        )
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
        validate_slot_in_session_window(session, slot)
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

    def remove_participant(
        self,
        session_id: str,
        name: str,
        external_id: str | None = None,
        *,
        target_external_id: str | None = None,
    ) -> Session:
        session = self.get(session_id)
        before = len(session.participants)
        target_id = clean_external_id(target_external_id)
        previous_participants = merge_duplicate_participants(session.participants)
        removed_ids = {
            participant.id
            for participant in previous_participants
            if (
                target_id in participant_identity_ids(participant)
                if target_id
                else participant_key(participant.name) == participant_key(name)
            )
        }
        session.participants = [
            participant
            for participant in previous_participants
            if (
                target_id not in participant_identity_ids(participant)
                if target_id
                else participant_key(participant.name) != participant_key(name)
            )
        ]
        if len(session.participants) == before:
            raise HTTPException(status_code=404, detail="Participant not found")
        if session.chief_participant_id in removed_ids:
            session.chief_participant_id = None

        refresh_schedule_state(session)
        session.messages.append(ChatMessage(role="assistant", content=f"Participante quitado: {name.strip()}."))
        if external_id:
            reply = f"Listo: quite a {name} de la sesion.\n\n{build_channel_reply(session)}"
            add_command_receipt(session, external_id, "remove_participant", reply)
        return repository.save(session)

    def configure_participant_requirements(
        self,
        session_id: str,
        name: str,
        *,
        required: bool | None = None,
        priority: int | None = None,
    ) -> Session:
        """Marca a un participante como requerido y/o le asigna peso de prioridad."""
        session = self.get(session_id)
        participant = next(
            (item for item in session.participants if participant_key(item.name) == participant_key(name)),
            None,
        )
        if not participant:
            raise HTTPException(status_code=404, detail="Participant not found")
        if required is not None:
            participant.required = bool(required)
        if priority is not None:
            participant.priority = max(0, int(priority))
        # Cambio de prioridades puede reordenar opciones y decisiones: recalcula
        # y, si el resultado cambia, limpia la decision anterior (como ya hace
        # refresh_schedule_state ante cualquier cambio de disponibilidad).
        refresh_schedule_state(session)
        session.messages.append(
            ChatMessage(
                role="assistant",
                content=f"Requerimientos de {participant.name} actualizados.",
            )
        )
        return repository.save(session)

    def calculate(self, session_id: str) -> Session:
        session = self.get(session_id)
        decision_is_confirmed = session.selected_option is not None and session.decision_summary is not None
        refresh_schedule_state(session, clear_decision=not decision_is_confirmed)
        if session.options:
            best = session.options[0]
            session.messages.append(
                ChatMessage(
                    role="assistant",
                    content=f"Calculo listo. Mejor opcion: {best.day} {best.start}-{best.end} con {best.coverage_percent}% de cobertura.",
                )
            )
        session.status = "confirmed" if decision_is_confirmed else "calculated"
        return repository.save(session)

    def confirm(
        self,
        session_id: str,
        option_id: str,
        confirmed_by: str = "admin",
        source: str = "panel",
        external_id: str | None = None,
        now: datetime | None = None,
        expected_proposal_revision: int | None = None,
        force: bool = False,
    ) -> Session:
        session = self.get(session_id)
        if expected_proposal_revision is not None and expected_proposal_revision != session.proposal_revision:
            raise HTTPException(
                status_code=409,
                detail="La propuesta cambio. Revisa las opciones actuales antes de confirmar.",
            )
        option = find_option(session.options, option_id)
        if not option:
            raise HTTPException(status_code=400, detail="Option not found")
        if session.selected_option and session.decision_summary and session.selected_option.id == option.id:
            # Un doble clic del panel o un reintento del gateway debe devolver
            # la decision ya cerrada, no crear otro registro de auditoria.
            if external_id:
                try:
                    document_text = build_calendar_ics(session)
                except Exception:
                    document_text = None
                add_command_receipt(
                    session,
                    external_id,
                    "confirm",
                    build_confirmed_channel_reply(session),
                    attachment_kind="calendar" if document_text else None,
                    document_text=document_text,
                )
                return repository.save(session)
            return session
        blockers = confirmation_blockers(session, ignore_missing_info=force)
        if blockers:
            raise HTTPException(status_code=409, detail=" ".join(blockers))

        snapshot = create_calendar_event_snapshot(session, option, now)
        session.selected_option = option
        session.selected_event_date = snapshot.start_at.split("T", 1)[0]
        session.selected_calendar_event = snapshot
        session.decision_summary = build_summary(session, option)
        session.decision_history.append(
            DecisionRecord(
                option=option.model_copy(deep=True),
                summary=session.decision_summary,
                confirmed_by=confirmed_by.strip() or "admin",
                source=source if source in {"panel", "whatsapp", "slack", "api"} else "api",
                event_date=session.selected_event_date,
                calendar_event=snapshot.model_copy(deep=True),
                external_id=external_id,
                created_at=snapshot.dtstamp,
            )
        )
        session.messages.append(ChatMessage(role="assistant", content=session.decision_summary))
        session.insights = build_insights(session)
        session.status = "confirmed"
        session.habitual_slot = compute_habitual_slot(session)
        if external_id:
            try:
                document_text = build_calendar_ics(session)
            except Exception:
                document_text = None
            add_command_receipt(
                session,
                external_id,
                "confirm",
                build_confirmed_channel_reply(session),
                attachment_kind="calendar" if document_text else None,
                document_text=document_text,
            )
        return repository.save(session)

    def attach_google_calendar_event(self, session_id: str, event_id: str, html_link: str) -> Session:
        """Adjunta el evento real de Google Calendar a la decision confirmada."""
        session = self.get(session_id)
        if session.selected_calendar_event:
            session.selected_calendar_event.google_event_id = event_id
            session.selected_calendar_event.google_event_html_link = html_link
        if session.decision_history:
            last = session.decision_history[-1]
            if last.calendar_event:
                last.calendar_event.google_event_id = event_id
                last.calendar_event.google_event_html_link = html_link
        return repository.save(session)

    def cancel_decision(self, session_id: str, external_id: str | None = None) -> Session:
        session = self.get(session_id)
        if not session.selected_option and not session.decision_summary:
            raise HTTPException(status_code=400, detail="Session has no confirmed decision")

        session.selected_option = None
        session.selected_event_date = None
        session.selected_calendar_event = None
        session.decision_summary = None
        session.status = "calculated" if session.options else "draft"
        # El historial de decisiones sigue existiendo para auditoria; el horario
        # habitual puede mantenerse o cambiar segun el nuevo modal del historial.
        session.habitual_slot = compute_habitual_slot(session)
        session.messages.append(ChatMessage(role="assistant", content="Decision cancelada. Puedes confirmar otra opcion."))
        if external_id:
            add_command_receipt(
                session,
                external_id,
                "cancel",
                build_cancelled_channel_reply(session),
            )
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
        listening_enabled: bool | None,
        trigger_word: str | None,
        reply_format: str | None = None,
        workday_start_hour: int | None = None,
        workday_end_hour: int | None = None,
        group_jid: str | None = None,
        group_name: str | None = None,
        group_participant_count: int | None = None,
        group_participant_ids: list[str] | None = None,
        coordinator_ids: list[str] | None = None,
        participant_roster: list[ParticipantRosterEntry] | None = None,
    ) -> Session:
        session = self.get(session_id)
        previous_window = (
            session.channel_config.workday_start_hour,
            session.channel_config.workday_end_hour,
        )
        previous_roster = (
            session.channel_config.group_participant_count,
            tuple(sorted(session.channel_config.group_participant_ids)),
        )
        next_start = workday_start_hour if workday_start_hour is not None else previous_window[0]
        next_end = workday_end_hour if workday_end_hour is not None else previous_window[1]
        if next_start >= next_end:
            raise HTTPException(status_code=400, detail="La hora de inicio debe ser anterior a la hora de fin.")
        if listening_enabled is not None:
            session.channel_config.listening_enabled = listening_enabled
        if trigger_word is not None:
            session.channel_config.trigger_word = trigger_word.strip()
        session.channel_config.workday_start_hour = next_start
        session.channel_config.workday_end_hour = next_end
        if reply_format is not None:
            session.channel_config.reply_format = reply_format
        if group_jid is not None:
            session.channel_config.group_jid = group_jid.strip() or None
        if group_name is not None:
            cleaned_group_name = group_name.strip()
            session.channel_config.group_name = cleaned_group_name or None
            known_prefixes = ("WhatsApp - ", "Slack - ")
            if cleaned_group_name and (
                session.title.startswith(known_prefixes) or session.title == "Coordinacion WhatsApp"
            ):
                prefix = next((p for p in known_prefixes if session.title.startswith(p)), known_prefixes[0])
                session.title = f"{prefix}{cleaned_group_name}"
        if group_participant_count is not None:
            session.channel_config.group_participant_count = group_participant_count
        if group_participant_ids is not None:
            # WhatsApp no garantiza el orden de los integrantes. Comparar y
            # persistir un conjunto estable evita invalidar propuestas solo
            # porque groupMetadata devolvio el mismo padron reordenado.
            session.channel_config.group_participant_ids = sorted(unique_ids(group_participant_ids))
        if coordinator_ids is not None:
            session.channel_config.coordinator_ids = sorted(unique_ids(coordinator_ids))
        roster_hydrated = False
        if participant_roster is not None or group_participant_ids is not None:
            roster_hydrated = hydrate_participant_roster(
                session,
                participant_roster,
                session.channel_config.group_participant_ids,
            )
        roster_changed = previous_roster != (
            session.channel_config.group_participant_count,
            tuple(sorted(session.channel_config.group_participant_ids)),
        )
        if previous_window != (next_start, next_end) or roster_changed or roster_hydrated:
            refresh_schedule_state(session)
        session.messages.append(
            ChatMessage(
                role="system",
                content=(
                    "Canal configurado: "
                    f"{'escucha activa' if session.channel_config.listening_enabled else 'escucha pausada'}, "
                    f"invocacion {session.channel_config.trigger_word}, "
                    f"horario {next_start:02d}:00-{next_end:02d}:00."
                ),
            )
        )
        return repository.save(session)

    def add_channel_message(
        self,
        session_id: str,
        sender: str,
        text: str,
        external_id: str | None = None,
        sender_id: str | None = None,
        sender_aliases: list[str] | None = None,
        mentioned_jids: list[str] | None = None,
    ) -> tuple[Session, bool]:
        session = self.get(session_id)
        if session.archived_at or not session.channel_config.listening_enabled:
            return session, False
        invoked = should_invoke(session, text)
        session.channel_messages.append(
            ChannelMessage(
                sender=sender.strip(),
                sender_id=clean_external_id(sender_id),
                sender_aliases=unique_ids([sender_id or "", *(sender_aliases or [])]),
                mentioned_jids=unique_ids(mentioned_jids or []),
                text=text.strip(),
                detected_invocation=invoked,
                external_id=external_id,
            )
        )
        return repository.save(session), invoked

    def add_channel_messages(
        self,
        session_id: str,
        messages: list[tuple],
    ) -> tuple[Session, bool]:
        session = self.get(session_id)
        if session.archived_at or not session.channel_config.listening_enabled:
            return session, False
        invoked = False

        for item in messages:
            sender, text = item[0], item[1]
            sender_id = item[2] if len(item) > 2 else None
            sender_aliases = item[3] if len(item) > 3 else []
            mentioned_jids = item[4] if len(item) > 4 else []
            message_invoked = should_invoke(session, text)
            invoked = invoked or message_invoked
            session.channel_messages.append(
                ChannelMessage(
                    sender=sender.strip(),
                    sender_id=clean_external_id(sender_id),
                    sender_aliases=unique_ids([sender_id or "", *(sender_aliases or [])]),
                    mentioned_jids=unique_ids(mentioned_jids or []),
                    text=text.strip(),
                    detected_invocation=message_invoked,
                )
            )

        return repository.save(session), invoked

    def add_agent_channel_reply(
        self,
        session_id: str,
        reply: str,
        *,
        reply_to_external_id: str | None = None,
        reply_format: str | None = None,
        attachment_kind: str | None = None,
    ) -> Session:
        session = self.get(session_id)
        session.last_agent_reply = reply
        session.channel_messages.append(
            ChannelMessage(
                sender="Agente",
                text=reply,
                kind="agent",
                reply_to_external_id=reply_to_external_id,
                reply_format=reply_format,
                attachment_kind=attachment_kind,
            )
        )
        session.messages.append(ChatMessage(role="assistant", content=reply, source="channel_simulator"))
        return repository.save(session)

    def reset_channel_context(self, session_id: str, *, external_id: str | None = None) -> Session:
        """Inicia otra coordinacion sin destruir el historial auditable del grupo.

        El estado y la respuesta se guardan juntos. Asi un retry del gateway no
        puede limpiar dos veces ni dejar mensajes nuevos detras de un reset a
        medio completar.
        """
        session = self.get(session_id)
        session.participants = []
        session.options = []
        session.availability_matrix = []
        session.missing_info = []
        session.insights = []
        session.last_processing = None
        # La revision debe ser monotona durante toda la vida de la sesion. Si
        # volviera a cero, un comando R1 de una ronda antigua podria confirmar
        # accidentalmente una propuesta R1 de la coordinacion nueva (ABA).
        session.proposal_revision += 1
        session.selected_option = None
        session.selected_event_date = None
        session.selected_calendar_event = None
        session.decision_summary = None
        session.archived_at = None
        session.status = "draft"
        # Reiniciar no usa datos anteriores: el horario habitual de la ronda
        # previa tampoco aplica a la coordinacion nueva (se recomputara al
        # confirmar la proxima decision). El indice marca donde empieza la
        # ronda actual dentro del historial de decisiones conservado.
        session.habitual_slot = None
        session.habitual_history_index = len(session.decision_history)

        reply = build_reset_channel_reply(session)
        reply_message = ChannelMessage(
            sender="Agente",
            text=reply,
            kind="agent",
            reply_to_external_id=external_id,
            reply_format="text",
        )
        session.channel_messages.append(reply_message)
        session.channel_context_message_id = reply_message.id
        session.last_agent_reply = reply
        session.messages.append(ChatMessage(role="assistant", content=reply, source="channel_simulator"))
        if external_id:
            add_command_receipt(session, external_id, "reset", reply)
        return repository.save(session)

    def channel_message_by_external_id(self, session: Session, external_id: str | None) -> ChannelMessage | None:
        if not external_id:
            return None
        return next(
            (
                message
                for message in session.channel_messages
                if message.kind == "human" and message.external_id == external_id
            ),
            None,
        )

    def agent_reply_for_external_id(self, session: Session, external_id: str) -> ChannelMessage | None:
        return next(
            (
                message
                for message in session.channel_messages
                if message.kind == "agent" and message.reply_to_external_id == external_id
            ),
            None,
        )

    def decision_by_external_id(self, session: Session, external_id: str):
        return next(
            (record for record in reversed(session.decision_history) if record.external_id == external_id),
            None,
        )

    def command_receipt_by_external_id(self, session: Session, external_id: str):
        return next(
            (
                receipt
                for receipt in reversed(session.channel_command_receipts)
                if receipt.external_id == external_id
            ),
            None,
        )

    def pending_channel_messages(self, session: Session) -> list[ChannelMessage]:
        context_index = next(
            (
                index
                for index, message in enumerate(session.channel_messages)
                if message.id == session.channel_context_message_id
            ),
            -1,
        )
        last_agent_index = context_index
        for index, message in enumerate(session.channel_messages[context_index + 1 :], start=context_index + 1):
            if message.kind == "agent":
                last_agent_index = index

        return [
            message
            for message in session.channel_messages[last_agent_index + 1 :]
            if message.kind == "human"
        ]


def find_option(options: list[TimeOption], option_id: str) -> TimeOption | None:
    return next((option for option in options if option.id == option_id), None)


def add_command_receipt(
    session: Session,
    external_id: str,
    command_name: str,
    reply: str,
    *,
    attachment_kind: str | None = None,
    document_text: str | None = None,
) -> None:
    session.channel_command_receipts.append(
        ChannelCommandReceipt(
            external_id=external_id,
            command_name=command_name,
            reply=reply,
            attachment_kind=attachment_kind,
            document_text=document_text,
        )
    )
    session.channel_command_receipts = session.channel_command_receipts[-1000:]


def refresh_schedule_state(session: Session, *, clear_decision: bool = True) -> None:
    session.participants = merge_duplicate_participants(session.participants)
    session.missing_info = find_missing_info(session)
    session.availability_matrix = build_availability_matrix(session)
    previous_options = session.options
    next_options = options_from_matrix(session.availability_matrix)
    previous_by_signature = {option_signature(option): option for option in previous_options}
    for option in next_options:
        previous = previous_by_signature.get(option_signature(option))
        if previous:
            option.id = previous.id
    if option_signatures(previous_options) != option_signatures(next_options):
        session.proposal_revision += 1
    session.options = next_options
    if clear_decision:
        clear_stale_decision(session)
    session.insights = build_insights(session)
    if clear_decision or session.status != "confirmed":
        session.status = "calculated" if session.options else "draft"


def option_signature(option: TimeOption) -> tuple:
    return (
        option.week_offset,
        option.day,
        option.start,
        option.end,
        tuple(option.available_participants),
        tuple(option.unavailable_participants),
        option.coverage_percent,
    )


def option_signatures(options: list[TimeOption]) -> tuple[tuple, ...]:
    return tuple(option_signature(option) for option in options)


def clear_stale_decision(session: Session) -> None:
    if not session.selected_option and not session.decision_summary:
        return

    session.selected_option = None
    session.selected_event_date = None
    session.selected_calendar_event = None
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
    group_memory=None,
    habitual_used: bool = False,
) -> ProcessingSummary:
    from app.services.group_memory import (
        GroupMemoryContext,
        format_group_memory_preview,
    )

    source_value = source or "unknown"
    participants_detected = len(extraction.participants)
    removals_detected = sum(1 for removal in extraction.removals if removal.slots)
    cached = "_cache" in source_value or bool(token_usage and token_usage.cached)
    fallback_used = "fallback" in source_value
    has_new_data = participants_detected > 0 or removals_detected > 0
    quality_flags = list(extraction.quality_flags)

    memory = group_memory if isinstance(group_memory, GroupMemoryContext) else None
    retrieval_used = bool(memory and memory.has_content())
    retrieval_source = memory.source if retrieval_used and memory else None
    retrieval_participant_count = len(memory.known_participants) if memory else 0
    retrieval_past_decision_count = len(memory.past_decisions) if memory else 0
    retrieval_preview = format_group_memory_preview(memory) if retrieval_used else ""

    if "ambiguous_mentioned_identity" in quality_flags:
        confidence = "low"
        label = "Baja"
        detail = "La mención no identifica de forma inequívoca a una sola persona; debe reenviarse por separado."
    elif "third_party_requires_mention" in quality_flags:
        confidence = "low"
        label = "Baja"
        detail = "Se descartó disponibilidad atribuida a otra persona porque no tenía una mención real."
    elif "no_new_availability" in source_value:
        confidence = "low"
        label = "Baja"
        detail = "No se detectaron datos nuevos de disponibilidad en los mensajes pendientes."
    elif "ambiguous_cross_day_range" in quality_flags:
        confidence = "low"
        label = "Baja"
        detail = "Se detecto un rango entre dias que no pudo normalizarse con seguridad; conviene aclararlo."
    elif "cross_day_range_normalized" in quality_flags:
        confidence = "medium"
        label = "Media"
        detail = "Se expandio un rango entre dias con reglas deterministicas y se validaron sus limites."
    elif "invalid_interval_discarded" in quality_flags:
        confidence = "low"
        label = "Baja"
        detail = "El extractor devolvio al menos un intervalo temporal incoherente y fue descartado."
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
        quality_flags=quality_flags,
        habitual_used=habitual_used,
        retrieval_used=retrieval_used,
        retrieval_source=retrieval_source,
        retrieval_participant_count=retrieval_participant_count,
        retrieval_past_decision_count=retrieval_past_decision_count,
        retrieval_preview=retrieval_preview,
    )


def participant_key(name: str) -> str:
    decomposed = unicodedata.normalize("NFD", name.strip().lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(without_accents.split())


def find_participant(
    participants: list[Participant],
    name: str,
    external_id: str | None = None,
    external_ids: list[str] | None = None,
) -> Participant | None:
    incoming_ids = set(unique_ids([external_id or "", *(external_ids or [])]))
    if incoming_ids:
        # Con evidencia técnica, el nombre visible deja de ser una clave. Dos
        # JIDs distintos llamados igual son dos personas distintas.
        return next(
            (
                participant
                for participant in participants
                if incoming_ids.intersection(participant_identity_ids(participant))
            ),
            None,
        )
    key = participant_key(name)
    return next((participant for participant in participants if participant_key(participant.name) == key), None)


def merge_duplicate_participants(participants: list[Participant]) -> list[Participant]:
    merged: list[Participant] = []
    for participant in participants:
        normalize_participant_identity(participant)
        ids = set(participant_identity_ids(participant))
        matching = [
            existing
            for existing in merged
            if (
                ids.intersection(participant_identity_ids(existing))
                if ids
                else not participant_identity_ids(existing)
                and participant_key(existing.name) == participant_key(participant.name)
            )
        ]
        if not matching:
            merged.append(participant)
            continue

        primary = matching[0]
        primary.availability = merge_slots(primary.availability, participant.availability)
        merge_participant_identity(primary, ids)
        if is_roster_placeholder_name(primary.name) and not is_roster_placeholder_name(participant.name):
            primary.name = participant.name
        # Si cualquiera de los dos ya es una persona real (no solo un
        # registro del padron), el resultado deja de ser "roster_only".
        primary.roster_only = primary.roster_only and participant.roster_only
        # Un registro puente PN/LID puede conectar duplicados históricos que
        # antes parecían identidades separadas.
        for duplicate in matching[1:]:
            primary.availability = merge_slots(primary.availability, duplicate.availability)
            merge_participant_identity(primary, participant_identity_ids(duplicate))
            primary.roster_only = primary.roster_only and duplicate.roster_only
            merged.remove(duplicate)

    return merged


def participant_identity_ids(participant: Participant) -> list[str]:
    return unique_ids([participant.external_id or "", *participant.external_ids])


def is_generated_contact_name(name: str) -> bool:
    return name.strip().lower() == "contacto mencionado" or bool(
        re.fullmatch(r"Contacto [A-F0-9]{4}", name.strip())
    )


def resolve_generated_participant_names(session: Session) -> bool:
    """Sustituye placeholders por pushNames ya vistos en el historial del grupo.

    Caso real: WhatsApp deja `@119048071307283` en el texto, el extractor crea
    "Contacto mencionado", pero el mismo LID ya hablo antes como "Gabriel".
    """

    display_names = build_channel_identity_display_names(session.channel_messages)
    if not display_names:
        return False

    generated_name_counts: dict[str, int] = {}
    for participant in session.participants:
        if is_generated_contact_name(participant.name):
            generated_name_counts[participant.name] = generated_name_counts.get(participant.name, 0) + 1

    changed = False
    renames: list[tuple[str, str]] = []
    for participant in session.participants:
        if not is_generated_contact_name(participant.name):
            continue
        resolved = resolve_display_name_for_identities(
            participant_identity_ids(participant),
            display_names,
            fallback=participant.name,
        )
        if resolved != participant.name and is_usable_channel_display_name(resolved):
            renames.append((participant.name, resolved))
            participant.name = resolved
            changed = True

    # Solo reescribe textos cuando el placeholder era unico. Si hubo dos
    # "Contacto mencionado" distintos, un replace ciego mezclaria personas;
    # en ese caso las opciones se reconstruyen desde la matriz mas abajo.
    for old_name, resolved in renames:
        if generated_name_counts.get(old_name, 0) != 1:
            continue
        session.missing_info = [
            item.replace(old_name, resolved) if isinstance(item, str) else item
            for item in session.missing_info
        ]
        if session.last_agent_reply:
            session.last_agent_reply = session.last_agent_reply.replace(old_name, resolved)
        if session.decision_summary and old_name in session.decision_summary:
            session.decision_summary = session.decision_summary.replace(old_name, resolved)
        for record in session.decision_history:
            if old_name in record.summary:
                record.summary = record.summary.replace(old_name, resolved)

    if renames or _options_have_generated_labels(session):
        changed = refresh_option_participant_labels(session) or changed
        if session.status == "confirmed" and session.selected_option:
            rebuilt = build_confirmed_channel_reply(session)
            if rebuilt != session.last_agent_reply:
                session.last_agent_reply = rebuilt
                changed = True
            if session.decision_summary and "Contacto mencionado" in session.decision_summary:
                available = ", ".join(session.selected_option.available_participants)
                session.decision_summary = (
                    f"WhatsApp - {session.title}: decision confirmada para "
                    f"{session.selected_option.day} de {session.selected_option.start} a "
                    f"{session.selected_option.end}. Asisten: {available}."
                )
                changed = True
    return changed


def _options_have_generated_labels(session: Session) -> bool:
    options = list(session.options)
    if session.selected_option:
        options.append(session.selected_option)
    for record in session.decision_history:
        options.append(record.option)
    for option in options:
        for name in [*option.available_participants, *option.unavailable_participants]:
            if is_generated_contact_name(name):
                return True
        if is_generated_contact_name(option.explanation) or "Contacto mencionado" in option.explanation:
            return True
    return "Contacto mencionado" in (session.last_agent_reply or "")


def refresh_option_participant_labels(session: Session) -> bool:
    """Recalcula Asisten/No calzan de opciones con los nombres actuales."""

    matrix = {
        (cell.week_offset, cell.day, cell.start, cell.end): cell
        for cell in build_availability_matrix(session)
    }
    changed = False

    def refresh(option: TimeOption) -> bool:
        cell = matrix.get((option.week_offset, option.day, option.start, option.end))
        if not cell:
            return False
        updated = False
        if option.available_participants != cell.available_participants:
            option.available_participants = list(cell.available_participants)
            updated = True
        if option.unavailable_participants != cell.unavailable_participants:
            option.unavailable_participants = list(cell.unavailable_participants)
            updated = True
        if updated:
            option.explanation = (
                f"Asisten {len(option.available_participants)} participante(s): "
                f"{', '.join(option.available_participants) or 'nadie'}."
            )
        return updated

    for option in session.options:
        changed = refresh(option) or changed
    if session.selected_option:
        changed = refresh(session.selected_option) or changed
    for record in session.decision_history:
        if refresh(record.option):
            available = ", ".join(record.option.available_participants)
            if available and "Asisten:" in record.summary:
                # Conserva prefijo del summary y solo actualiza la lista final.
                prefix = record.summary.split("Asisten:", 1)[0]
                record.summary = f"{prefix}Asisten: {available}."
            changed = True
    return changed


_GENERATED_CONTACT_LABEL_PATTERN = re.compile(r"\bContacto [A-F0-9]{4}\b", re.IGNORECASE)


def normalize_generated_contact_labels(session: Session) -> bool:
    """Migra etiquetas antiguas sin tocar las identidades técnicas.

    Versiones anteriores mostraban los primeros cuatro caracteres del hash de
    un JID como nombre (por ejemplo, ``Contacto 5000``). Ese valor nunca fue
    un nombre de WhatsApp y puede confundir al grupo. La migración recorre la
    sesión completa para que panel, respuestas y auditoría queden coherentes.
    """

    data = session.model_dump(mode="python")
    changed = False

    def replace(value):
        nonlocal changed
        if isinstance(value, str):
            replacement = _GENERATED_CONTACT_LABEL_PATTERN.sub("Contacto mencionado", value)
            changed = changed or replacement != value
            return replacement
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    migrated = Session.model_validate(replace(data))
    if changed:
        session.__dict__.update(migrated.__dict__)
    return changed


def canonical_external_id(values: list[str]) -> str | None:
    cleaned = unique_ids(values)
    if not cleaned:
        return None
    return next((value for value in cleaned if value.endswith("@s.whatsapp.net")), cleaned[0])


def normalize_participant_identity(participant: Participant) -> None:
    identities = participant_identity_ids(participant)
    participant.external_ids = identities
    participant.external_id = canonical_external_id(identities)


def merge_participant_identity(participant: Participant, values: list[str] | set[str]) -> None:
    identities = unique_ids([*participant_identity_ids(participant), *list(values)])
    participant.external_ids = identities
    participant.external_id = canonical_external_id(identities)


def roster_name_for_external_id(external_id: str) -> str:
    clean_id = clean_external_id(external_id) or ""
    local_part = clean_id.split("@", 1)[0].split(":", 1)[0]
    if clean_id.endswith("@s.whatsapp.net") and local_part.isdigit():
        return f"+{local_part}"
    suffix = local_part[-8:] or "nuevo"
    return f"Participante {suffix}"


def is_roster_placeholder_name(name: str) -> bool:
    clean_name = name.strip()
    return (
        is_generated_contact_name(clean_name)
        or clean_name.startswith("+")
        or clean_name.lower().startswith("participante ")
        or not is_usable_channel_display_name(clean_name)
    )


# Coincide con el min_length de WhatsAppUserId (schemas.py): un id mas corto
# nunca es un JID de WhatsApp ni un user id de Slack real, pero puede llegar
# asi desde un payload malformado. Se descarta en vez de romper la sesion.
_MIN_EXTERNAL_ID_LEN = 3


def hydrate_participant_roster(
    session: Session,
    participant_roster: list[ParticipantRosterEntry] | None,
    participant_ids: list[str] | None,
) -> bool:
    """Crea el padron del grupo antes de que llegue el primer mensaje.

    Los participantes creados aqui quedan marcados `roster_only=True`: se
    conoce su nombre por el padron del canal (Slack o WhatsApp), pero todavia
    no escribieron nada. Esto evita que `find_missing_info` los nombre uno por
    uno apenas se vincula el canal (ver decision_engine.find_missing_info)."""
    if participant_roster is None and participant_ids is None:
        return False

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in participant_roster or []:
        external_id = clean_external_id(entry.id)
        if not external_id or len(external_id) < _MIN_EXTERNAL_ID_LEN or external_id in seen:
            continue
        name = roster_display_name(entry.name, external_id)
        entries.append((external_id, name))
        seen.add(external_id)

    for external_id in participant_ids or []:
        clean_id = clean_external_id(external_id)
        if clean_id and len(clean_id) >= _MIN_EXTERNAL_ID_LEN and clean_id not in seen:
            entries.append((clean_id, roster_name_for_external_id(clean_id)))
            seen.add(clean_id)

    changed = False
    for external_id, name in entries:
        participant = find_participant(session.participants, name, external_id, [external_id])
        if participant:
            before = (participant.name, participant.external_id, tuple(participant.external_ids))
            merge_participant_identity(participant, [external_id])
            if is_roster_placeholder_name(participant.name) and not is_roster_placeholder_name(name):
                participant.name = name
            changed = changed or before != (
                participant.name,
                participant.external_id,
                tuple(participant.external_ids),
            )
            continue

        session.participants.append(
            Participant(name=name, external_id=external_id, external_ids=[external_id], roster_only=True)
        )
        changed = True

    normalized = merge_duplicate_participants(session.participants)
    if len(normalized) != len(session.participants):
        changed = True
    session.participants = normalized
    return changed


def roster_display_name(name: str | None, external_id: str) -> str:
    """Devuelve un nombre visible o un fallback tecnico legible.

    WhatsApp puede entregar nombres vacios, un punto u otras etiquetas que
    no representan a una persona. No se inventa un nombre: se conserva el
    nombre valido y, si no existe, se muestra el numero/JID normalizado.
    """
    clean_name = " ".join((name or "").strip().split())
    return clean_name if is_usable_channel_display_name(clean_name) else roster_name_for_external_id(external_id)


def normalize_roster_display_names(session: Session) -> bool:
    """Corrige etiquetas invalidas del padron sin tocar sus identidades."""
    changed = False
    for participant in session.participants:
        if is_generated_contact_name(participant.name) or is_usable_channel_display_name(participant.name):
            continue
        fallback = roster_name_for_external_id(participant.external_id or "")
        if fallback and participant.name != fallback:
            participant.name = fallback
            changed = True
    return changed


def merge_channel_identity_evidence(session: Session) -> bool:
    """Une aliases PN/LID solo cuando el gateway los entrego en un mensaje.

    Un nombre igual nunca basta para fusionar dos personas. En cambio, los
    aliases del mismo remitente son evidencia tecnica explicita y permiten
    reparar sesiones antiguas donde el padrón se hidrato antes de recibir el
    vinculo PN/LID.
    """
    groups: list[set[str]] = []
    for message in session.channel_messages:
        identities = set(channel_message_identity_ids(message))
        if not identities:
            continue
        matching = [group for group in groups if group.intersection(identities)]
        if not matching:
            groups.append(identities)
            continue
        merged = set(identities)
        for group in matching:
            merged.update(group)
            groups.remove(group)
        groups.append(merged)

    if not groups:
        return False

    changed = False
    for participant in session.participants:
        participant_ids = set(participant_identity_ids(participant))
        related = [group for group in groups if participant_ids.intersection(group)]
        if not related:
            continue
        identities = set().union(*related)
        before = tuple(participant_identity_ids(participant))
        merge_participant_identity(participant, identities)
        if before != tuple(participant_identity_ids(participant)):
            changed = True
    return changed


def channel_message_identity_ids(message: ChannelMessage) -> list[str]:
    return unique_ids([message.sender_id or "", *(message.sender_aliases or [])])


def should_invoke(session: Session, text: str) -> bool:
    if not session.channel_config.listening_enabled:
        return False

    trigger = session.channel_config.trigger_word.strip().lower()
    if not trigger:
        return False
    pattern = rf"(?<![\w@]){re.escape(trigger)}(?![\w])"
    return bool(re.search(pattern, text.lower()))


def clean_external_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def unique_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_external_id(value)
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def validate_slot_in_session_window(session: Session, slot: TimeSlot) -> None:
    start = time_to_minutes(slot.start)
    end = time_to_minutes(slot.end)
    window_start = session.channel_config.workday_start_hour * 60
    window_end = session.channel_config.workday_end_hour * 60
    if start < window_start or end > window_end:
        raise HTTPException(
            status_code=400,
            detail=(
                "El horario debe estar dentro de la ventana configurada "
                f"{session.channel_config.workday_start_hour:02d}:00-"
                f"{session.channel_config.workday_end_hour:02d}:00."
            ),
        )


def confirmation_blockers(session: Session, *, ignore_missing_info: bool = False) -> list[str]:
    blockers: list[str] = []
    if session.archived_at:
        blockers.append("La sesion esta archivada.")
    # ignore_missing_info: "confirmar igual" del coordinador. Solo salta este
    # blocker puntual (brecha de gente que nunca respondio); las protecciones
    # de datos ambiguos/dudosos y de requeridos siguen aplicando siempre.
    if session.missing_info and not ignore_missing_info:
        blockers.append("Falta informacion del grupo antes de confirmar.")
    processing = session.last_processing
    identity_flags = set(processing.quality_flags) if processing else set()
    if "ambiguous_mentioned_identity" in identity_flags:
        blockers.append("Hay una mención ambigua; reenvíenla mencionando a una sola persona por mensaje.")
    elif "third_party_requires_mention" in identity_flags:
        blockers.append("Hay horarios de terceros descartados; reenvíenlos usando una mención real, no como texto suelto.")
    # Una invocacion de comando sin disponibilidad nueva se registra con
    # confianza baja porque no hubo nada que interpretar. Eso no invalida una
    # propuesta ya calculada: bloquear aqui hacia que ``@coordina confirmar``
    # fallara aunque la ultima propuesta fuera completamente valida.
    elif (
        processing
        and processing.source != "channel_no_new_availability"
        and (processing.confidence == "low" or processing.fallback_used)
    ):
        blockers.append("La ultima interpretacion debe revisarse o repetirse con el LLM principal.")
    # Requeridos: si existen y ninguna opcion los cubre, no se debe confirmar
    # sin antes revisar (la propuesta vigente avisa quien queda fuera).
    has_required = any(participant.required for participant in session.participants)
    if has_required and session.options and not session.options[0].required_met:
        missing_names = ", ".join(session.options[0].required_missing) or "desconocidos"
        blockers.append(f"Ninguna opcion incluye a todos los requeridos (faltan: {missing_names}).")
    return blockers


def proposal_token(session: Session) -> str:
    return f"R{session.proposal_revision}"


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
    if _required_names(session):
        if option.required_met:
            summary += " Todos los requeridos asistirian."
        elif option.required_missing:
            summary += f" No asistirian requeridos: {', '.join(option.required_missing)}."
    return summary


def _channel_header(session: Session) -> str:
    name = session.channel_config.group_name
    return f"*Coordina - {name}*" if name else "*Coordina*"


def _required_names(session: Session) -> list[str]:
    return [participant.name for participant in session.participants if participant.required]


def _option_line(option: TimeOption, index: int, now: datetime | None = None, detailed: bool = False) -> str:
    event_date = format_short_date(option_event_date(option, now))
    suffix = f"{option.coverage_percent}% de cobertura" if detailed else f"{option.coverage_percent}%"
    return f"{index}. {option.day.capitalize()} {event_date}, {option.start}-{option.end} - {suffix}"


def build_channel_reply(session: Session, now: datetime | None = None) -> str:
    """Mensaje profesional para WhatsApp (usa *negrita* nativa de WhatsApp)."""
    header = _channel_header(session)
    trigger = session.channel_config.trigger_word
    identity_warning = build_identity_warning(session)
    required_names = _required_names(session)

    if not session.participants:
        warning = f"\n\n{identity_warning}" if identity_warning else ""
        return (
            f"{header}\n\n"
            "Aun no detecto disponibilidades claras."
            f"{warning}\n"
            "Escriban por ejemplo _yo puedo lunes en la tarde_ y vuelvan a invocarme con "
            f"{trigger}."
        )

    lines = [header]
    detected_count = len(session.participants)
    group_count = session.channel_config.group_participant_count
    if group_count is not None and group_count != detected_count:
        lines.append(
            f"_{group_count} integrante(s) actual(es) del grupo · "
            f"{detected_count} persona(s) con disponibilidad_"
        )
    elif group_count is not None:
        lines.append(f"_{group_count} integrante(s) del grupo con disponibilidad_")
    elif detected_count:
        lines.append(f"_{detected_count} persona(s) con disponibilidad_")
    lines.append("")
    if identity_warning:
        lines.extend([identity_warning, ""])
    if required_names:
        lines.append(f"_Requeridos: {', '.join(required_names)}_")
        lines.append("")

    if session.options:
        best = session.options[0]
        lines.append("*Mejor opcion*")
        lines.append(_option_line(best, 1, now, detailed=True))
        if best.available_participants:
            lines.append(f"Asisten: {', '.join(best.available_participants)}")
        if best.unavailable_participants:
            lines.append(f"No calzan: {', '.join(best.unavailable_participants)}")
        if required_names:
            if best.required_met:
                lines.append("Requeridos incluidos ✓")
            else:
                lines.append(f"⚠ No calza con requerido(s): {', '.join(best.required_missing)}")

        others = session.options[1:]
        if others:
            lines.append("")
            lines.append("*Otras opciones*")
            for index, option in enumerate(others, start=2):
                lines.append(_option_line(option, index, now))
        lines.append("")
        blockers = confirmation_blockers(session)
        if blockers:
            lines.append("*Aun no se puede confirmar con seguridad*")
            lines.extend(f"- {item}" for item in blockers)
        else:
            lines.append(
                f"Para cerrar: escribe *{trigger} confirmar* "
                f"(o *{trigger} confirmar 2* para otra opcion)."
            )
    else:
        lines.append("Aun no hay una opcion calculable con la informacion disponible.")

    if session.missing_info:
        lines.append("")
        lines.append("*Falta informacion*")
        lines.extend(f"- {item}" for item in session.missing_info)

    lines.append("")
    lines.append(f"Comandos: *{trigger} faltan*, *{trigger} exportar*, *{trigger} ayuda*.")

    return "\n".join(lines)


def build_identity_warning(session: Session) -> str:
    flags = set(session.last_processing.quality_flags) if session.last_processing else set()
    if "ambiguous_mentioned_identity" in flags:
        return (
            "*No incorporé una disponibilidad ambigua.* "
            "Mencionen a una sola persona por mensaje, por ejemplo: _@Gabo puede todo el día_."
        )
    if "third_party_requires_mention" in flags:
        return (
            "*No incorporé horarios escritos a nombre de otra persona sin una mención real.* "
            "Reenvíenlos mencionando de verdad a esa persona (no como texto), por ejemplo: _@Gabo puede todo el día_."
        )
    return ""


def build_confirmed_channel_reply(session: Session, now: datetime | None = None) -> str:
    header = _channel_header(session)
    if not session.selected_option:
        return f"{header}\n\nNo hay una opcion confirmada todavia."

    option = session.selected_option
    event_date = confirmed_event_date(session, now)
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
    if _required_names(session):
        if option.required_met:
            lines.append("Todos los requeridos asistirian.")
        elif option.required_missing:
            lines.append(f"⚠ No asistirian requeridos: {', '.join(option.required_missing)}.")

    real_event_link = (
        session.selected_calendar_event.google_event_html_link
        if session.selected_calendar_event
        else None
    )
    google_url = real_event_link or build_google_calendar_url(session, now)
    if google_url:
        lines.append("")
        if real_event_link:
            lines.append("Evento creado en Google Calendar:")
        else:
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
            "- Para informar por otra persona, menciónenla de verdad (no como texto): _@Gabo puede todo el día_",
            "- Mencionen solo a una persona por mensaje.",
            "",
            "*Comandos*",
            f"- *{trigger}*  ·  propone los mejores horarios",
            f"- *{trigger} confirmar* (o _confirmar 2_)  ·  cierra la decision y envia el evento al calendario",
            f"- *{trigger} confirmar igual*  ·  confirma aunque falte gente por identificar (no salta otras protecciones)",
            f"- *{trigger} cancela*  ·  deshace la decision confirmada",
            f"- *{trigger} resumen*  ·  estado actual y opciones",
            f"- *{trigger} faltan*  ·  quienes no han dado su horario",
            f"- *{trigger} exportar*  ·  reporte de la sesion",
            f"- *{trigger} quita a @contacto*  ·  elimina exactamente a la persona mencionada",
            f"- *{trigger} reinicia historial*  ·  comienza otra coordinacion sin usar datos anteriores",
            f"- *{trigger} ayuda*  ·  este mensaje",
            "",
            "*Participantes requeridos* (solo coordinadores del grupo)",
            f"- *{trigger} requerido a @contacto*  ·  esa persona debe estar si o si; las opciones que no la incluyan no se recomiendan",
            f"- *{trigger} normal a @contacto*  ·  esa persona deja de ser requerida",
            "",
            "*Formato de respuesta*",
            f"- *{trigger} con imagen*  ·  incluye el calendario grafico",
            f"- *{trigger} solo texto*  ·  sin imagen",
        ]
    )


def build_reset_channel_reply(session: Session) -> str:
    return "\n".join(
        [
            _channel_header(session),
            "",
            "*Nueva coordinacion iniciada*",
            "No usare participantes, disponibilidades, opciones ni decisiones anteriores.",
            "El historial previo queda guardado en el panel solo para auditoria.",
            "",
            "Escriban sus nuevos horarios y luego invoquen de nuevo con "
            f"*{session.channel_config.trigger_word}*.",
        ]
    )


def build_no_habitual_reply(session: Session) -> str:
    return "\n".join(
        [
            _channel_header(session),
            "",
            "Aun no tengo registrado un *horario habitual* para este grupo.",
            "En cuanto confirmen una opcion quedara fijado y podran escribir "
            "simplemente _a la hora de siempre_.",
            "",
            "Por ahora escriban el horario concreto, por ejemplo: "
            "_yo puedo martes de 10 a 11_.",
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
        flags = []
        if participant.required:
            flags.append("requerido")
        if participant.priority:
            flags.append(f"prioridad {participant.priority}")
        suffix = f" ({', '.join(flags)})" if flags else ""
        if participant.availability:
            slots = ", ".join(f"{slot.day} {slot.start}-{slot.end}" for slot in participant.availability)
            lines.append(f"{participant.name}{suffix}: {slots}")
        else:
            lines.append(f"{participant.name}{suffix}: sin disponibilidad")
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
            if option.required_missing:
                lines.append(f"   Faltan requeridos: {', '.join(option.required_missing)}")
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
            "requerido",
            "prioridad",
        ]
    )

    group_name = session.channel_config.group_name or session.title
    for participant in sorted(session.participants, key=lambda item: participant_key(item.name)):
        if not participant.availability:
            writer.writerow(
                [
                    "disponibilidad",
                    group_name,
                    session.status,
                    participant.name,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "si" if participant.required else "no",
                    participant.priority or "",
                ]
            )
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
                    "si" if participant.required else "no",
                    participant.priority or "",
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
                "si" if option.required_met else "no",
                option.weighted_score,
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
                "si" if option.required_met else "no",
                option.weighted_score,
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
    message = last_invoking_message(session)
    return message.text if message else ""


def last_invoking_sender(session: Session) -> str:
    message = last_invoking_message(session)
    return message.sender if message else "admin"


def last_invoking_message(session: Session) -> ChannelMessage | None:
    return next(
        (
            message
            for message in reversed(session.channel_messages)
            if message.kind == "human" and message.detected_invocation
        ),
        None,
    )


def classify_channel_command(session: Session) -> dict | None:
    invoking_message = last_invoking_message(session)
    if not invoking_message:
        return None
    raw = invoking_message.text

    text = _strip_accents(raw)
    trigger = _strip_accents(session.channel_config.trigger_word)
    command_text = text.replace(trigger, "", 1).strip() if trigger else text

    if not command_text:
        return None

    reset_with_target = re.search(
        r"\b(?:reinicia(?:r)?|resetea(?:r)?|restablece(?:r)?|borra(?:r)?|limpia(?:r)?|reset)\b"
        r".*\b(?:historial|contexto|coordinacion|sesion)\b",
        command_text,
    )
    starts_over = re.search(r"\b(?:nueva coordinacion|empezar de nuevo|partir de cero)\b", command_text)
    if reset_with_target or starts_over:
        return {"name": "reset"}

    if re.search(r"\b(ayuda|help|comandos|instrucciones)\b", command_text):
        return {"name": "help"}

    number_token = re.search(r"(?<!\w)([+-]?\d+(?:[.,]\d+)?(?:ra|ro|ta|to)?)(?!\w)", command_text)
    if re.search(r"\b(confirmar|confirma|confirmo)\b", command_text) or (
        re.search(r"\b(cerrar|cierra)\b", command_text) and number_token
    ):
        revision_match = re.search(r"\br\s*(\d+)\b", command_text)
        revision = int(revision_match.group(1)) if revision_match else None
        # "confirmar igual/de todos modos/aunque falte(n)": el coordinador
        # decide avanzar aunque no todos hayan respondido. No salta las demas
        # protecciones (menciones ambiguas, requeridos sin cubrir, etc.).
        force = bool(re.search(r"\b(igual|de todos modos|aunque falte\w*|forzar|forzado)\b", command_text))
        if not number_token:
            return {
                "name": "confirm",
                "option_index": 1,
                "option_label": "1",
                "proposal_revision": revision,
                "force": force,
            }

        option_label = number_token.group(1)
        integer_match = re.fullmatch(r"([+-]?\d+)(?:ra|ro|ta|to)?", option_label)
        option_index = int(integer_match.group(1)) if integer_match else 0
        return {
            "name": "confirm",
            "option_index": option_index,
            "option_label": option_label,
            "proposal_revision": revision,
            "force": force,
        }

    if re.search(r"\b(cancelar|cancela|cancelen|anular|anula|deshacer|deshaz)\b", command_text):
        return {"name": "cancel"}

    if re.search(r"\b(faltan|pendientes|quien falta|quienes faltan|datos faltantes)\b", command_text):
        return {"name": "missing"}

    if re.search(r"\b(exportar|informe|reporte|respaldo)\b", command_text):
        return {"name": "export"}

    if re.search(r"\b(resumen|estado|status|opciones actuales)\b", command_text):
        return {"name": "summary"}

    requirement_match = _classify_requirement_command(command_text)
    if requirement_match:
        return requirement_match

    remove_match = re.search(
        r"\b(?:quita|quitar|elimina|eliminar|saca|sacar|olvida|olvidar)\s+"
        r"(?:a\s+)?(?P<mention>@?)(?P<target>[a-z0-9_-]+(?:\s+[a-z0-9_-]+){0,7})\s*$",
        command_text,
    )
    if remove_match:
        candidate = " ".join(remove_match.group("target").split())
        mentioned_ids = unique_ids(invoking_message.mentioned_jids)
        if remove_match.group("mention") != "@" or len(mentioned_ids) != 1:
            return {
                "name": "remove_participant",
                "participant_name": candidate,
                "requires_mention": True,
            }
        target_external_id = mentioned_ids[0]
        by_identity = find_participant(
            session.participants,
            candidate,
            target_external_id,
            [target_external_id],
        )
        known = next(
            (
                participant.name
                for participant in session.participants
                if participant_key(participant.name) == participant_key(candidate)
            ),
            candidate,
        )
        return {
            "name": "remove_participant",
            "participant_name": by_identity.name if by_identity else known,
            "target_external_id": target_external_id,
        }

    return None


def _classify_requirement_command(command_text: str) -> dict | None:
    """Comandos de requerimientos/prioridad (solo administradores del grupo).

    Se evalúan ANTES que remove_participant para que "quitar requerido @Ana"
    no se interprete como eliminar al participante "requerido @Ana".
    """
    priority = re.search(
        r"\b(?:prioridad|prioricemos|prioriza)\s+(?:a\s+)?"
        r"(?P<mention>@?)(?P<target>[a-z0-9_-]+(?:\s+[a-z0-9_-]+){0,7})"
        r"\s+(?P<value>\d{1,3})\s*$",
        command_text,
    )
    if priority:
        return {
            "name": "set_priority",
            "participant_name": " ".join(priority.group("target").split()),
            "mention": priority.group("mention"),
            "priority": int(priority.group("value")),
        }

    required = re.search(
        r"\b(?:requerido|requerida|imprescindible|obligatorio|obligatoria|debe estar)"
        r"\s+(?:a\s+)?(?P<mention>@?)(?P<target>[a-z0-9_-]+(?:\s+[a-z0-9_-]+){0,7})\s*$",
        command_text,
    )
    if required:
        return {
            "name": "set_required",
            "participant_name": " ".join(required.group("target").split()),
            "mention": required.group("mention"),
        }

    normal = re.search(
        r"\b(?:normal|sin prioridad|quitar requerido|quita requerido|ya no es requerido)"
        r"\s+(?:a\s+)?(?P<mention>@?)(?P<target>[a-z0-9_-]+(?:\s+[a-z0-9_-]+){0,7})\s*$",
        command_text,
    )
    if normal:
        return {
            "name": "clear_requirements",
            "participant_name": " ".join(normal.group("target").split()),
            "mention": normal.group("mention"),
        }

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
