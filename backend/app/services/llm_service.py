import json
import logging
import math
import re
import subprocess
import time
import unicodedata
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from urllib.error import HTTPError
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.prompts.extraction_prompt import EXTRACTION_PROMPT
from app.schemas import (
    AvailabilityRemoval,
    ChannelMessage,
    ExtractedAvailability,
    ImpliedAvailability,
    Participant,
    ScheduleInterpretation,
    TimeSlot,
    TokenUsage,
)
from app.services.schedule_compiler import compile_interpretation
from app.settings import settings

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
DAY_ALIASES = {
    "lunes": ("lunes", "lun"),
    "martes": ("martes", "mar"),
    "miercoles": ("miercoles", "mierc", "mier", "mie"),
    "jueves": ("jueves", "jue"),
    "viernes": ("viernes", "vier", "vie"),
}
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 18
logger = logging.getLogger(__name__)


class GeminiApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(f"Gemini API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds


class LlmUnavailableError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int = 503,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def estimate_gemini_cost(
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    input_cost = (
        prompt_tokens / 1_000_000
    ) * settings.gemini_input_price_per_million

    output_cost = (
        completion_tokens / 1_000_000
    ) * settings.gemini_output_price_per_million

    return round(input_cost + output_cost, 8)

class LlmService:
    def __init__(self) -> None:
        self._cache: dict[
            str,
            tuple[ExtractedAvailability, str, TokenUsage | None]
        ] = {}
        self._gemini_blocked_until = 0.0
        self._gemini_block_reason = ""

    def extract_availability(self, message: str) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        cache_key = build_cache_key(message)
        if settings.llm_cache_enabled and cache_key in self._cache:
            cached_extraction, cached_source, cached_tokens = self._cache[cache_key]

            return (
                cached_extraction.model_copy(deep=True),
                f"{cached_source}_cache",
                build_cached_token_usage(cached_tokens),
            )

        if settings.llm_provider == "local":
            try:
                return self._remember(cache_key, self._extract_with_local_model(message), f"local_{settings.local_llm_model}", None)
            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"El proveedor local fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error
                # No cachear fallbacks: el proximo intento debe reintentar el LLM real.
                fallback = self._extract_with_mock_rules(message)
                return fallback, f"mock_fallback_local_{settings.local_llm_model}", None

        if settings.llm_provider == "gemini":
            if self._is_gemini_in_cooldown():
                if not settings.llm_fallback_enabled:
                    retry_after = self._gemini_retry_after_seconds()
                    raise LlmUnavailableError(
                        (
                            "Gemini esta temporalmente pausado por cuota o disponibilidad. "
                            f"Espera {retry_after} segundos o activa LLM_FALLBACK_ENABLED=true para seguir con mock."
                        ),
                        status_code=429,
                        retry_after_seconds=retry_after,
                    )
                fallback = self._extract_with_mock_rules(message)
                source = f"mock_fallback_gemini_cooldown_{settings.gemini_model}"
                return fallback, source, None

            try:
                extraction, token_usage = self._extract_with_gemini(message)

                logger.info(
                    "Gemini token usage: prompt=%s completion=%s total=%s estimated_cost_usd=%s",
                    token_usage.prompt_tokens,
                    token_usage.completion_tokens,
                    token_usage.total_tokens,
                    token_usage.estimated_cost_usd,
                )

                return self._remember(
                    cache_key,
                    extraction,
                    f"gemini_{settings.gemini_model}",
                    token_usage,
                )

            except GeminiApiError as error:
                if error.status_code in {429, 503}:
                    cooldown = error.retry_after_seconds or settings.gemini_cooldown_seconds
                    self._block_gemini(cooldown, f"Gemini devolvio {error.status_code}.")

                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        build_gemini_unavailable_message(error),
                        status_code=error.status_code,
                        retry_after_seconds=error.retry_after_seconds,
                    ) from error

                logger.warning("Gemini failed; using mock fallback. %s", error)

                # No cachear fallbacks: un 429/503 pasajero no debe dejar pegada
                # la interpretacion del mock para este mensaje.
                fallback = self._extract_with_mock_rules(message)
                return fallback, f"mock_fallback_gemini_{error.status_code}_{settings.gemini_model}", None

            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"Gemini fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error

                logger.warning("Gemini failed before request completion; using mock fallback. %s", error)

                fallback = self._extract_with_mock_rules(message)
                return fallback, f"mock_fallback_gemini_{settings.gemini_model}", None
        if settings.llm_provider == "ollama":
            try:
                return self._remember(cache_key, self._extract_with_ollama(message), "ollama", None)
            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"Ollama fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error
                fallback = self._extract_with_mock_rules(message)
                return fallback, "mock_fallback", None

        return self._remember(cache_key, self._extract_with_mock_rules(message), "mock", None)

    def extract_channel_availability(
        self,
        messages: list[ChannelMessage]
    ) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        transcript = build_channel_extraction_text(messages)
        if not has_extractable_scheduling_signal(transcript):
            return ExtractedAvailability(), "channel_no_new_availability", None

        extraction, source, token_usage = self.extract_availability(transcript)
        extraction = normalize_channel_extraction(extraction, messages)
        return extraction, f"channel_{source}", token_usage

    def _extract_with_ollama(self, message: str) -> ExtractedAvailability:
        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": message},
            ],
        }
        request = urllib.request.Request(
            settings.ollama_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))

        content = raw["message"]["content"]
        parsed = json.loads(extract_json(content))
        extraction = parse_extraction_payload(parsed, message)
        return merge_missing_participants(extraction, self._extract_with_mock_rules(message))

    def _extract_with_local_model(self, message: str) -> ExtractedAvailability:
        if not settings.local_llm_script.exists():
            raise FileNotFoundError(f"No existe local_llm.py en {settings.local_llm_script}")

        command = [
            settings.local_llm_python,
            str(settings.local_llm_script),
            "--model",
            settings.local_llm_model,
            "--max-tokens",
            str(settings.local_llm_max_tokens),
            "--temperature",
            "0.1",
            build_extraction_prompt(message),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.local_llm_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "El LLM local fallo sin detalle.")

        parsed = json.loads(extract_json(result.stdout))
        extraction = parse_extraction_payload(parsed, message)
        return merge_missing_participants(extraction, self._extract_with_mock_rules(message))

    def _extract_with_gemini(
        self,
        message: str,
    ) -> tuple[ExtractedAvailability, TokenUsage]:
        if not settings.gemini_api_key:
            raise ValueError("Falta GEMINI_API_KEY")

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": build_extraction_prompt(message)}],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        url = settings.gemini_url.format(model=settings.gemini_model)

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": settings.gemini_api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=settings.gemini_timeout_seconds,
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))

        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")

            raise GeminiApiError(
                error.code,
                detail,
                parse_retry_delay_seconds(detail),
            ) from error

        content = extract_gemini_text(raw)

        parsed = json.loads(extract_json(content))

        extraction = parse_extraction_payload(parsed, message)
        extraction = merge_missing_participants(extraction, self._extract_with_mock_rules(message))

        usage = raw.get("usageMetadata", {})

        prompt_tokens = usage.get("promptTokenCount", 0)

        completion_tokens = usage.get("candidatesTokenCount", 0)

        total_tokens = usage.get("totalTokenCount", 0)

        token_usage = TokenUsage(
            provider="gemini",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimate_gemini_cost(
                prompt_tokens,
                completion_tokens,
            ),
        )

        return extraction, token_usage

    def _is_gemini_in_cooldown(self) -> bool:
        return time.monotonic() < self._gemini_blocked_until

    def _gemini_retry_after_seconds(self) -> int:
        return max(1, math.ceil(self._gemini_blocked_until - time.monotonic()))

    def _block_gemini(self, seconds: int, reason: str) -> None:
        self._gemini_blocked_until = time.monotonic() + max(seconds, 1)
        self._gemini_block_reason = reason
        logger.warning("Gemini provider paused for %s seconds. %s", seconds, reason)
    
    def _extract_with_mock_rules(self, message: str) -> ExtractedAvailability:
        participants: dict[str, Participant] = {}
        removals: list[AvailabilityRemoval] = []
        fragments = split_scheduling_fragments(message)

        for fragment in fragments:
            names = detect_participant_names(fragment)
            if not names:
                continue

            slots = detect_slots(fragment)
            week = detect_week_offset(fragment)
            if week:
                slots = [slot.model_copy(update={"week_offset": week}) for slot in slots]
            for name in names:
                participant = participants.setdefault(name, Participant(name=name))
                if is_unavailability_fragment(fragment):
                    removals.append(AvailabilityRemoval(participant_name=name, slots=slots))
                else:
                    participant.availability.extend(slots)

        extraction = ExtractedAvailability(
            participants=list(participants.values()),
            removals=removals,
            implied=build_implied_from_removals(removals),
        )
        return apply_exclusive_availability_overrides(extraction, message)

    def _remember(
        self,
        cache_key: str,
        extraction: ExtractedAvailability,
        source: str,
        token_usage: TokenUsage | None = None,
    ) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        if settings.llm_cache_enabled:
            if len(self._cache) >= settings.llm_cache_max_items:
                oldest_key = next(iter(self._cache))
                self._cache.pop(oldest_key, None)
            self._cache[cache_key] = (
                extraction.model_copy(deep=True),
                source,
                token_usage,
            )

        return extraction, source, token_usage


