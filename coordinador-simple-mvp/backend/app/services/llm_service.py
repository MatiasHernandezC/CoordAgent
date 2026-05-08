import json
import logging
import math
import re
import subprocess
import time
import unicodedata
from urllib.error import HTTPError
import urllib.request

from app.prompts.extraction_prompt import EXTRACTION_PROMPT
from app.schemas import (
    AvailabilityRemoval,
    ChannelMessage,
    ExtractedAvailability,
    Participant,
    TimeSlot,
    TokenUsage,
)
from app.settings import settings

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
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
                fallback = self._extract_with_mock_rules(message)
                return self._remember(cache_key, fallback, f"mock_fallback_local_{settings.local_llm_model}", None)

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

                fallback = self._extract_with_mock_rules(message)

                return self._remember(
                    cache_key,
                    fallback,
                    f"mock_fallback_gemini_{error.status_code}_{settings.gemini_model}",
                    None
                )

            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"Gemini fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error

                logger.warning("Gemini failed before request completion; using mock fallback. %s", error)

                fallback = self._extract_with_mock_rules(message)

                return self._remember(
                    cache_key,
                    fallback,
                    f"mock_fallback_gemini_{settings.gemini_model}",
                    None
                )
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
                return self._remember(cache_key, fallback, "mock_fallback", None)

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
        return normalize_llm_extraction(ExtractedAvailability.model_validate(parsed), message)

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
        return normalize_llm_extraction(ExtractedAvailability.model_validate(parsed), message)

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

        extraction = normalize_llm_extraction(ExtractedAvailability.model_validate(parsed), message)

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
        fragments = re.split(r",|;|\by\b|\bpero\b", message, flags=re.IGNORECASE)

        for fragment in fragments:
            name = detect_name(fragment)
            if not name:
                continue

            participant = participants.setdefault(name, Participant(name=name))
            slots = detect_slots(fragment)
            if is_unavailability_fragment(fragment):
                removals.append(AvailabilityRemoval(participant_name=name, slots=slots))
            else:
                participant.availability.extend(slots)

        extraction = ExtractedAvailability(participants=list(participants.values()), removals=removals)
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


def detect_name(fragment: str) -> str | None:
    normalized_fragment = normalize(fragment)
    negative_match = re.search(
        r"\b([a-z][a-z]+)\s+(ya\s+no\s+puede|no\s+puede|ya\s+no\s+podre|no\s+podre|no\s+cree\s+que\s+pueda|no\s+cree\s+pueda|no\s+podra|no\s+tiene|no\s+esta)\b",
        normalized_fragment,
    )
    if negative_match:
        return negative_match.group(1).capitalize()

    if (
        re.search(r"\byo\b", normalized_fragment)
        or re.search(r"\bpuedo\b", normalized_fragment)
        or re.search(r"\bpodre\b", normalized_fragment)
    ):
        return "Yo"

    if is_unavailability_fragment(fragment) and re.search(
        r"\b(pueda|me\s+desocupo|tendre|tengo|estoy|estare)\b",
        normalized_fragment,
    ):
        return "Yo"

    match = re.search(
        r"\b([a-z][a-z]+)\s+(si\s+)?(solo\s+|solamente\s+|unicamente\s+)?(puede|puedo|podre|podra|tiene|esta)\b",
        normalized_fragment,
    )
    if match:
        return match.group(1).capitalize()

    return None


def is_unavailability_fragment(fragment: str) -> bool:
    normalized = normalize(fragment)
    return any(
        pattern in normalized
        for pattern in [
            "ya no puedo",
            "no puedo",
            "ya no puede",
            "no puede",
            "ya no podre",
            "no podre",
            "no creo pueda",
            "no creo que pueda",
            "no cree pueda",
            "no cree que pueda",
            "no podra",
            "no me sirve",
            "no le sirve",
            "no me acomoda",
            "no le acomoda",
            "no esta disponible",
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
    for message in messages:
        if message.kind != "human":
            continue

        text = remove_invocation_tokens(message.text)
        fragments.append(rewrite_first_person_availability(message.sender, text))

    return ", ".join(fragment for fragment in fragments if fragment)


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

    return extraction


def has_extractable_scheduling_signal(text: str) -> bool:
    normalized = normalize(text)
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
            "despues de las",
            "a las",
            "hasta las",
            "desocupo",
            "solo",
            "solamente",
            "unicamente",
        ]
    )
    has_availability_intent = any(
        pattern in normalized
        for pattern in [
            "puedo",
            "puede",
            "podre",
            "podra",
            "disponible",
            "sirve",
            "acomoda",
            "no creo",
            "fuera",
            "ocupado",
            "desocupo",
        ]
    )
    return has_time_anchor and has_availability_intent


