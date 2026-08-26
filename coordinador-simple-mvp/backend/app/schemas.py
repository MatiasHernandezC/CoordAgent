from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, SecretStr, model_validator

Day = Literal["lunes", "martes", "miercoles", "jueves", "viernes"]
SessionStatus = Literal["draft", "calculated", "confirmed"]
ProcessingConfidence = Literal["high", "medium", "low"]
# Formato de la respuesta que el agente envia al grupo:
#   text  = solo el mensaje de texto profesional
#   image = solo la imagen con el calendario de disponibilidad
#   both  = mensaje + imagen (default)
ReplyFormat = Literal["text", "image", "both"]
WhatsAppUserId = Annotated[str, Field(min_length=3, max_length=120)]

class TokenUsage(BaseModel):
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    time_to_first_token_ms: int | None = None
    cached: bool = False

class TimeSlot(BaseModel):
    day: Day
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    # Semana relativa a la que pertenece el bloque: 0 = proxima ocurrencia del dia
    # (esta semana), 1 = "la otra/proxima semana", 2 = "en dos semanas", etc.
    # Default 0 mantiene retrocompatibilidad con datos y llamadas existentes.
    week_offset: int = 0


class ParticipantRosterEntry(BaseModel):
    id: WhatsAppUserId
    name: str = Field(min_length=1, max_length=120)