def split_scheduling_fragments(message: str) -> list[str]:
    fragments: list[str] = []
    base_fragments = re.split(r",|;|\n", message, flags=re.IGNORECASE)
    split_between_people = (
        r"\s+\b(?:y|pero)\b\s+"
        r"(?=\w+\s+"
        r"(?:si\s+)?(?:solo\s+|solamente\s+|unicamente\s+)?"
        r"(?:ya\s+no\s+|no\s+)?"
        r"(?:puede|puedo|podre|podra|podia|podria|tiene|esta|le\s+sirve|le\s+acomoda|le\s+tinca|le\s+va\s+bien|va\s+a\s+participar|participa|se\s+suma)"
        r"\b)"
    )

    for fragment in base_fragments:
        for piece in re.split(split_between_people, fragment, flags=re.IGNORECASE):
            if piece and piece.strip():
                fragments.append(piece.strip())

    return fragments


NAME_STOPWORDS = {
    "a",
    "adelante",
    "ahi",
    "al",
    "antes",
    "con",
    "de",
    "del",
    "despues",
    "dia",
    "dias",
    "el",
    "eso",
    "esto",
    "hoy",
    "la",
    "las",
    "manana",
    "pasado",
    "lo",
    "los",
    "mi",
    "mis",
    "ningun",
    "nigun",
    "no",
    "nos",
    "que",
    "se",
    "semana",
    "si",
    "solo",
    "tambien",
    "tarde",
    "todos",
    "un",
    "una",
    "yo",
    *WEEKDAYS,
}
NAME_LIST_PATTERN = r"[a-z0-9][a-z0-9_-]{1,}(?:\s*(?:,|\by\b)\s*[a-z0-9][a-z0-9_-]{1,}){0,5}"
NEGATIVE_INTENT_PATTERN = (
    r"ya\s+no\s+puede|ya\s+no\s+pueden|no\s+puede|no\s+pueden|"
    r"ya\s+no\s+podre|no\s+podre|no\s+cree\s+que\s+pueda|no\s+cree\s+pueda|"
    r"no\s+podra|no\s+podran|no\s+podria|no\s+podrian|no\s+podia|no\s+podian|"
    r"no\s+tiene|no\s+tienen|no\s+esta|no\s+estan|"
    r"no\s+le\s+sirve|no\s+les\s+sirve|no\s+le\s+acomoda|no\s+les\s+acomoda|"
    r"no\s+le\s+tinca|no\s+les\s+tinca|no\s+le\s+va\s+bien|no\s+les\s+va\s+bien"
)
POSITIVE_INTENT_PATTERN = (
    r"puede|pueden|puedo|podre|podra|podran|podia|podian|podria|podrian|"
    r"tiene|tienen|esta|estan|disponible|disponibles|libre|libres|"
    r"le\s+sirve|les\s+sirve|le\s+acomoda|les\s+acomoda|"
    r"le\s+tinca|les\s+tinca|le\s+va\s+bien|les\s+va\s+bien"
)
REPORTED_SPEECH_PATTERN = r"dijo|dice|comento|aviso|menciono|cuenta|conto"
PARTICIPATION_PATTERN = (
    r"va\s+a\s+participar|van\s+a\s+participar|participa|participan|"
    r"se\s+quiere\s+unir|se\s+quieren\s+unir|se\s+suma|se\s+suman"
)