def remove_invocation_tokens(text: str) -> str:
    return re.sub(r"@\w+", "", text).strip()


def build_extraction_prompt(message: str) -> str:
    return (
        f"{EXTRACTION_PROMPT.strip()}\n\n"
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

    if re.search(r"\bno\s+creo\s+(que\s+)?pueda\b", normalized):
        return re.sub(r"\bno\s+creo\s+(que\s+)?pueda\b", f"{name} no puede", text, count=1, flags=re.IGNORECASE)

    if is_first_person_unavailability(text):
        return f"{name} no puede {text}"

    if re.search(r"\byo\s+puedo\b", normalized):
        return re.sub(r"\byo\s+puedo\b", f"{name} puede", text, flags=re.IGNORECASE)

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

    return text


def detect_slots(fragment: str) -> list[TimeSlot]:
    normalized = normalize(fragment)
    days = detect_days(normalized)
    if not days:
        return []

    start = "09:00"
    end = "18:00"

    if is_unavailability_fragment(fragment):
        until_match = re.search(r"(hasta las|hasta al menos las|al menos hasta las)\s+(\d{1,2})", normalized)
        if until_match:
            hour = normalize_hour_for_context(int(until_match.group(2)), normalized, prefer_evening=True)
            return [TimeSlot(day=day, start="09:00", end=f"{hour:02d}:00") for day in days]

        release_match = re.search(r"(me\s+desocupo|se\s+desocupa|desocupo|desocupa)\s+(a\s+las|recien\s+a\s+las)?\s*(\d{1,2})", normalized)
        if release_match:
            hour = normalize_hour_for_context(int(release_match.group(3)), normalized, prefer_evening=True)
            return [TimeSlot(day=day, start="09:00", end=f"{hour:02d}:00") for day in days]

    if "cualquier hora" in normalized or "todo el dia" in normalized:
        start, end = "09:00", "18:00"
    elif "manana" in normalized:
        start, end = "09:00", "12:00"
    elif "tarde" in normalized or "atrde" in normalized:
        start, end = "15:00", "18:00"

    hour_match = re.search(r"(desde las|despues de las)\s+(\d{1,2})", normalized)
    if hour_match:
        hour = normalize_hour_for_context(int(hour_match.group(2)), normalized)
        start = f"{hour:02d}:00"
        end = "18:00"

    exact_hour_match = re.search(r"\ba las\s+(\d{1,2})\b", normalized)
    if exact_hour_match:
        hour = normalize_hour_for_context(int(exact_hour_match.group(1)), normalized)
        start = f"{hour:02d}:00"
        end = f"{hour + 1:02d}:00"

    return [TimeSlot(day=day, start=start, end=end) for day in days]


def detect_days(text: str) -> list[str]:
    if (
        "todos los dias" in text
        or "todos los dias habiles" in text
        or "ningun dia" in text
        or "nigun dia" in text
        or "ningun dia habil" in text
        or "nigun dia habil" in text
    ):
        return WEEKDAYS

    return [day for day in WEEKDAYS if day in text]


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
    return f"{settings.llm_provider}:{provider_model}:{normalized_message}"


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
    invalid_names = {"", "el", "la", "los", "las", "un", "una", "al", "del"}

    for participant in extraction.participants:
        if first_person and participant.name.strip().lower() in invalid_names:
            participant.name = "Yo"

    for removal in extraction.removals:
        if first_person and removal.participant_name.strip().lower() in invalid_names:
            removal.participant_name = "Yo"

    extraction = apply_exclusive_availability_overrides(extraction, original_message)
    return ground_extraction_to_message(extraction, original_message, first_person)


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


def find_exclusive_availability_overrides(
    original_message: str,
) -> list[tuple[str, list[TimeSlot], list[TimeSlot]]]:
    overrides: list[tuple[str, list[TimeSlot], list[TimeSlot]]] = []
    for fragment in re.split(r",|;|\n", original_message):
        if not is_exclusive_availability_fragment(fragment):
            continue

        participant_name = detect_name(fragment)
        if not participant_name:
            continue

        availability = detect_slots(fragment)
        if not availability:
            continue

        available_days = {slot.day for slot in availability}
        removal_slots = [
            TimeSlot(day=day, start="09:00", end="18:00")
            for day in WEEKDAYS
            if day not in available_days
        ]
        overrides.append((participant_name, availability, removal_slots))

    return overrides


def is_exclusive_availability_fragment(fragment: str) -> bool:
    normalized = normalize(fragment)
    return bool(
        re.search(r"\b(solo|solamente|unicamente)\b", normalized)
        and re.search(r"\b(puedo|puede|podre|podra|disponible|sirve|acomoda)\b", normalized)
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

    return extraction.model_copy(
        update={
            "participants": grounded_participants,
            "removals": grounded_removals,
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
            "me desocupo",
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
