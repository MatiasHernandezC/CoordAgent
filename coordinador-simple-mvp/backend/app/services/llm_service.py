import json
import re
import subprocess
import unicodedata
from urllib.error import HTTPError
import urllib.request

from app.prompts.extraction_prompt import EXTRACTION_PROMPT
from app.schemas import (
    ChannelMessage,
    ExtractedAvailability,
    Participant,
    TimeSlot,
    TokenUsage,
)
from app.settings import settings

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]

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

    def extract_availability(self, message: str) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        cache_key = build_cache_key(message)
        if settings.llm_cache_enabled and cache_key in self._cache:
            cached_extraction, cached_source, cached_tokens = self._cache[cache_key]

            return (
                cached_extraction.model_copy(deep=True),
                f"{cached_source}_cache",
                cached_tokens,
            )

        if settings.llm_provider == "local":
            try:
                return self._remember(cache_key, self._extract_with_local_model(message), f"local_{settings.local_llm_model}", None)
            except Exception:
                fallback = self._extract_with_mock_rules(message)
                return self._remember(cache_key, fallback, f"mock_fallback_local_{settings.local_llm_model}", None)

        if settings.llm_provider == "gemini":
            try:
                extraction, token_usage = self._extract_with_gemini(message)

                print(
                    f"[TOKENS] "
                    f"prompt={token_usage.prompt_tokens} "
                    f"completion={token_usage.completion_tokens} "
                    f"total={token_usage.total_tokens} "
                    f"cost=${token_usage.estimated_cost_usd}"
                )

                return self._remember(
                    cache_key,
                    extraction,
                    f"gemini_{settings.gemini_model}",
                    token_usage,
                )

            except Exception as error:
                print("\n========== GEMINI ERROR ==========")
                print(str(error))
                print("==================================\n")

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
            except Exception:
                fallback = self._extract_with_mock_rules(message)
                return self._remember(cache_key, fallback, "mock_fallback", None)

        return self._remember(cache_key, self._extract_with_mock_rules(message), "mock", None)

    def extract_channel_availability(
        self,
        messages: list[ChannelMessage]
    ) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        transcript = build_channel_extraction_text(messages)
        extraction, source, token_usage = self.extract_availability(transcript)
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
        return ExtractedAvailability.model_validate(parsed)

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
        return ExtractedAvailability.model_validate(parsed)

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

            raise RuntimeError(
                f"Gemini API error {error.code}: {detail}"
            ) from error

        content = extract_gemini_text(raw)

        parsed = json.loads(extract_json(content))

        extraction = ExtractedAvailability.model_validate(parsed)

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
    
    def _extract_with_mock_rules(self, message: str) -> ExtractedAvailability:
        participants: dict[str, Participant] = {}
        fragments = re.split(r",|;|\by\b|\bpero\b", message, flags=re.IGNORECASE)

        for fragment in fragments:
            name = detect_name(fragment)
            if not name:
                continue

            participant = participants.setdefault(name, Participant(name=name))
            participant.availability.extend(detect_slots(fragment))

        return ExtractedAvailability(participants=list(participants.values()))

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
    if re.search(r"\byo\b", normalized_fragment):
        return "Yo"

    match = re.search(r"\b([a-z][a-z]+)\s+(puede|puedo|tiene|esta)\b", normalized_fragment)
    if match:
        return match.group(1).capitalize()

    return None


def build_channel_extraction_text(messages: list[ChannelMessage]) -> str:
    fragments: list[str] = []
    for message in messages:
        if message.kind != "human":
            continue

        text = remove_invocation_tokens(message.text)
        fragments.append(rewrite_first_person_availability(message.sender, text))

    return ", ".join(fragment for fragment in fragments if fragment)


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

    if re.search(r"\byo\s+puedo\b", normalized):
        return re.sub(r"\byo\s+puedo\b", f"{name} puede", text, flags=re.IGNORECASE)

    if re.search(r"\bpuedo\b", normalized):
        return re.sub(r"\bpuedo\b", f"{name} puede", text, count=1, flags=re.IGNORECASE)

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

    if "cualquier hora" in normalized or "todo el dia" in normalized:
        start, end = "09:00", "18:00"
    elif "manana" in normalized:
        start, end = "09:00", "12:00"
    elif "tarde" in normalized:
        start, end = "15:00", "18:00"

    hour_match = re.search(r"(desde las|despues de las|a las)\s+(\d{1,2})", normalized)
    if hour_match:
        hour = int(hour_match.group(2))
        if hour < 8:
            hour += 12
        start = f"{hour:02d}:00"
        end = "18:00"

    return [TimeSlot(day=day, start=start, end=end) for day in days]


def detect_days(text: str) -> list[str]:
    if "todos los dias" in text or "todos los dias habiles" in text:
        return WEEKDAYS

    return [day for day in WEEKDAYS if day in text]


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