def detect_participant_names(fragment: str) -> list[str]:
    normalized_fragment = normalize(fragment)
    candidates: list[str] = []

    patterns = [
        rf"\b(?P<names>{NAME_LIST_PATTERN})\s+(?P<intent>{NEGATIVE_INTENT_PATTERN})\b",
        rf"\b(?P<names>{NAME_LIST_PATTERN})\s+(?:{REPORTED_SPEECH_PATTERN})\s+que\s+(?:si\s+)?(?:solo\s+|solamente\s+|unicamente\s+)?(?P<intent>{POSITIVE_INTENT_PATTERN}|{NEGATIVE_INTENT_PATTERN}|{PARTICIPATION_PATTERN})\b",
        rf"\b(?P<names>{NAME_LIST_PATTERN})\s+(?:tambien\s+)?(?P<intent>{PARTICIPATION_PATTERN})\b",
        rf"\b(?P<names>{NAME_LIST_PATTERN})\s+(?:si\s+)?(?:solo\s+|solamente\s+|unicamente\s+)?(?P<intent>{POSITIVE_INTENT_PATTERN})\b",
        rf"\ba\s+(?P<names>{NAME_LIST_PATTERN})\s+(?:si\s+)?(?:solo\s+|solamente\s+|unicamente\s+)?(?P<intent>le\s+sirve|les\s+sirve|le\s+acomoda|les\s+acomoda|le\s+tinca|les\s+tinca|le\s+va\s+bien|les\s+va\s+bien)\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, normalized_fragment):
            candidates.extend(split_candidate_names(match.group("names")))

    if (
        re.search(r"\byo\b", normalized_fragment)
        or re.search(r"\bpuedo\b", normalized_fragment)
        or re.search(r"\bpodre\b", normalized_fragment)
    ):
        candidates.append("Yo")

    if re.search(r"\b(estoy\s+libre|me\s+sirve|me\s+acomoda|me\s+tinca|me\s+va\s+bien)\b", normalized_fragment):
        candidates.append("Yo")

    if is_unavailability_fragment(fragment) and re.search(
        r"\b(pueda|me\s+desocupo|tendre|tengo|estoy|estare)\b",
        normalized_fragment,
    ):
        candidates.append("Yo")

    return unique_names(candidates)


def detect_name(fragment: str) -> str | None:
    names = detect_participant_names(fragment)
    return names[0] if names else None


def split_candidate_names(raw_names: str) -> list[str]:
    return [
        name
        for piece in re.split(r"\s*(?:,|\by\b)\s*", raw_names)
        if (name := clean_candidate_name(piece))
    ]


def clean_candidate_name(value: str) -> str | None:
    normalized_name = normalize(value).strip()
    if not normalized_name or normalized_name in NAME_STOPWORDS:
        return None
    if len(normalized_name) < 2 or not re.fullmatch(r"[a-z0-9_-]+", normalized_name):
        return None
    return normalized_name.capitalize()


def unique_names(names: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def build_implied_from_removals(removals: list[AvailabilityRemoval]) -> list[ImpliedAvailability]:
    """Pragmatica de no-disponibilidad parcial: "no puedo despues de las 16" implica
    poder antes. El merge solo la aplica si la persona no tiene nada ese dia."""
    implied: list[ImpliedAvailability] = []
    for removal in removals:
        complement: list[TimeSlot] = []
        for slot in removal.slots:
            start_hour = int(slot.start[:2])
            end_hour = int(slot.end[:2])
            if start_hour > 9:
                complement.append(
                    TimeSlot(day=slot.day, start="09:00", end=slot.start, week_offset=slot.week_offset)
                )
            if end_hour < 18:
                complement.append(
                    TimeSlot(day=slot.day, start=slot.end, end="18:00", week_offset=slot.week_offset)
                )
        if complement:
            implied.append(
                ImpliedAvailability(participant_name=removal.participant_name, slots=complement)
            )
    return implied


def parse_extraction_payload(parsed: dict, message: str) -> ExtractedAvailability:
    """Parsea la salida del LLM: esquema semantico nuevo (entries) o legacy
    (participants/removals) como red de seguridad si el modelo no siguio el formato."""
    if isinstance(parsed, dict) and "entries" in parsed:
        interpretation = ScheduleInterpretation.model_validate(parsed)
        extraction = compile_interpretation(interpretation)
        return normalize_llm_extraction(extraction, message)

    extraction = ExtractedAvailability.model_validate(parsed)
    return normalize_llm_extraction(extraction, message)


def extraction_person_names(extraction: ExtractedAvailability) -> set[str]:
    names = {participant.name.strip().lower() for participant in extraction.participants}
    names.update(removal.participant_name.strip().lower() for removal in extraction.removals)
    names.update(item.participant_name.strip().lower() for item in extraction.implied)
    return {name for name in names if name}


def merge_missing_participants(
    primary: ExtractedAvailability,
    secondary: ExtractedAvailability,
) -> ExtractedAvailability:
    """Complementa la interpretacion del LLM SOLO con personas que este no menciono.

    Nunca mezcla slots de una misma persona: la union por slots solo puede ampliar
    disponibilidad y destruye topes como "puedo hasta las 16" (el LLM dice 09-16 y
    el mock agregaria 16-18)."""
    known = extraction_person_names(primary)

    participants = list(primary.participants)
    removals = list(primary.removals)
    implied = list(primary.implied)

    for participant in secondary.participants:
        if participant.name.strip().lower() not in known:
            participants.append(participant.model_copy(deep=True))

    for removal in secondary.removals:
        if removal.participant_name.strip().lower() not in known:
            removals.append(removal.model_copy(deep=True))

    for item in secondary.implied:
        if item.participant_name.strip().lower() not in known:
            implied.append(item.model_copy(deep=True))

    return primary.model_copy(
        update={"participants": participants, "removals": removals, "implied": implied}
    )


def merge_extractions(primary: ExtractedAvailability, secondary: ExtractedAvailability) -> ExtractedAvailability:
    participants: list[Participant] = []
    removals: list[AvailabilityRemoval] = []

    for participant in [*primary.participants, *secondary.participants]:
        merge_participant(participants, participant)

    for removal in [*primary.removals, *secondary.removals]:
        merge_removal(removals, removal)

    return primary.model_copy(
        update={
            "participants": participants,
            "removals": removals,
        }
    )


def merge_participant(participants: list[Participant], participant: Participant) -> None:
    existing = next(
        (
            item
            for item in participants
            if item.name.strip().lower() == participant.name.strip().lower()
        ),
        None,
    )
    if existing is None:
        participants.append(participant.model_copy(deep=True))
        return

    known_slots = {slot_key(slot) for slot in existing.availability}
    for slot in participant.availability:
        key = slot_key(slot)
        if key not in known_slots:
            existing.availability.append(slot.model_copy(deep=True))
            known_slots.add(key)


def merge_removal(removals: list[AvailabilityRemoval], removal: AvailabilityRemoval) -> None:
    existing_removal = next(
        (
            item
            for item in removals
            if item.participant_name.strip().lower() == removal.participant_name.strip().lower()
        ),
        None,
    )
    if existing_removal is None:
        removals.append(removal.model_copy(deep=True))
        return

    known_slots = {slot_key(slot) for slot in existing_removal.slots}
    for slot in removal.slots:
        key = slot_key(slot)
        if key not in known_slots:
            existing_removal.slots.append(slot.model_copy(deep=True))
            known_slots.add(key)


def slot_key(slot: TimeSlot) -> tuple[str, str, str]:
    return slot.day, slot.start, slot.end


def detect_time_range(text: str) -> tuple[int, int] | None:
    normalized = normalize(text)
    time_part = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
    match = re.search(
        rf"\b(?:de|desde|entre)?\s*(?:las\s*)?{time_part}\s*(?:a|hasta|-|y)\s*(?:las\s*)?{time_part}\b",
        normalized,
    )
    if not match:
        return None

    start_raw = int(match.group(1))
    start_minute = parse_minute(match.group(2))
    start_suffix = match.group(3)
    end_raw = int(match.group(4))
    end_minute = parse_minute(match.group(5))
    end_suffix = match.group(6)

    if start_minute is None or end_minute is None:
        return None

    inferred_start_suffix = infer_range_suffix(start_raw, start_suffix, end_suffix)
    inferred_end_suffix = end_suffix or start_suffix
    start_hour = normalize_range_hour(start_raw, inferred_start_suffix, normalized)
    end_hour = normalize_range_hour(end_raw, inferred_end_suffix, normalized)

    if end_hour <= start_hour and end_raw <= 12:
        end_hour += 12

    start_block = start_hour
    end_block = end_hour + (1 if end_minute > 0 else 0)
    return clamp_hour_range(start_block, end_block)


def parse_minute(value: str | None) -> int | None:
    if value is None:
        return 0

    minute = int(value)
    if minute > 59:
        return None
    return minute


def infer_range_suffix(hour: int, own_suffix: str | None, other_suffix: str | None) -> str | None:
    if own_suffix:
        return own_suffix
    if other_suffix == "pm" and hour <= 7:
        return "pm"
    if other_suffix == "am":
        return "am"
    return None


def normalize_range_hour(hour: int, suffix: str | None, text: str) -> int:
    if suffix == "pm":
        return hour + 12 if hour < 12 else hour
    if suffix == "am":
        return 0 if hour == 12 else hour
    if hour >= 13:
        return hour
    if "noche" in text and hour < 12:
        return hour + 12
    if hour < 8:
        return hour + 12
    return hour


def clamp_hour_range(start_hour: int, end_hour: int) -> tuple[int, int] | None:
    start = max(WORKDAY_START_HOUR, min(start_hour, WORKDAY_END_HOUR))
    end = max(WORKDAY_START_HOUR, min(end_hour, WORKDAY_END_HOUR))
    if end <= start:
        return None
    return start, end


def is_unavailability_fragment(fragment: str) -> bool:
    normalized = normalize(fragment)
    return any(
        pattern in normalized
        for pattern in [
            "ya no puedo",
            "no puedo",
            "ya no puede",
            "no puede",
            "ya no pueden",
            "no pueden",
            "ya no podre",
            "no podre",
            "no creo pueda",
            "no creo que pueda",
            "no cree pueda",
            "no cree que pueda",
            "no podra",
            "no podran",
            "no podria",
            "no podrian",
            "no podia",
            "no podian",
            "no me sirve",
            "no le sirve",
            "no les sirve",
            "no me acomoda",
            "no le acomoda",
            "no les acomoda",
            "no me tinca",
            "no le tinca",
            "no les tinca",
            "no me va bien",
            "no le va bien",
            "no les va bien",
            "no esta disponible",
            "no estan disponibles",
            "no estoy libre",
            "no esta libre",
            "no estan libres",
            "sin disponibilidad",
            "me desocupo",
            "estare fuera",
            "estoy fuera",
            "fuera de mi casa",
            "tendre que estar fuera",
            "tengo que estar fuera",
            "estoy ocupado",
            "estare ocupado",
        ]
    )


def build_channel_extraction_text(messages: list[ChannelMessage]) -> str:
    fragments: list[str] = []
    context_day: str | None = None
    for message in messages:
        if message.kind != "human":
            continue

        text = remove_invocation_tokens(message.text)
        fragment = rewrite_first_person_availability(message.sender, text)
        # Ancla las referencias relativas ("hoy", "manana", "pasado manana") a un
        # dia habil concreto en el propio texto, para que tanto el LLM como el
        # mock reciban un dia real y no expandan a toda la semana.
        fragment = resolve_relative_days(fragment)
        normalized = normalize(fragment)

        explicit_days = detect_days(normalized)
        if explicit_days:
            context_day = explicit_days[-1]
        elif context_day and should_apply_context_day(normalized):
            fragment = f"{fragment} {context_day}"

        # Prefijo con el remitente: conserva la autoria de cada mensaje para que
        # el LLM atribuya la primera persona ("me queda el viernes") a quien
        # escribio, y para que el grounding reconozca los nombres.
        fragments.append(f"- {message.sender.strip()}: {fragment}")

    return "\n".join(fragment for fragment in fragments if fragment)


def normalize_channel_extraction(
    extraction: ExtractedAvailability,
    messages: list[ChannelMessage],
) -> ExtractedAvailability:
    signal_senders = {
        message.sender.strip()
        for message in messages
        if message.kind == "human"
        and has_extractable_scheduling_signal(
            rewrite_first_person_availability(
                message.sender,
                remove_invocation_tokens(message.text),
            )
        )
    }
    if len(signal_senders) != 1:
        return extraction

    sender = next(iter(signal_senders))
    for participant in extraction.participants:
        if participant.name.strip().lower() == "yo":
            participant.name = sender

    for removal in extraction.removals:
        if removal.participant_name.strip().lower() == "yo":
            removal.participant_name = sender

    for item in extraction.implied:
        if item.participant_name.strip().lower() == "yo":
            item.participant_name = sender

    return extraction


def has_today_reference(normalized_text: str) -> bool:
    return bool(re.search(r"\bhoy\b", normalized_text))


def rewrite_today_reference(text: str, day: str) -> str:
    return re.sub(r"\bhoy\b", day, text, flags=re.IGNORECASE)


def resolve_relative_days(text: str, now: datetime | None = None) -> str:
    """Reemplaza referencias de fecha relativas por el dia habil concreto:
    "hoy"/"manana"/"pasado manana" -> nombre de dia. Desambigua "manana" (dia
    siguiente) de "la/en la/por la manana" (franja horaria), que se deja intacta.
    El anclaje usa current_workday_name para respetar zona horaria y tests."""
    today = current_workday_name(now)
    if today not in WEEKDAYS:
        return text  # fin de semana: fuera de la ventana de planificacion

    base = WEEKDAYS.index(today)

    def weekday_for(offset: int) -> str:
        index = base + offset
        while index % 7 >= len(WEEKDAYS):  # sabado/domingo -> lunes siguiente
            index += 1
        return WEEKDAYS[index % 7]

    def tomorrow_repl(match: "re.Match[str]") -> str:
        preceding = match.string[max(0, match.start() - 6):match.start()].lower()
        if re.search(r"\bla\s+$", preceding):  # "la/en la/por la/de la manana" = franja
            return match.group(0)
        return weekday_for(1)

    text = re.sub(r"\bpasado\s+ma[nñ]ana\b", weekday_for(2), text, flags=re.IGNORECASE)
    text = re.sub(r"\bma[nñ]ana\b", tomorrow_repl, text, flags=re.IGNORECASE)
    text = re.sub(r"\bhoy\b", weekday_for(0), text, flags=re.IGNORECASE)
    return text


def _load_zoneinfo(name: str) -> tzinfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def current_workday_name(now: datetime | None = None) -> str | None:
    # Cadena de fallback: zona configurada -> Santiago -> UTC. El ultimo escalon
    # no depende de la base IANA, asi que nunca lanza (p. ej. Windows sin tzdata).
    timezone = _load_zoneinfo(settings.app_timezone) or _load_zoneinfo("America/Santiago") or dt_timezone.utc

    current = now.astimezone(timezone) if now else datetime.now(timezone)
    weekday = current.weekday()
    if weekday >= len(WEEKDAYS):
        return None
    return WEEKDAYS[weekday]


def should_apply_context_day(normalized_text: str) -> bool:
    if detect_days(normalized_text):
        return False
    return has_extractable_scheduling_signal(normalized_text) and has_hour_reference(normalized_text)


def has_hour_reference(normalized_text: str) -> bool:
    if detect_time_range(normalized_text):
        return True
    return bool(
        re.search(
            r"\b(?:a\s+(?:la|las|los)|desde\s+(?:la|las)|despues\s+de\s+(?:la|las)|tipo\s+(?:la|las)|como\s+a\s+(?:la|las))\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
            normalized_text,
        )
    )


def has_extractable_scheduling_signal(text: str) -> bool:
    normalized = normalize(text)
    has_named_participant_signal = bool(detect_participant_names(normalized)) and bool(
        re.search(
            rf"\b({POSITIVE_INTENT_PATTERN}|{NEGATIVE_INTENT_PATTERN}|{PARTICIPATION_PATTERN}|{REPORTED_SPEECH_PATTERN}\s+que)\b",
            normalized,
        )
    )
    has_time_anchor = bool(detect_days(normalized)) or any(
        pattern in normalized
        for pattern in [
            "todos los dias",
            "todos los dias habiles",
            "manana",
            "tarde",
            "atrde",
            "cualquier hora",
            "desde las",
            "desde la",
            "despues de las",
            "despues de la",
            "a las",
            "a la",
            "hasta las",
            "hasta la",
            "desocupo",
            "solo",
            "solamente",
            "unicamente",
            "entre",
        ]
    )
    has_time_anchor = has_time_anchor or detect_time_range(normalized) is not None
    has_availability_intent = any(
        pattern in normalized
        for pattern in [
            "puedo",
            "puede",
            "pueden",
            "podre",
            "podra",
            "podran",
            "podia",
            "podian",
            "podria",
            "podrian",
            "disponible",
            "libre",
            "sirve",
            "acomoda",
            "tinca",
            "va bien",
            "participar",
            "participa",
            "se suma",
            "se quiere unir",
            "dijo que",
            "dice que",
            "no creo",
            "fuera",
            "ocupado",
            "desocupo",
        ]
    )
    return has_named_participant_signal or (has_time_anchor and has_availability_intent)


def remove_invocation_tokens(text: str) -> str:
    return re.sub(r"@\w+", "", text).strip()


def _timezone_now(now: datetime | None = None) -> datetime:
    tz = _load_zoneinfo(settings.app_timezone) or _load_zoneinfo("America/Santiago") or dt_timezone.utc
    return now.astimezone(tz) if now else datetime.now(tz)


def _relative_workday(current: datetime, offset: int) -> str:
    """Dia habil para una fecha relativa; fin de semana rueda al lunes siguiente."""
    target = current + timedelta(days=offset)
    while target.weekday() >= len(WEEKDAYS):
        target += timedelta(days=1)
    return WEEKDAYS[target.weekday()]


def build_temporal_context(now: datetime | None = None) -> str:
    current = _timezone_now(now)
    hoy = _relative_workday(current, 0)
    manana = _relative_workday(current, 1)
    pasado = _relative_workday(current, 2)
    fecha = current.strftime("%d/%m/%Y")
    return (
        "Contexto temporal (usalo para resolver referencias relativas a un dia habil concreto):\n"
        f"- Hoy es {hoy} {fecha}.\n"
        f'- "hoy" = {hoy}. "manana" (como dia) = {manana}. "pasado manana" = {pasado}.\n'
        '- "este <dia>", "el <dia>", "proximo <dia>" = ese dia habil.\n'
        '- Recuerda: un dia relativo es UN dia especifico, nunca "todos".\n'
        "- Semana (campo week_offset): esta semana / \"el <dia>\" / \"hoy\" / \"manana\" = 0. "
        '"la otra semana", "la proxima semana", "la semana que viene" = 1. '
        '"en 2 semanas", "en dos semanas" = 2. En caso de duda usa 0.'
    )


def build_extraction_prompt(message: str, now: datetime | None = None) -> str:
    return (
        f"{EXTRACTION_PROMPT.strip()}\n\n"
        f"{build_temporal_context(now)}\n\n"
        "Texto a analizar:\n"
        f"{message}\n\n"
        "Respuesta esperada: solo el objeto JSON valido, sin markdown, sin explicaciones."
    )


def build_local_extraction_prompt(message: str) -> str:
    return build_extraction_prompt(message)


def rewrite_first_person_availability(sender: str, text: str) -> str:
    normalized = normalize(text)
    name = sender.strip()

    if re.search(r"\byo\s+(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?podre\b", normalized):
        return re.sub(
            r"\byo\s+(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?podre\b",
            lambda match: f"{name} {'solo ' if match.group(2) else ''}puede",
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    if re.search(r"\byo\s+(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?puedo\b", normalized):
        return re.sub(
            r"\byo\s+(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?puedo\b",
            lambda match: f"{name} {'solo ' if match.group(2) else ''}puede",
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    if re.search(r"\byo\s+ya\s+no\s+puedo\b", normalized):
        return re.sub(r"\byo\s+ya\s+no\s+puedo\b", f"{name} ya no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\byo\s+no\s+puedo\b", normalized):
        return re.sub(r"\byo\s+no\s+puedo\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\byo\s+ya\s+no\s+podre\b", normalized):
        return re.sub(r"\byo\s+ya\s+no\s+podre\b", f"{name} ya no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\byo\s+no\s+podre\b", normalized):
        return re.sub(r"\byo\s+no\s+podre\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bya\s+no\s+puedo\b", normalized):
        return re.sub(r"\bya\s+no\s+puedo\b", f"{name} ya no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+puedo\b", normalized):
        return re.sub(r"\bno\s+puedo\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bya\s+no\s+podre\b", normalized):
        return re.sub(r"\bya\s+no\s+podre\b", f"{name} ya no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+podre\b", normalized):
        return re.sub(r"\bno\s+podre\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+me\s+sirve\b", normalized):
        return re.sub(r"\bno\s+me\s+sirve\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+me\s+acomoda\b", normalized):
        return re.sub(r"\bno\s+me\s+acomoda\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+me\s+tinca\b", normalized):
        return re.sub(r"\bno\s+me\s+tinca\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+me\s+va\s+bien\b", normalized):
        return re.sub(r"\bno\s+me\s+va\s+bien\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bno\s+creo\s+(que\s+)?pueda\b", normalized):
        return re.sub(r"\bno\s+creo\s+(que\s+)?pueda\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if is_first_person_unavailability(text):
        return f"{name} no puede {text}"

    if re.search(r"\byo\s+puedo\b", normalized):
        return re.sub(r"\byo\s+puedo\b", f"{name} puede", text, flags=re.IGNORECASE)

    if re.search(r"\b(yo\s+)?(tambien|igual)\s+puedo\b", normalized):
        return re.sub(
            r"\b(yo\s+)?(tambi[eé]n|igual)\s+puedo\b",
            f"{name} puede",
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    if re.search(r"\bpuedo\b", normalized):
        return re.sub(r"\bpuedo\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\b(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?podre\b", normalized):
        return re.sub(
            r"\b(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?podre\b",
            lambda match: f"{name} {'solo ' if match.group(2) else ''}puede",
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    if re.search(r"\bme\s+sirve\b", normalized):
        return re.sub(r"\bme\s+sirve\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bme\s+acomoda\b", normalized):
        return re.sub(r"\bme\s+acomoda\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bme\s+tinca\b", normalized):
        return re.sub(r"\bme\s+tinca\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\bme\s+va\s+bien\b", normalized):
        return re.sub(r"\bme\s+va\s+bien\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

    if re.search(r"\b(yo\s+)?estoy\s+libre\b", normalized):
        return re.sub(r"\b(yo\s+)?estoy\s+libre\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

    return text


def detect_slots(fragment: str) -> list[TimeSlot]:
    normalized = normalize(fragment)
    days = detect_days(normalized)
    if not days:
        return []

    range_hours = detect_time_range(normalized)
    if range_hours:
        start_hour, end_hour = range_hours
        return [
            TimeSlot(day=day, start=f"{start_hour:02d}:00", end=f"{end_hour:02d}:00")
            for day in days
        ]

    start = "09:00"
    end = "18:00"

    if is_unavailability_fragment(fragment):
        until_match = re.search(r"(hasta las|hasta la|hasta al menos las|hasta al menos la|al menos hasta las|al menos hasta la)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?", normalized)
        if until_match:
            hour = normalize_hour_for_context(int(until_match.group(2)), normalized, prefer_evening=True)
            return [TimeSlot(day=day, start="09:00", end=f"{hour:02d}:00") for day in days]

        release_match = re.search(r"(me\s+desocupo|se\s+desocupa|desocupo|desocupa)\s+(a\s+(?:la|las)|recien\s+a\s+(?:la|las))?\s*(\d{1,2})(?::\d{2})?\s*(am|pm)?", normalized)
        if release_match:
            hour = normalize_hour_for_context(int(release_match.group(3)), normalized, prefer_evening=True)
            return [TimeSlot(day=day, start="09:00", end=f"{hour:02d}:00") for day in days]

    if "cualquier hora" in normalized or "todo el dia" in normalized:
        start, end = "09:00", "18:00"
    elif "manana" in normalized:
        start, end = "09:00", "12:00"
    elif "tarde" in normalized or "atrde" in normalized:
        start, end = "15:00", "18:00"

    # Tope superior en fragmentos positivos: "puede el lunes pero no despues de
    # las 4" o "esta libre el lunes hasta las 4" acotan el final, no el inicio.
    negated_after_match = re.search(
        r"\bno\b[^,;.]{0,40}?\bdespues\s+de\s+las?\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?",
        normalized,
    )
    positive_until_match = re.search(
        r"\b(?:hasta|antes\s+de)\s+las?\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?",
        normalized,
    )
    upper_bound = negated_after_match or positive_until_match
    if upper_bound and not is_unavailability_fragment(fragment):
        hour = normalize_hour_for_context(int(upper_bound.group(1)), normalized)
        bound_end = f"{hour:02d}:00"
        if bound_end <= start:
            start = "09:00"
        return [TimeSlot(day=day, start=start, end=bound_end) for day in days if bound_end > start]

    hour_match = re.search(r"(desde las|desde la|despues de las|despues de la|despues las|despues la|pasado las|pasado la|tipo las|tipo la|como a las|como a la)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?", normalized)
    if hour_match:
        hour = normalize_hour_for_context(int(hour_match.group(2)), normalized)
        start = f"{hour:02d}:00"
        end = "18:00"

    exact_hour_match = re.search(r"\ba\s+(?:la|las|los)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?\b", normalized)
    if exact_hour_match:
        hour = normalize_hour_for_context(int(exact_hour_match.group(1)), normalized)
        start = f"{hour:02d}:00"
        end = f"{hour + 1:02d}:00"

    return [TimeSlot(day=day, start=start, end=end) for day in days]


def detect_days(text: str) -> list[str]:
    normalized = normalize(text)
    if (
        "todos los dias" in normalized
        or "todos los dias habiles" in normalized
        or "cualquier dia" in normalized
        or "ningun dia" in normalized
        or "nigun dia" in normalized
        or "ningun dia habil" in normalized
        or "nigun dia habil" in normalized
    ):
        return WEEKDAYS

    days: list[str] = []
    for day, aliases in DAY_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\.?\b", normalized) for alias in aliases):
            days.append(day)

    return days


# Numeros en palabras para "en N semanas" (mock; el LLM maneja el caso general).
_WEEK_NUMBER_WORDS = {
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
}


def detect_week_offset(fragment: str) -> int:
    """Detecta a que semana se refiere el fragmento (mock).

    0 = esta semana / proxima ocurrencia (default), 1 = "la otra/proxima
    semana", N = "en N semanas". Se acota a 8 igual que el compilador."""
    normalized = normalize(fragment)

    # "en 3 semanas" / "en tres semanas" / "dentro de 2 semanas"
    numeric = re.search(r"\b(?:en|dentro\s+de)\s+(\d{1,2})\s+semanas?\b", normalized)
    if numeric:
        return max(0, min(int(numeric.group(1)), 8))
    worded = re.search(r"\b(?:en|dentro\s+de)\s+(" + "|".join(_WEEK_NUMBER_WORDS) + r")\s+semanas?\b", normalized)
    if worded:
        return _WEEK_NUMBER_WORDS[worded.group(1)]

    # "la proxima/otra/siguiente semana", "la semana que viene", "la semana entrante"
    if re.search(
        r"\b(?:la\s+)?(?:proxima|otra|siguiente|que\s+viene|entrante|subsiguiente)\s+semana\b",
        normalized,
    ) or re.search(r"\bla\s+semana\s+(?:que\s+viene|entrante|proxima|siguiente)\b", normalized):
        # "subsiguiente" coloquialmente = la de despues de la proxima, pero para el
        # mock lo tratamos como 1 (el LLM afina); "que viene" siempre es 1.
        return 1

    return 0


def is_first_person_unavailability(fragment: str) -> bool:
    normalized = normalize(fragment)
    return is_unavailability_fragment(fragment) and any(
        pattern in normalized
        for pattern in [
            "no creo pueda",
            "no creo que pueda",
            "me desocupo",
            "tendre que estar fuera",
            "tengo que estar fuera",
            "fuera de mi casa",
            "estoy ocupado",
            "estare ocupado",
        ]
    )


def normalize_hour_for_context(hour: int, text: str, prefer_evening: bool = False) -> int:
    if hour >= 13:
        return hour

    if "pm" in text or "noche" in text:
        return hour + 12 if hour < 12 else hour

    if prefer_evening and ("desocupo" in text or "recien" in text or "fuera de mi casa" in text):
        return hour + 12 if hour <= 11 else hour

    if hour < 8:
        return hour + 12

    return hour


def normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def build_cache_key(message: str) -> str:
    provider_model = {
        "local": settings.local_llm_model,
        "gemini": settings.gemini_model,
        "ollama": settings.ollama_model,
    }.get(settings.llm_provider, "mock")
    normalized_message = " ".join(message.strip().lower().split())
    # La fecha entra en la clave: "manana" cambia de dia real cada dia, cachear
    # sin fecha reutilizaria una interpretacion relativa desactualizada.
    today = _timezone_now().strftime("%Y-%m-%d")
    return f"{settings.llm_provider}:{provider_model}:{today}:{normalized_message}"


def build_cached_token_usage(token_usage: TokenUsage | None) -> TokenUsage | None:
    if token_usage is None:
        return None

    return token_usage.model_copy(
        update={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "cached": True,
        }
    )


def build_gemini_unavailable_message(error: GeminiApiError) -> str:
    if error.status_code == 429:
        retry_part = ""
        if error.retry_after_seconds:
            retry_part = f" Reintenta en {error.retry_after_seconds} segundos."
        return (
            "Gemini rechazo la solicitud por cuota o limite de uso."
            f"{retry_part} Para seguir probando hoy, usa LLM_PROVIDER=local o activa "
            "LLM_FALLBACK_ENABLED=true."
        )

    return (
        f"Gemini devolvio error {error.status_code}. "
        "Revisa la API key, el modelo configurado o activa LLM_FALLBACK_ENABLED=true."
    )


def normalize_llm_extraction(extraction: ExtractedAvailability, original_message: str) -> ExtractedAvailability:
    first_person = looks_like_first_person_message(original_message)
    invalid_names = {"", "el", "la", "los", "las", "un", "una", "al", "del", "no", "nos"}

    for participant in extraction.participants:
        participant.name = normalize_person_name(participant.name)
        if first_person and participant.name.strip().lower() in invalid_names:
            participant.name = "Yo"

    for removal in extraction.removals:
        removal.participant_name = normalize_person_name(removal.participant_name)
        if first_person and removal.participant_name.strip().lower() in invalid_names:
            removal.participant_name = "Yo"

    for item in extraction.implied:
        item.participant_name = normalize_person_name(item.participant_name)
        if first_person and item.participant_name.strip().lower() in invalid_names:
            item.participant_name = "Yo"

    extraction = discard_invalid_names(extraction, invalid_names)
    extraction = apply_grounded_availability_overrides(extraction, original_message)
    extraction = apply_exclusive_availability_overrides(extraction, original_message)
    extraction = discard_ungrounded_days(extraction, original_message)
    return ground_extraction_to_message(extraction, original_message, first_person)


def normalize_person_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return cleaned
    if cleaned.lower() == "yo":
        return "Yo"
    if cleaned.islower() or cleaned.isupper():
        return " ".join(part[:1].upper() + part[1:].lower() for part in cleaned.split())
    if cleaned[0].islower():
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned


def discard_invalid_names(extraction: ExtractedAvailability, invalid_names: set[str]) -> ExtractedAvailability:
    participants = [
        participant
        for participant in extraction.participants
        if participant.name.strip().lower() not in invalid_names
    ]
    removals = [
        removal
        for removal in extraction.removals
        if removal.participant_name.strip().lower() not in invalid_names
    ]
    implied = [
        item
        for item in extraction.implied
        if item.participant_name.strip().lower() not in invalid_names
    ]
    return extraction.model_copy(update={"participants": participants, "removals": removals, "implied": implied})


def discard_ungrounded_days(extraction: ExtractedAvailability, original_message: str) -> ExtractedAvailability:
    allowed_days = set(detect_days(original_message))
    if not allowed_days:
        return extraction

    preserve_exclusive_removals = has_exclusive_week_constraint(original_message)
    exclusive_participants = {
        normalize(participant.name)
        for participant in extraction.participants
        if any(slot.day in allowed_days for slot in participant.availability)
    }

    participants: list[Participant] = []
    for participant in extraction.participants:
        grounded_slots = [slot for slot in participant.availability if slot.day in allowed_days]
        if grounded_slots or not participant.availability:
            participants.append(participant.model_copy(update={"availability": grounded_slots}))

    removals: list[AvailabilityRemoval] = []
    for removal in extraction.removals:
        participant_key = normalize(removal.participant_name)
        keep_week_removals = preserve_exclusive_removals and (
            not exclusive_participants or participant_key in exclusive_participants
        )
        grounded_slots = [
            slot
            for slot in removal.slots
            if slot.day in allowed_days or keep_week_removals
        ]
        if grounded_slots:
            removals.append(removal.model_copy(update={"slots": grounded_slots}))

    implied: list[ImpliedAvailability] = []
    for item in extraction.implied:
        grounded_slots = [slot for slot in item.slots if slot.day in allowed_days]
        if grounded_slots:
            implied.append(item.model_copy(update={"slots": grounded_slots}))

    return extraction.model_copy(update={"participants": participants, "removals": removals, "implied": implied})


def apply_exclusive_availability_overrides(
    extraction: ExtractedAvailability,
    original_message: str,
) -> ExtractedAvailability:
    overrides = find_exclusive_availability_overrides(original_message)
    if not overrides:
        return extraction

    participants = list(extraction.participants)
    removals = list(extraction.removals)

    for participant_name, availability, removal_slots in overrides:
        existing = next(
            (participant for participant in participants if participant.name.lower() == participant_name.lower()),
            None,
        )
        if existing:
            existing.availability = availability
        else:
            participants.append(Participant(name=participant_name, availability=availability))

        allowed_days = {slot.day for slot in availability}
        removals = [
            removal
            for removal in removals
            if removal.participant_name.lower() != participant_name.lower()
            or not any(slot.day in allowed_days for slot in removal.slots)
        ]
        removals.append(AvailabilityRemoval(participant_name=participant_name, slots=removal_slots))

    return extraction.model_copy(update={"participants": participants, "removals": removals})


def apply_grounded_availability_overrides(
    extraction: ExtractedAvailability,
    original_message: str,
) -> ExtractedAvailability:
    overrides = find_grounded_availability_overrides(original_message)
    if not overrides:
        return extraction

    participants = list(extraction.participants)
    removals = list(extraction.removals)
    implied = list(extraction.implied)

    for participant_name, availability in overrides:
        override_days = {slot.day for slot in availability}
        existing = next(
            (participant for participant in participants if participant.name.lower() == participant_name.lower()),
            None,
        )
        if existing:
            existing.availability = [
                slot for slot in existing.availability if slot.day not in override_days
            ] + availability
        else:
            participants.append(Participant(name=participant_name, availability=availability))

        removals = [
            removal
            for removal in removals
            if removal.participant_name.lower() != participant_name.lower()
            or not any(slot.day in override_days for slot in removal.slots)
        ]
        implied = [
            item
            for item in implied
            if item.participant_name.lower() != participant_name.lower()
            or not any(slot.day in override_days for slot in item.slots)
        ]

    return extraction.model_copy(update={"participants": participants, "removals": removals, "implied": implied})


def has_exclusive_week_constraint(original_message: str) -> bool:
    normalized = normalize(original_message)
    return bool(
        re.search(r"\b(solo|solamente|unicamente|unico|unica)\b", normalized)
        or re.search(r"\b(?:solo\s+)?me\s+queda\b", normalized)
        or re.search(r"\b(?:solo\s+)?le\s+queda\b", normalized)
        or re.search(r"\b(?:solo\s+)?les\s+queda\b", normalized)
    )


def find_grounded_availability_overrides(original_message: str) -> list[tuple[str, list[TimeSlot]]]:
    overrides: list[tuple[str, list[TimeSlot]]] = []
    normalized = normalize(original_message)
    days = detect_days(normalized)
    if not days:
        return overrides

    release_patterns = [
        r"\b(?:vuelvo|vuelve|regreso|regresa|llego|llega)\b.{0,140}?\b(?:tipo\s+|a\s+(?:la|las)\s+|como\s+a\s+(?:la|las)\s+)(\d{1,2})(?::\d{2})?\s*(am|pm)?.{0,140}?\b(?:de\s+ahi|desde\s+ahi|en\s+adelante|libre)\b",
        r"\b(?:de\s+ahi|desde\s+ahi|en\s+adelante)\b.{0,140}?\blibre\b.*?\b(?:vuelvo|vuelve|regreso|regresa|llego|llega)\b.{0,140}?\b(?:tipo\s+|a\s+(?:la|las)\s+|como\s+a\s+(?:la|las)\s+)(\d{1,2})(?::\d{2})?\s*(am|pm)?",
    ]

    hour: int | None = None
    for pattern in release_patterns:
        match = re.search(pattern, normalized)
        if match:
            hour = normalize_hour_for_context(int(match.group(1)), normalized)
            break

    if hour is not None:
        participant_name = detect_name(original_message) or detect_release_subject_name(normalized) or "Yo"
        overrides.append(
            (
                participant_name,
                [TimeSlot(day=day, start=f"{hour:02d}:00", end="18:00") for day in days],
            )
        )

    return overrides


def detect_release_subject_name(normalized_message: str) -> str | None:
    match = re.search(
        r"\b(?P<name>[a-z0-9][a-z0-9_-]{1,})\s+"
        r"(?:vuelve|regresa|llega)\b",
        normalized_message,
    )
    if not match:
        return None
    return clean_candidate_name(match.group("name"))


def find_exclusive_availability_overrides(
    original_message: str,
) -> list[tuple[str, list[TimeSlot], list[TimeSlot]]]:
    overrides: list[tuple[str, list[TimeSlot], list[TimeSlot]]] = []
    for fragment in split_scheduling_fragments(original_message):
        if not is_exclusive_availability_fragment(fragment):
            continue

        participant_name = detect_name(fragment)
        if not participant_name:
            continue

        availability = detect_slots(fragment)
        if not availability:
            continue

        week = detect_week_offset(fragment)
        if week:
            availability = [slot.model_copy(update={"week_offset": week}) for slot in availability]

        available_days = {slot.day for slot in availability}
        removal_slots = [
            TimeSlot(day=day, start="09:00", end="18:00", week_offset=week)
            for day in WEEKDAYS
            if day not in available_days
        ]
        overrides.append((participant_name, availability, removal_slots))

    return overrides


def is_exclusive_availability_fragment(fragment: str) -> bool:
    normalized = normalize(fragment)
    return bool(
        re.search(r"\b(solo|solamente|unicamente)\b", normalized)
        and re.search(r"\b(puedo|puede|pueden|podre|podra|podran|podia|podian|podria|podrian|disponible|sirve|acomoda)\b", normalized)
        and detect_days(normalized)
    )


def ground_extraction_to_message(
    extraction: ExtractedAvailability,
    original_message: str,
    first_person: bool,
) -> ExtractedAvailability:
    normalized_message = normalize(original_message)

    grounded_participants = [
        participant
        for participant in extraction.participants
        if participant.name == "Yo" and first_person or normalize(participant.name) in normalized_message
    ]
    grounded_removals = [
        removal
        for removal in extraction.removals
        if removal.slots
        and (removal.participant_name == "Yo" and first_person or normalize(removal.participant_name) in normalized_message)
    ]
    grounded_implied = [
        item
        for item in extraction.implied
        if item.slots
        and (item.participant_name == "Yo" and first_person or normalize(item.participant_name) in normalized_message)
    ]

    return extraction.model_copy(
        update={
            "participants": grounded_participants,
            "removals": grounded_removals,
            "implied": grounded_implied,
        }
    )


def looks_like_first_person_message(message: str) -> bool:
    normalized = normalize(message)
    return any(
        pattern in normalized
        for pattern in [
            "yo ",
            "puedo",
            "pueda",
            "me sirve",
            "me acomoda",
            "me tinca",
            "me va bien",
            "me desocupo",
            "vuelvo",
            "regreso",
            "llego",
            "de ahi en adelante",
            "desde ahi",
            "no creo",
            "tendre",
            "tengo",
            "estoy",
            "estare",
            "mi casa",
        ]
    )


def parse_retry_delay_seconds(detail: str) -> int | None:
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return None

    for item in payload.get("error", {}).get("details", []):
        retry_delay = item.get("retryDelay")
        if not retry_delay:
            continue
        match = re.match(r"(\d+)s", retry_delay)
        if match:
            return int(match.group(1))

    return None


def extract_json(content: str) -> str:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError("LLM response does not contain JSON")
    return match.group(0)


def extract_gemini_text(raw: dict) -> str:
    candidates = raw.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini response does not contain candidates")

    parts = candidates[0].get("content", {}).get("parts") or []
    texts = [part.get("text", "") for part in parts if part.get("text")]
    if not texts:
        raise ValueError("Gemini response does not contain text")

    return "\n".join(texts)


llm_service = LlmService()
