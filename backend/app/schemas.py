from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

Day = Literal["lunes", "martes", "miercoles", "jueves", "viernes"]
SessionStatus = Literal["draft", "calculated", "confirmed"]
ProcessingConfidence = Literal["high", "medium", "low"]
# Formato de la respuesta que el agente envia al grupo:
#   text  = solo el mensaje de texto profesional
#   image = solo la imagen con el calendario de disponibilidad
#   both  = mensaje + imagen (default)
ReplyFormat = Literal["text", "image", "both"]

class TokenUsage(BaseModel):
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cached: bool = False

class TimeSlot(BaseModel):
    day: Day
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    # Semana relativa a la que pertenece el bloque: 0 = proxima ocurrencia del dia
    # (esta semana), 1 = "la otra/proxima semana", 2 = "en dos semanas", etc.
    # Default 0 mantiene retrocompatibilidad con datos y llamadas existentes.
    week_offset: int = 0


class Participant(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    availability: list[TimeSlot] = Field(default_factory=list)


class AvailabilityRemoval(BaseModel):
    participant_name: str
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


class ProcessingSummary(BaseModel):
    source: str = "unknown"
    confidence: ProcessingConfidence = "medium"
    confidence_label: str = "Media"
    detail: str = ""
    participants_detected: int = 0
    removals_detected: int = 0
    cached: bool = False
    fallback_used: bool = False
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DecisionRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    option: TimeOption
    summary: str
    confirmed_by: str = "admin"
    source: Literal["panel", "whatsapp", "api"] = "panel"
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
    group_jid: str | None = None
    group_name: str | None = None
    group_participant_count: int | None = None


class ChannelMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    sender: str
    text: str
    kind: Literal["human", "agent"] = "human"
    detected_invocation: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # Version del esquema persistido: permite detectar y migrar registros antiguos.
    # Al tener default, las sesiones ya guardadas sin este campo siguen validando.
    schema_version: int = 1
    title: str
    participants: list[Participant] = Field(default_factory=list)
    options: list[TimeOption] = Field(default_factory=list)
    availability_matrix: list[AvailabilityCell] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    channel_config: ChannelConfig = Field(default_factory=ChannelConfig)
    channel_messages: list[ChannelMessage] = Field(default_factory=list)
    last_processing: ProcessingSummary | None = None
    last_agent_reply: str | None = None
    selected_option: TimeOption | None = None
    decision_summary: str | None = None
    decision_history: list[DecisionRecord] = Field(default_factory=list)
    archived_at: str | None = None
    status: SessionStatus = "draft"


class CreateSessionRequest(BaseModel):
    title: str = "Reunion grupal"


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    session: Session
    llm_source: str
    elapsed_ms: int = 0
    token_usage: TokenUsage | None = None


class ConfirmRequest(BaseModel):
    option_id: str


class ImpliedAvailability(BaseModel):
    """Disponibilidad inferida pragmaticamente (ej. "no puedo despues de las 16"
    implica poder antes). Solo se aplica si la persona no tiene nada ese dia."""

    participant_name: str
    slots: list[TimeSlot] = Field(default_factory=list)


class ExtractedAvailability(BaseModel):
    participants: list[Participant] = Field(default_factory=list)
    removals: list[AvailabilityRemoval] = Field(default_factory=list)
    implied: list[ImpliedAvailability] = Field(default_factory=list)


class ScheduleEntry(BaseModel):
    """Restriccion semantica que emite el LLM: el modelo razona el lenguaje y
    esta IR tipada se compila de forma determinista a ExtractedAvailability."""

    person: str = ""
    kind: Literal["available", "unavailable", "only"] = "available"
    days: list[str] = Field(default_factory=list)
    start: str | None = None
    end: str | None = None
    # 0 = esta semana / proxima ocurrencia; 1 = "la otra/proxima semana"; etc.
    week_offset: int = 0


class ScheduleInterpretation(BaseModel):
    entries: list[ScheduleEntry] = Field(default_factory=list)


class AddParticipantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


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
    listening_enabled: bool = True
    trigger_word: str = Field(default="@coordina", min_length=2, max_length=40)
    reply_format: ReplyFormat | None = None
    group_jid: str | None = Field(default=None, max_length=120)
    group_name: str | None = Field(default=None, max_length=120)
    group_participant_count: int | None = Field(default=None, ge=0, le=2048)


class ChannelMessageRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=500)


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


class RuntimeInfo(BaseModel):
    provider: str
    provider_label: str
    model: str
    cache_enabled: bool
    fallback_enabled: bool
    gemini_configured: bool
    warnings: list[str] = Field(default_factory=list)


class ExportResponse(BaseModel):
    filename: str
    text: str
