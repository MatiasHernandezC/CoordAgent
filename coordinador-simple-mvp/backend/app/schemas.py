from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

Day = Literal["lunes", "martes", "miercoles", "jueves", "viernes"]
SessionStatus = Literal["draft", "calculated", "confirmed"]

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
    available_participants: list[str]
    unavailable_participants: list[str] = Field(default_factory=list)
    score: int
    coverage_percent: int = 0
    explanation: str = ""


class AvailabilityCell(BaseModel):
    day: Day
    start: str
    end: str
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


class ChannelMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    sender: str
    text: str
    kind: Literal["human", "agent"] = "human"
    detected_invocation: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    participants: list[Participant] = Field(default_factory=list)
    options: list[TimeOption] = Field(default_factory=list)
    availability_matrix: list[AvailabilityCell] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    channel_config: ChannelConfig = Field(default_factory=ChannelConfig)
    channel_messages: list[ChannelMessage] = Field(default_factory=list)
    last_agent_reply: str | None = None
    selected_option: TimeOption | None = None
    decision_summary: str | None = None
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


class ExtractedAvailability(BaseModel):
    participants: list[Participant] = Field(default_factory=list)
    removals: list[AvailabilityRemoval] = Field(default_factory=list)


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