class Participant(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    # JID estable de WhatsApp cuando la disponibilidad fue escrita por la
    # propia persona. El nombre visible puede cambiar o repetirse.
    external_id: str | None = None
    # Un mismo usuario puede alternar entre PN y LID. Se conservan todos los
    # aliases que WhatsApp haya enlazado de forma explícita; nunca se infieren
    # por similitud de nombres.
    external_ids: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    availability: list[TimeSlot] = Field(default_factory=list)
    # Miembro que SI o SI debe estar en la reunion: las opciones que no lo
    # incluyan dejan de recomendarse (el motor las filtra).
    required: bool = False
    # Peso de prioridad para el score de opciones (ej. el jefe). 0 = peso 1.
    priority: int = 0
    # Se creo desde el padron del canal (Slack/WhatsApp) antes de que la
    # persona escribiera algo. Mientras siga en True y sin disponibilidad, no
    # se le nombra en "falta disponibilidad" (ver find_missing_info): recien
    # se vinculo el canal, nombrar a todo el padron uno por uno seria ruido.
    roster_only: bool = False


class AvailabilityRemoval(BaseModel):
    participant_name: str
    external_id: str | None = None
    external_ids: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    slots: list[TimeSlot] = Field(default_factory=list)


class TimeOption(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    day: Day
    start: str
    end: str
    week_offset: int = 0
    available_participants: list[str]
    unavailable_participants: list[str] = Field(default_factory=list)
    score: int
    coverage_percent: int = 0
    explanation: str = ""
    # Prioridades de la sesion ya aplicadas: score ponderado por pesos,
    # y si la opcion cubre a todos los participantes requeridos.
    weighted_score: int = 0
    required_met: bool = False
    required_missing: list[str] = Field(default_factory=list)


class ProcessingSummary(BaseModel):
    source: str = "unknown"
    confidence: ProcessingConfidence = "medium"
    confidence_label: str = "Media"
    detail: str = ""
    participants_detected: int = 0
    removals_detected: int = 0
    cached: bool = False
    fallback_used: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    # La frase "a la hora de siempre" se resolvio con el horario habitual de la sesion.
    habitual_used: bool = False
    # Structured RAG metadata (defaults keep old sessions / clients compatible).
    retrieval_used: bool = False
    retrieval_source: str | None = None
    retrieval_participant_count: int = 0
    retrieval_past_decision_count: int = 0
    retrieval_preview: str = ""
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CalendarEventSnapshot(BaseModel):
    uid: str
    start_at: str
    end_at: str
    timezone: str
    dtstamp: str
    # Solo si el evento se creo realmente en Google Calendar (cuenta conectada).
    google_event_id: str | None = None
    google_event_html_link: str | None = None


class DecisionRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    option: TimeOption
    summary: str
    confirmed_by: str = "admin"
    source: Literal["panel", "whatsapp", "slack", "api"] = "panel"
    event_date: str | None = None
    calendar_event: CalendarEventSnapshot | None = None
    external_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AvailabilityCell(BaseModel):
    day: Day
    start: str
    end: str
    week_offset: int = 0
    available_participants: list[str] = Field(default_factory=list)
    unavailable_participants: list[str] = Field(default_factory=list)
    score: int = 0
    coverage_percent: int = 0
    weighted_score: int = 0
    required_met: bool = False
    required_missing: list[str] = Field(default_factory=list)


class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["user", "assistant", "system"]
    content: str
    source: str | None = None
    token_usage: TokenUsage | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ChannelConfig(BaseModel):
    listening_enabled: bool = True
    trigger_word: str = "@coordina"
    reply_format: ReplyFormat = "both"
    workday_start_hour: int = Field(default=9, ge=0, le=22)
    workday_end_hour: int = Field(default=18, ge=1, le=23)
    group_jid: str | None = None
    group_name: str | None = None
    group_participant_count: int | None = None
    group_participant_ids: list[str] = Field(default_factory=list)
    coordinator_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_workday_window(self) -> "ChannelConfig":
        if self.workday_start_hour >= self.workday_end_hour:
            raise ValueError("La hora de inicio debe ser anterior a la hora de fin.")
        return self


class ChannelMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    sender: str
    sender_id: str | None = None
    sender_aliases: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    # Solo contiene menciones verificadas por contextInfo.mentionedJid. Un
    # texto que simplemente incluya "@Nombre" no llena esta lista.
    mentioned_jids: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    text: str
    kind: Literal["human", "agent"] = "human"
    detected_invocation: bool = False
    # ID estable del mensaje de WhatsApp. Permite reintentos sin insertar ni
    # procesar dos veces la misma entrada.
    external_id: str | None = None
    reply_to_external_id: str | None = None
    reply_format: ReplyFormat | None = None
    attachment_kind: Literal["calendar"] | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ChannelCommandReceipt(BaseModel):
    external_id: str
    command_name: str
    reply: str
    reply_format: ReplyFormat = "text"
    attachment_kind: Literal["calendar"] | None = None
    document_text: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # Version del esquema persistido: permite detectar y migrar registros antiguos.
    # Al tener default, las sesiones ya guardadas sin este campo siguen validando.
    schema_version: int = 1
    title: str
    # La cuenta propietaria administra el grupo; el administrador de plataforma
    # mantiene acceso global. Las sesiones históricas pueden no tener dueño.
    owner_username: str | None = None
    # Código temporal que el propietario escribe dentro del grupo de WhatsApp.
    # Se elimina inmediatamente después de una vinculación correcta.
    link_code: str | None = None
    linked_at: str | None = None
    # Una sesion puede estar asignada a varias cuentas. Los administradores
    # conservan acceso global y los registros antiguos parten sin asignacion.
    assigned_usernames: list[str] = Field(default_factory=list)
    # Se guarda el ID estable del participante, no su nombre visible (puede
    # cambiar o repetirse dentro del grupo).
    chief_participant_id: str | None = None
    participants: list[Participant] = Field(default_factory=list)
    options: list[TimeOption] = Field(default_factory=list)
    availability_matrix: list[AvailabilityCell] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    channel_config: ChannelConfig = Field(default_factory=ChannelConfig)
    channel_messages: list[ChannelMessage] = Field(default_factory=list)
    # Respuesta del agente que marca el inicio de la coordinacion vigente. El
    # historial anterior se conserva para auditoria, pero deja de ser contexto
    # activo para el LLM y para el resumen principal del panel.
    channel_context_message_id: str | None = None
    channel_command_receipts: list[ChannelCommandReceipt] = Field(default_factory=list)
    last_processing: ProcessingSummary | None = None
    last_agent_reply: str | None = None
    # Cambia solo cuando cambia el contenido de las alternativas. WhatsApp usa
    # esta revision para impedir que "confirmar 1" cierre otra propuesta.
    proposal_revision: int = 0
    selected_option: TimeOption | None = None
    # Fecha calendario fijada al confirmar. Evita que un .ics descargado mas
    # tarde se mueva silenciosamente a la semana siguiente.
    selected_event_date: str | None = None
    selected_calendar_event: CalendarEventSnapshot | None = None
    decision_summary: str | None = None
    decision_history: list[DecisionRecord] = Field(default_factory=list)
    archived_at: str | None = None
    status: SessionStatus = "draft"
    # Horario "de siempre" del grupo calculado desde decision_history.
    # Cuando un mensaje dice "a la hora de siempre" se resuelve a este slot.
    habitual_slot: TimeSlot | None = None
    # Indice en decision_history donde comienza la ronda actual (para que el
    # horario habitual tras "reinicia historial" no arrastre decisiones de
    # rondas anteriores, que solo se conservan para auditoria).
    habitual_history_index: int | None = None


class CreateSessionRequest(BaseModel):
    title: str = "Reunion grupal"


class CreatePanelUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=10, max_length=128)


class RegisterPanelUserRequest(CreatePanelUserRequest):
    pass


class UpdatePanelUserRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=80)
    active: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.display_name is None and self.active is None:
            raise ValueError("Debes indicar un cambio para el usuario.")
        return self


class ResetPanelUserPasswordRequest(BaseModel):
    password: str = Field(min_length=10, max_length=128)


class AssignSessionUsersRequest(BaseModel):
    usernames: list[str] = Field(default_factory=list, max_length=100)


class TransferSessionOwnerRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)


class AssignChiefRequest(BaseModel):
    participant_id: str = Field(min_length=1, max_length=80)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    session: Session
    llm_source: str
    elapsed_ms: int = 0
    token_usage: TokenUsage | None = None


class ConfirmRequest(BaseModel):
    option_id: str
    expected_proposal_revision: int | None = Field(default=None, ge=0)


class ImpliedAvailability(BaseModel):
    """Disponibilidad inferida pragmaticamente (ej. "no puedo despues de las 16"
    implica poder antes). Solo se aplica si la persona no tiene nada ese dia."""

    participant_name: str
    external_id: str | None = None
    external_ids: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    slots: list[TimeSlot] = Field(default_factory=list)


class ExtractedAvailability(BaseModel):
    participants: list[Participant] = Field(default_factory=list)
    removals: list[AvailabilityRemoval] = Field(default_factory=list)
    implied: list[ImpliedAvailability] = Field(default_factory=list)
    # Personas cuya nueva declaracion reemplaza, en vez de ampliar, la agenda
    # anterior ("en realidad", "corrijo", "ahora solo").
    replacements: list[str] = Field(default_factory=list)
    # Diagnosticos internos del pipeline. Permiten no declarar confianza alta si
    # el proveedor emitio intervalos incoherentes o hubo que corregir un rango.
    quality_flags: list[str] = Field(default_factory=list)


class ScheduleEntry(BaseModel):
    """Restriccion semantica que emite el LLM: el modelo razona el lenguaje y
    esta IR tipada se compila de forma determinista a ExtractedAvailability."""

    person: str = ""
    kind: Literal["available", "unavailable", "only", "replace"] = "available"
    days: list[str] = Field(default_factory=list)
    start: str | None = None
    end: str | None = None
    # 0 = esta semana / proxima ocurrencia; 1 = "la otra/proxima semana"; etc.
    week_offset: int = 0


class ScheduleInterpretation(BaseModel):
    entries: list[ScheduleEntry] = Field(default_factory=list)


class AddParticipantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ConfigureParticipantRequirementsRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # Solo se configura si la persona es obligatoria para la coordinacion.
    required: bool | None = None


class AddAvailabilityRequest(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    day: Day
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")

    @model_validator(mode="after")
    def check_start_before_end(self) -> "AddAvailabilityRequest":
        # Como el formato es HH:MM con cero a la izquierda, comparar como texto ordena
        # bien dentro del dia. Evita el no-op silencioso cuando inicio >= fin.
        if self.start >= self.end:
            raise ValueError("La hora de inicio debe ser anterior a la hora de fin.")
        return self


class ChannelConfigRequest(BaseModel):
    # Todos son parciales: la sincronizacion de metadata del gateway no debe
    # reactivar una pausa ni restablecer preferencias del panel.
    listening_enabled: bool | None = None
    trigger_word: str | None = Field(default=None, min_length=2, max_length=40)
    reply_format: ReplyFormat | None = None
    workday_start_hour: int | None = Field(default=None, ge=0, le=22)
    workday_end_hour: int | None = Field(default=None, ge=1, le=23)
    group_jid: str | None = Field(default=None, max_length=120)
    group_name: str | None = Field(default=None, max_length=120)
    group_participant_count: int | None = Field(default=None, ge=0, le=2048)
    group_participant_ids: list[str] | None = None
    coordinator_ids: list[str] | None = None
    participant_roster: list[ParticipantRosterEntry] | None = None

    @model_validator(mode="after")
    def check_complete_workday_window(self) -> "ChannelConfigRequest":
        if (
            self.workday_start_hour is not None
            and self.workday_end_hour is not None
            and self.workday_start_hour >= self.workday_end_hour
        ):
            raise ValueError("La hora de inicio debe ser anterior a la hora de fin.")
        return self


class ResolveChannelGroupRequest(BaseModel):
    group_jid: str = Field(min_length=6, max_length=120)
    group_name: str = Field(default="grupo de WhatsApp", min_length=1, max_length=120)
    group_participant_count: int | None = Field(default=None, ge=0, le=2048)
    group_participant_ids: list[str] = Field(default_factory=list)
    coordinator_ids: list[str] = Field(default_factory=list)
    participant_roster: list[ParticipantRosterEntry] = Field(default_factory=list)
    trigger_word: str = Field(default="@coordina", min_length=2, max_length=40)
    create_if_missing: bool = True


class LinkChannelGroupRequest(ResolveChannelGroupRequest):
    link_code: str = Field(min_length=8, max_length=32)


class ChannelMessageRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=80)
    sender_id: str | None = Field(default=None, min_length=3, max_length=120)
    sender_aliases: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    mentioned_jids: list[WhatsAppUserId] = Field(default_factory=list, max_length=16)
    text: str = Field(min_length=1, max_length=500)
    message_id: str | None = Field(default=None, min_length=1, max_length=240)


class ChannelBatchRequest(BaseModel):
    messages: list[ChannelMessageRequest] = Field(min_length=1, max_length=20)


class ChannelMessageResponse(BaseModel):
    session: Session
    invoked: bool
    llm_source: str | None = None
    agent_reply: str | None = None
    agent_reply_image: str | None = None  # PNG del calendario en base64 (sin prefijo data URI)
    agent_reply_caption: str | None = None  # texto que acompana la imagen en WhatsApp
    agent_reply_format: ReplyFormat | None = None
    # Documento adjunto (ej. evento .ics al confirmar): contenido en base64 + metadata.
    agent_reply_document: str | None = None
    agent_reply_document_name: str | None = None
    agent_reply_document_mimetype: str | None = None
    elapsed_ms: int = 0
    token_usage: TokenUsage | None = None
    duplicate: bool = False


class RuntimeInfo(BaseModel):
    provider: str
    provider_label: str
    model: str
    cache_enabled: bool
    fallback_enabled: bool
    gemini_configured: bool
    gemini_key_count: int = 0
    gemini_available_key_count: int = 0
    gemini_active_key_name: str | None = None
    gemini_key_management_enabled: bool = False
    warnings: list[str] = Field(default_factory=list)


LlmKeyStatus = Literal[
    "unverified",
    "ready",
    "cooldown",
    "invalid",
    "incompatible",
    "disabled",
]


class LlmKeyPublic(BaseModel):
    id: str
    name: str
    priority: int
    enabled: bool
    status: LlmKeyStatus
    source: Literal["panel", "legacy_env"] = "panel"
    managed: bool = True
    success_count: int = 0
    failure_count: int = 0
    request_count: int = 0
    error_count: int = 0
    quota_exhaustion_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    ttft_sample_count: int = 0
    last_ttft_ms: int | None = None
    average_ttft_ms: int | None = None
    best_ttft_ms: int | None = None
    worst_ttft_ms: int | None = None
    last_generation_at: str | None = None
    last_used_at: str | None = None
    cooldown_until: str | None = None
    last_error_code: int | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class LlmKeyAuditPublic(BaseModel):
    id: str
    credential_id: str | None = None
    credential_name: str
    action: str
    actor: str
    result: str
    created_at: str


class LlmKeyListResponse(BaseModel):
    keys: list[LlmKeyPublic]
    audit: list[LlmKeyAuditPublic] = Field(default_factory=list)
    active_key_id: str | None = None
    available_count: int = 0
    management_enabled: bool = False


class CreateLlmKeyRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    secret: SecretStr


class UpdateLlmKeyRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    priority: int | None = Field(default=None, ge=0, le=10000)
    enabled: bool | None = None


class DeleteLlmKeyRequest(BaseModel):
    confirm_name: str = Field(min_length=2, max_length=80)


class ExportResponse(BaseModel):
    filename: str
    text: str


class GoogleCalendarStatus(BaseModel):
    configured: bool
    connected: bool
    account_email: str | None = None
    connected_at: str | None = None


class GoogleCalendarAuthUrl(BaseModel):
    auth_url: str
