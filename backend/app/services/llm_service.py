import json
import hashlib
import logging
import math
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
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
from app.services.credential_crypto import CredentialEncryptionError
from app.services.group_memory import (
    GroupMemoryContext,
    apply_memory_identity,
    memory_fingerprint,
    resolve_memory_prompt_block,
)
from app.services.llm_key_service import llm_key_service
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

_DAY_TOKEN_PATTERN = (
    r"(?:lunes|lun\.?|martes|mar\.?|miercoles|mierc\.?|mier\.?|mie\.?|"
    r"jueves|jue\.?|viernes|vier\.?|vie\.?)"
)
_CROSS_DAY_RANGE_PATTERN = re.compile(
    rf"\b(?:desde|del)\s+(?:el\s+)?(?P<start_day>{_DAY_TOKEN_PATTERN})\b"
    rf"(?P<start_body>.*?)\b(?:hasta|al)\s+(?:el\s+)?(?P<end_day>{_DAY_TOKEN_PATTERN})\b"
    r"(?P<end_body>[^,;\n]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CrossDayRange:
    start_day: str
    end_day: str
    start_hour: int
    end_hour: int
    week_offset: int
    fragment: str
    unavailable: bool = False


@dataclass(frozen=True)
class ChannelIdentityBinding:
    token: str
    display_name: str
    external_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedChannelMessage:
    message: ChannelMessage
    text: str
    sender: ChannelIdentityBinding
    mention: ChannelIdentityBinding | None = None
    ambiguous_mention: bool = False
    requires_mention: bool = False
    has_authorized_subject: bool = False


class GeminiApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str, retry_after_seconds: int | None = None) -> None:
        detail = redact_sensitive_text(detail)
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

    def extract_availability(
        self,
        message: str,
        workday_start: int = WORKDAY_START_HOUR,
        workday_end: int = WORKDAY_END_HOUR,
        group_memory: GroupMemoryContext | str | None = None,
        active_round_context: str | None = None,
        active_round_days: list[str] | None = None,
    ) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        validate_workday_window(workday_start, workday_end)
        memory_obj = group_memory if isinstance(group_memory, GroupMemoryContext) else None
        memory_fp = memory_fingerprint(group_memory)
        memory_block = resolve_memory_prompt_block(group_memory)
        round_context = active_round_context
        cache_key = build_cache_key(
            message,
            workday_start,
            workday_end,
            memory_fingerprint=memory_fp,
        )
        if settings.llm_cache_enabled and cache_key in self._cache:
            cached_extraction, cached_source, cached_tokens = self._cache[cache_key]
            cached_extraction = apply_memory_identity(
                cached_extraction.model_copy(deep=True),
                memory_obj,
            )
            return (
                cached_extraction,
                f"{cached_source}_cache",
                build_cached_token_usage(cached_tokens),
            )

        if settings.llm_provider == "local":
            try:
                return self._remember(
                    cache_key,
                    self._extract_with_local_model(
                        message,
                        workday_start,
                        workday_end,
                        group_memory=memory_block,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    f"local_{settings.local_llm_model}",
                    None,
                    group_memory=memory_obj,
                )
            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"El proveedor local fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error
                # No cachear fallbacks: el proximo intento debe reintentar el LLM real.
                fallback = apply_memory_identity(
                    self._extract_with_mock_rules(
                        message,
                        workday_start,
                        workday_end,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    memory_obj,
                )
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
                fallback = apply_memory_identity(
                    self._extract_with_mock_rules(
                        message,
                        workday_start,
                        workday_end,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    memory_obj,
                )
                source = f"mock_fallback_gemini_cooldown_{settings.gemini_model}"
                return fallback, source, None

            try:
                extraction, token_usage = self._extract_with_gemini_pool(
                    message,
                    workday_start,
                    workday_end,
                    group_memory=memory_block,
                    active_round_context=round_context,
                    active_round_days=active_round_days,
                )

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
                    group_memory=memory_obj,
                )

            except GeminiApiError as error:
                no_keys_configured = error.detail.startswith("No hay llaves Gemini")
                if error.status_code == 503 and not no_keys_configured:
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
                fallback = apply_memory_identity(
                    self._extract_with_mock_rules(
                        message,
                        workday_start,
                        workday_end,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    memory_obj,
                )
                source = (
                    f"mock_fallback_gemini_{settings.gemini_model}"
                    if no_keys_configured
                    else f"mock_fallback_gemini_{error.status_code}_{settings.gemini_model}"
                )
                return fallback, source, None

            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"Gemini fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error

                logger.warning("Gemini failed before request completion; using mock fallback. %s", error)

                fallback = apply_memory_identity(
                    self._extract_with_mock_rules(
                        message,
                        workday_start,
                        workday_end,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    memory_obj,
                )
                return fallback, f"mock_fallback_gemini_{settings.gemini_model}", None
        if settings.llm_provider == "ollama":
            try:
                return self._remember(
                    cache_key,
                    self._extract_with_ollama(
                        message,
                        workday_start,
                        workday_end,
                        group_memory=memory_block,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    "ollama",
                    None,
                    group_memory=memory_obj,
                )
            except Exception as error:
                if not settings.llm_fallback_enabled:
                    raise LlmUnavailableError(
                        f"Ollama fallo y el fallback esta desactivado: {error}",
                        status_code=503,
                    ) from error
                fallback = apply_memory_identity(
                    self._extract_with_mock_rules(
                        message,
                        workday_start,
                        workday_end,
                        active_round_context=round_context,
                        active_round_days=active_round_days,
                    ),
                    memory_obj,
                )
                return fallback, "mock_fallback", None

        extraction = self._extract_with_mock_rules(
            message,
            workday_start,
            workday_end,
            active_round_context=round_context,
            active_round_days=active_round_days,
        )
        return self._remember(cache_key, extraction, "mock", None, group_memory=memory_obj)

    def extract_channel_availability(
        self,
        messages: list[ChannelMessage],
        workday_start: int = WORKDAY_START_HOUR,
        workday_end: int = WORKDAY_END_HOUR,
        group_memory: GroupMemoryContext | str | None = None,
        active_round_context: str | None = None,
    ) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        transcript = build_channel_extraction_text(messages)
        # Conversation context may fix the active coordination day ("para el jueves").
        round_context = active_round_context or transcript
        if not has_extractable_scheduling_signal(transcript):
            guarded = normalize_channel_extraction(ExtractedAvailability(), messages)
            source = "channel_identity_guard" if guarded.quality_flags else "channel_no_new_availability"
            return guarded, source, None

        extraction, source, token_usage = self.extract_availability(
            transcript,
            workday_start,
            workday_end,
            group_memory=group_memory,
            active_round_context=round_context,
        )
        extraction = normalize_channel_extraction(extraction, messages)
        return extraction, f"channel_{source}", token_usage

    def _extract_with_ollama(
        self,
        message: str,
        workday_start: int,
        workday_end: int,
        group_memory: str | None = None,
        active_round_context: str | None = None,
        active_round_days: list[str] | None = None,
    ) -> ExtractedAvailability:
        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": EXTRACTION_PROMPT,
                },
                {
                    "role": "user",
                    "content": build_extraction_prompt(
                        message,
                        workday_start=workday_start,
                        workday_end=workday_end,
                        group_memory=group_memory,
                    ),
                },
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
        extraction = parse_extraction_payload(
            parsed,
            message,
            workday_start,
            workday_end,
            active_round_context=active_round_context,
            active_round_days=active_round_days,
        )
        return merge_missing_participants(
            extraction,
            self._extract_with_mock_rules(
                message,
                workday_start,
                workday_end,
                active_round_context=active_round_context,
                active_round_days=active_round_days,
            ),
        )

    def _extract_with_local_model(
        self,
        message: str,
        workday_start: int,
        workday_end: int,
        group_memory: str | None = None,
        active_round_context: str | None = None,
        active_round_days: list[str] | None = None,
    ) -> ExtractedAvailability:
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
            build_extraction_prompt(
                message,
                workday_start=workday_start,
                workday_end=workday_end,
                group_memory=group_memory,
            ),
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
        extraction = parse_extraction_payload(
            parsed,
            message,
            workday_start,
            workday_end,
            active_round_context=active_round_context,
            active_round_days=active_round_days,
        )
        return merge_missing_participants(
            extraction,
            self._extract_with_mock_rules(
                message,
                workday_start,
                workday_end,
                active_round_context=active_round_context,
                active_round_days=active_round_days,
            ),
        )

    def _extract_with_gemini(
        self,
        message: str,
        workday_start: int,
        workday_end: int,
        api_key: str | None = None,
        group_memory: str | None = None,
        active_round_context: str | None = None,
        active_round_days: list[str] | None = None,
    ) -> tuple[ExtractedAvailability, TokenUsage]:
        selected_api_key = (api_key or settings.gemini_api_key).strip()
        if not selected_api_key:
            raise ValueError("Falta GEMINI_API_KEY")

        prompt_text = build_extraction_prompt(
            message,
            workday_start=workday_start,
            workday_end=workday_end,
            group_memory=group_memory,
        )
        raw, time_to_first_token_ms, used_model = self._call_gemini_with_model_fallback(
            prompt_text,
            selected_api_key,
        )
        content = extract_gemini_text(raw)
        parse_retries = 0
        try:
            parsed = json.loads(extract_json(content))
            extraction = parse_extraction_payload(
                parsed,
                message,
                workday_start,
                workday_end,
                active_round_context=active_round_context,
                active_round_days=active_round_days,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as first_error:
            parse_retries = 1
            logger.warning(
                "gemini_json_parse_error provider=gemini retry=1 error_type=%s model=%s",
                type(first_error).__name__,
                used_model,
            )
            repair_prompt = (
                prompt_text
                + "\n\nTu respuesta anterior no fue JSON valido. "
                "Responde SOLO con el objeto JSON pedido, sin markdown ni texto extra."
            )
            raw, time_to_first_token_ms, used_model = self._call_gemini_with_model_fallback(
                repair_prompt,
                selected_api_key,
            )
            content = extract_gemini_text(raw)
            try:
                parsed = json.loads(extract_json(content))
                extraction = parse_extraction_payload(
                    parsed,
                    message,
                    workday_start,
                    workday_end,
                    active_round_context=active_round_context,
                    active_round_days=active_round_days,
                )
                logger.info(
                    "gemini_json_parse_recovered provider=gemini retries=%s model=%s",
                    parse_retries,
                    used_model,
                )
            except (json.JSONDecodeError, ValueError, TypeError) as second_error:
                logger.warning(
                    "gemini_json_parse_failed provider=gemini retries=%s error_type=%s fallback=rules",
                    parse_retries,
                    type(second_error).__name__,
                )
                raise ValueError(
                    f"Gemini JSON invalido tras reintento: {type(second_error).__name__}"
                ) from second_error

        extraction = merge_missing_participants(
            extraction,
            self._extract_with_mock_rules(
                message,
                workday_start,
                workday_end,
                active_round_context=active_round_context,
                active_round_days=active_round_days,
            ),
        )

        usage = raw.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        completion_tokens = usage.get("candidatesTokenCount", 0)
        total_tokens = usage.get("totalTokenCount", 0)
        token_usage = TokenUsage(
            provider=f"gemini:{used_model}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimate_gemini_cost(
                prompt_tokens,
                completion_tokens,
            ),
            time_to_first_token_ms=time_to_first_token_ms,
        )
        return extraction, token_usage

    def _call_gemini_with_model_fallback(
        self,
        prompt_text: str,
        api_key: str,
    ) -> tuple[dict, int | None, str]:
        """Call Gemini trying primary model then optional fallback model."""
        models = gemini_models_for_request()
        last_error: GeminiApiError | None = None
        for index, model in enumerate(models):
            try:
                raw, ttft = self._call_gemini_raw(prompt_text, api_key, model=model)
                if index > 0:
                    logger.info(
                        "gemini_model_fallback_used primary=%s fallback=%s",
                        models[0],
                        model,
                    )
                return raw, ttft, model
            except GeminiApiError as error:
                last_error = error
                if is_incompatible_gemini_model_error(error) and index < len(models) - 1:
                    logger.warning(
                        "gemini_model_unavailable model=%s status=%s trying_next_model",
                        model,
                        error.status_code,
                    )
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise GeminiApiError(503, "No hay modelos Gemini configurados.")

    def _call_gemini_raw(
        self,
        prompt_text: str,
        api_key: str,
        model: str | None = None,
    ) -> tuple[dict, int | None]:
        model_name = (model or settings.gemini_model).strip()
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt_text}],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        base_url = settings.gemini_url.format(model=model_name)
        url = build_gemini_stream_url(base_url) if settings.gemini_streaming_enabled else base_url
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        request_started = time.perf_counter()
        try:
            with urllib.request.urlopen(
                request,
                timeout=settings.gemini_timeout_seconds,
            ) as response:
                if settings.gemini_streaming_enabled:
                    return read_gemini_sse(response, request_started)
                raw = json.loads(response.read().decode("utf-8"))
                return raw, None
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise GeminiApiError(
                error.code,
                detail,
                parse_retry_delay_seconds(detail),
            ) from error

    def _extract_with_gemini_pool(
        self,
        message: str,
        workday_start: int,
        workday_end: int,
        group_memory: str | None = None,
        active_round_context: str | None = None,
        active_round_days: list[str] | None = None,
    ) -> tuple[ExtractedAvailability, TokenUsage]:
        candidates = llm_key_service.candidate_records()
        if not candidates:
            retry_after = llm_key_service.next_retry_seconds()
            raise GeminiApiError(
                429 if retry_after else 503,
                "No hay llaves Gemini disponibles en este momento.",
                retry_after,
            )

        attempted_ids: set[str] = set()
        last_quota_error: GeminiApiError | None = None
        for credential in candidates:
            credential_id = credential["id"]
            if credential_id in attempted_ids:
                continue
            attempted_ids.add(credential_id)
            try:
                api_key = llm_key_service.reveal_secret(credential)
                result = self._extract_with_gemini(
                    message,
                    workday_start,
                    workday_end,
                    api_key=api_key,
                    group_memory=group_memory,
                    active_round_context=active_round_context,
                    active_round_days=active_round_days,
                )
                llm_key_service.mark_success(credential_id, result[1])
                logger.info(
                    "Gemini request succeeded with credential_id=%s ttft_ms=%s",
                    credential_id,
                    result[1].time_to_first_token_ms,
                )
                return result
            except CredentialEncryptionError:
                logger.exception("Gemini credential could not be decrypted. credential_id=%s", credential_id)
                llm_key_service.mark_invalid(credential_id, 500)
                continue
            except GeminiApiError as error:
                if error.status_code == 429:
                    cooldown = gemini_quota_cooldown_seconds(error, credential)
                    llm_key_service.mark_quota(credential_id, cooldown)
                    last_quota_error = error
                    logger.warning(
                        "Gemini quota exhausted; rotating credential_id=%s cooldown=%ss",
                        credential_id,
                        cooldown,
                    )
                    continue
                if is_invalid_gemini_key_error(error):
                    llm_key_service.mark_invalid(credential_id, error.status_code)
                    logger.warning("Gemini credential rejected. credential_id=%s", credential_id)
                    continue
                if is_incompatible_gemini_model_error(error):
                    llm_key_service.mark_incompatible(credential_id, error.status_code)
                    logger.warning(
                        "Gemini model unavailable for credential project; rotating credential_id=%s",
                        credential_id,
                    )
                    continue
                if error.status_code == 503:
                    llm_key_service.mark_transient_error(credential_id, error.status_code)
                raise

        retry_after = llm_key_service.next_retry_seconds()
        if last_quota_error is not None:
            raise GeminiApiError(
                429,
                "Todas las llaves Gemini disponibles alcanzaron su limite.",
                retry_after or last_quota_error.retry_after_seconds,
            ) from last_quota_error
        raise GeminiApiError(503, "No fue posible usar ninguna llave Gemini configurada.")

    def test_gemini_credential(self, credential_id: str, actor: str) -> dict:
        record = llm_key_service.get_record(credential_id)
        try:
            api_key = llm_key_service.reveal_secret(record)
            used_model = self._probe_gemini_key(api_key)
            llm_key_service.mark_success(credential_id)
            result = "ok" if used_model == settings.gemini_model else f"ok_fallback:{used_model}"
            llm_key_service.audit_test(record, actor, result)
            public = llm_key_service.public_record(credential_id)
            return {"ok": True, "key": public, "model_used": used_model}
        except CredentialEncryptionError:
            llm_key_service.mark_invalid(credential_id, 500)
            llm_key_service.audit_test(record, actor, "decrypt_error")
            return {"ok": False, "key": llm_key_service.public_record(credential_id)}
        except GeminiApiError as error:
            if error.status_code == 429:
                llm_key_service.mark_quota(
                    credential_id,
                    gemini_quota_cooldown_seconds(error, record),
                )
                result = "quota"
            elif is_invalid_gemini_key_error(error):
                llm_key_service.mark_invalid(credential_id, error.status_code)
                result = "invalid"
            elif is_incompatible_gemini_model_error(error):
                llm_key_service.mark_incompatible(credential_id, error.status_code)
                result = "incompatible"
            else:
                llm_key_service.mark_transient_error(credential_id, error.status_code)
                result = f"error_{error.status_code}"
            llm_key_service.audit_test(record, actor, result)
            return {"ok": False, "key": llm_key_service.public_record(credential_id)}

    def _probe_gemini_key(self, api_key: str) -> str:
        """Return the model name that accepted a minimal generateContent probe."""
        last_error: GeminiApiError | None = None
        for index, model in enumerate(gemini_models_for_request()):
            try:
                self._probe_gemini_key_on_model(api_key, model)
                if index > 0:
                    logger.info(
                        "gemini_probe_fallback_used primary=%s fallback=%s",
                        settings.gemini_model,
                        model,
                    )
                return model
            except GeminiApiError as error:
                last_error = error
                if is_incompatible_gemini_model_error(error) and index < len(gemini_models_for_request()) - 1:
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise GeminiApiError(503, "No hay modelos Gemini configurados.")

    def _probe_gemini_key_on_model(self, api_key: str, model: str) -> None:
        model_url = settings.gemini_url.format(model=model)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "Responde unicamente con OK."}],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 16,
            },
        }
        request = urllib.request.Request(
            model_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.gemini_timeout_seconds) as response:
                if response.status >= 400:
                    raise GeminiApiError(response.status, "No fue posible validar la llave Gemini.")
                try:
                    raw = json.loads(response.read().decode("utf-8"))
                    generated_text = extract_gemini_text(raw).strip()
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise GeminiApiError(502, "Gemini devolvio una respuesta de validacion invalida.") from error
                if not generated_text:
                    raise GeminiApiError(502, "Gemini no genero contenido durante la validacion.")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise GeminiApiError(
                error.code,
                detail,
                parse_retry_delay_seconds(detail),
            ) from error

    def _is_gemini_in_cooldown(self) -> bool:
        return time.monotonic() < self._gemini_blocked_until

    def _gemini_retry_after_seconds(self) -> int:
        return max(1, math.ceil(self._gemini_blocked_until - time.monotonic()))

    def _block_gemini(self, seconds: int, reason: str) -> None:
        self._gemini_blocked_until = time.monotonic() + max(seconds, 1)
        self._gemini_block_reason = reason
        logger.warning("Gemini provider paused for %s seconds. %s", seconds, reason)

    def _extract_with_mock_rules(
        self,
        message: str,
        workday_start: int = WORKDAY_START_HOUR,
        workday_end: int = WORKDAY_END_HOUR,
        active_round_context: str | None = None,
        active_round_days: list[str] | None = None,
    ) -> ExtractedAvailability:
        participants: dict[str, Participant] = {}
        removals: list[AvailabilityRemoval] = []
        fragments = split_scheduling_fragments(message)

        for fragment in fragments:
            names = detect_participant_names(fragment)
            if not names:
                continue

            slots = detect_slots(fragment, workday_start, workday_end)
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
            implied=build_implied_from_removals(removals, workday_start, workday_end),
            replacements=[
                name
                for fragment in fragments
                if is_replacement_availability_fragment(fragment)
                for name in detect_participant_names(fragment)
            ],
        )
        extraction = apply_exclusive_availability_overrides(extraction, message, workday_start, workday_end)
        extraction = apply_cross_day_availability_overrides(extraction, message, workday_start, workday_end)
        return discard_ungrounded_days(
            extraction,
            message,
            active_round_context=active_round_context,
            active_round_days=active_round_days,
        )

    def _remember(
        self,
        cache_key: str,
        extraction: ExtractedAvailability,
        source: str,
        token_usage: TokenUsage | None = None,
        group_memory: GroupMemoryContext | None = None,
    ) -> tuple[ExtractedAvailability, str, TokenUsage | None]:
        extraction = apply_memory_identity(extraction, group_memory)
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


def build_implied_from_removals(
    removals: list[AvailabilityRemoval],
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> list[ImpliedAvailability]:
    """Pragmatica de no-disponibilidad parcial: "no puedo despues de las 16" implica
    poder antes. El merge solo la aplica si la persona no tiene nada ese dia."""
    implied: list[ImpliedAvailability] = []
    for removal in removals:
        complement: list[TimeSlot] = []
        for slot in removal.slots:
            start_hour = int(slot.start[:2])
            end_hour = int(slot.end[:2])
            if start_hour > workday_start:
                complement.append(
                    TimeSlot(day=slot.day, start=f"{workday_start:02d}:00", end=slot.start, week_offset=slot.week_offset)
                )
            if end_hour < workday_end:
                complement.append(
                    TimeSlot(day=slot.day, start=slot.end, end=f"{workday_end:02d}:00", week_offset=slot.week_offset)
                )
        if complement:
            implied.append(
                ImpliedAvailability(participant_name=removal.participant_name, slots=complement)
            )
    return implied


def parse_extraction_payload(
    parsed: dict,
    message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
    active_round_context: str | None = None,
    active_round_days: list[str] | None = None,
) -> ExtractedAvailability:
    """Parsea la salida del LLM: esquema semantico nuevo (entries) o legacy
    (participants/removals) como red de seguridad si el modelo no siguio el formato."""
    if isinstance(parsed, dict) and "entries" in parsed:
        interpretation = ScheduleInterpretation.model_validate(parsed)
        extraction = compile_interpretation(interpretation, workday_start, workday_end)
        return normalize_llm_extraction(
            extraction,
            message,
            workday_start,
            workday_end,
            active_round_context=active_round_context,
            active_round_days=active_round_days,
        )

    extraction = ExtractedAvailability.model_validate(parsed)
    return normalize_llm_extraction(
        extraction,
        message,
        workday_start,
        workday_end,
        active_round_context=active_round_context,
        active_round_days=active_round_days,
    )


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
    replacements = list(primary.replacements)

    for participant in secondary.participants:
        if participant.name.strip().lower() not in known:
            participants.append(participant.model_copy(deep=True))

    for removal in secondary.removals:
        if removal.participant_name.strip().lower() not in known:
            removals.append(removal.model_copy(deep=True))

    for item in secondary.implied:
        if item.participant_name.strip().lower() not in known:
            implied.append(item.model_copy(deep=True))

    for name in secondary.replacements:
        if name.strip().lower() not in known and name not in replacements:
            replacements.append(name)

    return primary.model_copy(
        update={
            "participants": participants,
            "removals": removals,
            "implied": implied,
            "replacements": replacements,
        }
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
    for prepared in prepare_channel_messages(messages):
        if prepared.requires_mention:
            # Si un mensaje mezcla la propia disponibilidad con la de un
            # tercero sin mención, se descarta completo: no es posible atribuir
            # con seguridad qué horas pertenecen a cada sujeto después del LLM.
            continue
        fragment = rewrite_first_person_availability(prepared.sender.token, prepared.text)
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
        fragments.append(f"- {prepared.sender.token}: {fragment}")

    return "\n".join(fragment for fragment in fragments if fragment)


def normalize_channel_extraction(
    extraction: ExtractedAvailability,
    messages: list[ChannelMessage],
) -> ExtractedAvailability:
    prepared_messages = prepare_channel_messages(messages)
    bindings: dict[str, ChannelIdentityBinding] = {}
    self_report_tokens: set[str] = set()
    ambiguous_mention = False
    requires_mention = False

    for prepared in prepared_messages:
        self_rewritten = rewrite_first_person_availability(prepared.sender.token, prepared.text)
        is_self_report = (
            normalize(self_rewritten) != normalize(prepared.text)
            and has_extractable_scheduling_signal(self_rewritten)
        )
        if is_self_report:
            bindings[normalize(prepared.sender.token)] = prepared.sender
            self_report_tokens.add(prepared.sender.token)

        if prepared.mention and has_extractable_scheduling_signal(prepared.text):
            bindings[normalize(prepared.mention.token)] = prepared.mention
        ambiguous_mention = ambiguous_mention or prepared.ambiguous_mention
        requires_mention = requires_mention or prepared.requires_mention

    if len(self_report_tokens) == 1:
        sender = next(iter(self_report_tokens))
        for participant in extraction.participants:
            if participant.name.strip().lower() == "yo":
                participant.name = sender

        for removal in extraction.removals:
            if removal.participant_name.strip().lower() == "yo":
                removal.participant_name = sender

        for item in extraction.implied:
            if item.participant_name.strip().lower() == "yo":
                item.participant_name = sender

        extraction.replacements = [
            sender if name.strip().lower() == "yo" else name
            for name in extraction.replacements
        ]

    rejected = False
    participants: list[Participant] = []
    for participant in extraction.participants:
        binding = bindings.get(normalize(participant.name))
        if not binding:
            rejected = True
            continue
        participant.name = binding.display_name
        participant.external_ids = list(binding.external_ids)
        participant.external_id = canonical_channel_identity(binding.external_ids)
        participants.append(participant)

    removals: list[AvailabilityRemoval] = []
    for removal in extraction.removals:
        binding = bindings.get(normalize(removal.participant_name))
        if not binding:
            rejected = True
            continue
        removal.participant_name = binding.display_name
        removal.external_ids = list(binding.external_ids)
        removal.external_id = canonical_channel_identity(binding.external_ids)
        removals.append(removal)

    implied: list[ImpliedAvailability] = []
    for item in extraction.implied:
        binding = bindings.get(normalize(item.participant_name))
        if not binding:
            rejected = True
            continue
        item.participant_name = binding.display_name
        item.external_ids = list(binding.external_ids)
        item.external_id = canonical_channel_identity(binding.external_ids)
        implied.append(item)

    replacements: list[str] = []
    for name in extraction.replacements:
        binding = bindings.get(normalize(name))
        if not binding:
            rejected = True
            continue
        replacements.append(binding.display_name)

    quality_flags = list(extraction.quality_flags)
    if (rejected or requires_mention) and "third_party_requires_mention" not in quality_flags:
        quality_flags.append("third_party_requires_mention")
    if ambiguous_mention and "ambiguous_mentioned_identity" not in quality_flags:
        quality_flags.append("ambiguous_mentioned_identity")

    return extraction.model_copy(
        update={
            "participants": participants,
            "removals": removals,
            "implied": implied,
            "replacements": replacements,
            "quality_flags": quality_flags,
        }
    )


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
    # La mención del bot no aporta disponibilidad. Las demás arrobas se
    # conservan porque solo contextInfo.mentionedJid puede validarlas como una
    # persona real en prepare_channel_messages.
    return re.sub(r"(?<![\w@])@coordina(?![\w])", "", text, flags=re.IGNORECASE).strip()


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


def build_extraction_prompt(
    message: str,
    now: datetime | None = None,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
    group_memory: GroupMemoryContext | str | None = None,
) -> str:
    memory_block = resolve_memory_prompt_block(group_memory)
    memory_section = f"{memory_block}\n\n" if memory_block else ""
    return (
        f"{EXTRACTION_PROMPT.strip()}\n\n"
        "Ventana horaria configurada para ESTA coordinacion:\n"
        f"- Inicio del dia coordinable: {workday_start:02d}:00.\n"
        f"- Fin del dia coordinable: {workday_end:02d}:00.\n"
        "- start/end null significan estos limites configurados.\n"
        "- Nunca inventes disponibilidad fuera de esta ventana.\n\n"
        f"{build_temporal_context(now)}\n\n"
        f"{memory_section}"
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


def detect_slots(
    fragment: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> list[TimeSlot]:
    normalized = normalize(fragment)
    days = detect_days(normalized)
    if not days:
        # Time-only fragment ("puedo a las 7"): anchor to today's workday in Python.
        from app.services.temporal_grounding import (
            _current_or_next_workday,
            _message_has_time_anchor,
        )

        if _message_has_time_anchor(normalized):
            default_day = _current_or_next_workday()
            if default_day:
                days = [default_day]
        if not days:
            return []

    range_hours = detect_time_range(normalized)
    if range_hours:
        start_hour, end_hour = range_hours
        return slots_in_workday(days, start_hour, end_hour, workday_start, workday_end)

    start_hour = workday_start
    end_hour = workday_end

    if is_unavailability_fragment(fragment):
        until_match = re.search(r"(hasta las|hasta la|hasta al menos las|hasta al menos la|al menos hasta las|al menos hasta la)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?", normalized)
        if until_match:
            hour = normalize_hour_for_context(int(until_match.group(2)), normalized, prefer_evening=True)
            return slots_in_workday(days, workday_start, hour, workday_start, workday_end)

        release_match = re.search(r"(me\s+desocupo|se\s+desocupa|desocupo|desocupa)\s+(a\s+(?:la|las)|recien\s+a\s+(?:la|las))?\s*(\d{1,2})(?::\d{2})?\s*(am|pm)?", normalized)
        if release_match:
            hour = normalize_hour_for_context(int(release_match.group(3)), normalized, prefer_evening=True)
            return slots_in_workday(days, workday_start, hour, workday_start, workday_end)

    if "cualquier hora" in normalized or "todo el dia" in normalized:
        start_hour, end_hour = workday_start, workday_end
    elif "manana" in normalized:
        start_hour, end_hour = workday_start, min(12, workday_end)
    elif "tarde" in normalized or "atrde" in normalized:
        start_hour, end_hour = max(15, workday_start), workday_end

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
        return slots_in_workday(days, start_hour, hour, workday_start, workday_end)

    hour_match = re.search(r"(desde las|desde la|despues de las|despues de la|despues las|despues la|pasado las|pasado la|tipo las|tipo la|como a las|como a la)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?", normalized)
    if hour_match:
        hour = normalize_hour_for_context(int(hour_match.group(2)), normalized)
        start_hour = hour
        end_hour = workday_end

    exact_hour_match = re.search(r"\ba\s+(?:la|las|los)\s+(\d{1,2})(?::\d{2})?\s*(am|pm)?\b", normalized)
    if exact_hour_match:
        hour = normalize_hour_for_context(int(exact_hour_match.group(1)), normalized)
        start_hour = hour
        end_hour = hour + 1

    # If the user named an evening hour past the configured window (common in
    # WhatsApp: "a las 7" → 19:00 with workday ending 18:00), extend the window
    # just enough for this fragment so the slot is not silently dropped.
    effective_end = workday_end
    if end_hour > workday_end:
        effective_end = min(max(end_hour, workday_end), 23)
    if start_hour >= workday_end and start_hour < 23:
        effective_end = max(effective_end, min(start_hour + 1, 23))

    return slots_in_workday(days, start_hour, end_hour, workday_start, effective_end)


def slots_in_workday(
    days: list[str],
    start_hour: int,
    end_hour: int,
    workday_start: int,
    workday_end: int,
    week_offset: int = 0,
) -> list[TimeSlot]:
    start = max(start_hour, workday_start)
    end = min(end_hour, workday_end)
    if end <= start:
        return []
    return [
        TimeSlot(
            day=day,  # type: ignore[arg-type]
            start=f"{start:02d}:00",
            end=f"{end:02d}:00",
            week_offset=week_offset,
        )
        for day in days
    ]


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


def find_cross_day_ranges(
    message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> list[CrossDayRange]:
    """Reconoce disponibilidades continuas entre dos dias habiles.

    El LLM puede representar el limite inicial y final como si pertenecieran al
    mismo dia. Esta capa conserva el significado temporal del texto y lo expande
    a bloques diarios dentro de la jornada 09:00-18:00.
    """
    ranges: list[CrossDayRange] = []
    for fragment in split_scheduling_fragments(message):
        normalized = normalize(fragment)
        for match in _CROSS_DAY_RANGE_PATTERN.finditer(normalized):
            start_day = _canonical_day_token(match.group("start_day"))
            end_day = _canonical_day_token(match.group("end_day"))
            if not start_day or not end_day:
                continue

            start_index = WEEKDAYS.index(start_day)
            end_index = WEEKDAYS.index(end_day)
            if end_index <= start_index:
                continue

            start_hour = _cross_day_boundary_hour(
                match.group("start_body"), is_end=False, workday_start=workday_start, workday_end=workday_end
            )
            end_hour = _cross_day_boundary_hour(
                match.group("end_body"), is_end=True, workday_start=workday_start, workday_end=workday_end
            )
            if start_hour is None or end_hour is None:
                continue

            ranges.append(
                CrossDayRange(
                    start_day=start_day,
                    end_day=end_day,
                    start_hour=start_hour,
                    end_hour=end_hour,
                    week_offset=detect_week_offset(fragment),
                    fragment=fragment,
                    unavailable=is_unavailability_fragment(fragment),
                )
            )
    return ranges


def apply_cross_day_availability_overrides(
    extraction: ExtractedAvailability,
    original_message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> ExtractedAvailability:
    ranges = find_cross_day_ranges(original_message, workday_start, workday_end)
    flags = list(extraction.quality_flags)
    participants = [participant.model_copy(deep=True) for participant in extraction.participants]
    removals = [removal.model_copy(deep=True) for removal in extraction.removals]
    implied = [item.model_copy(deep=True) for item in extraction.implied]
    applied = False

    for day_range in ranges:
        names = detect_participant_names(day_range.fragment)
        if not names:
            continue

        slots = _slots_for_cross_day_range(day_range, workday_start, workday_end)
        if not slots:
            continue

        covered_days = {slot.day for slot in slots}
        for name in names:
            if day_range.unavailable:
                # Una negacion entre dias nunca debe convertirse en disponibilidad
                # positiva aunque el proveedor haya interpretado mal sus extremos.
                participant = next(
                    (item for item in participants if normalize(item.name) == normalize(name)),
                    None,
                )
                if participant is not None:
                    participant.availability = [
                        slot
                        for slot in participant.availability
                        if slot.week_offset != day_range.week_offset or slot.day not in covered_days
                    ]

                removal = next(
                    (item for item in removals if normalize(item.participant_name) == normalize(name)),
                    None,
                )
                if removal is None:
                    removal = AvailabilityRemoval(participant_name=name)
                    removals.append(removal)
                kept_removals = [
                    slot
                    for slot in removal.slots
                    if slot.week_offset != day_range.week_offset or slot.day not in covered_days
                ]
                removal.slots = _deduplicate_slots([*kept_removals, *slots])

                complement: list[TimeSlot] = []
                for slot in slots:
                    start_hour = int(slot.start[:2])
                    end_hour = int(slot.end[:2])
                    if start_hour > workday_start:
                        complement.extend(
                            slots_in_workday(
                                [slot.day], workday_start, start_hour, workday_start, workday_end, slot.week_offset
                            )
                        )
                    if end_hour < workday_end:
                        complement.extend(
                            slots_in_workday(
                                [slot.day], end_hour, workday_end, workday_start, workday_end, slot.week_offset
                            )
                        )
                if complement:
                    implied_item = next(
                        (item for item in implied if normalize(item.participant_name) == normalize(name)),
                        None,
                    )
                    if implied_item is None:
                        implied_item = ImpliedAvailability(participant_name=name)
                        implied.append(implied_item)
                    implied_item.slots = _deduplicate_slots([*implied_item.slots, *complement])
                applied = True
                continue

            participant = next(
                (item for item in participants if normalize(item.name) == normalize(name)),
                None,
            )
            if participant is None:
                participant = Participant(name=name)
                participants.append(participant)

            # El rango es mas especifico que cualquier interpretacion del LLM
            # para esos dias: reemplaza sus tramos, no los une con limites malos.
            kept = [
                slot
                for slot in participant.availability
                if slot.week_offset != day_range.week_offset or slot.day not in covered_days
            ]
            participant.availability = _deduplicate_slots([*kept, *slots])
            applied = True

    if applied and "cross_day_range_normalized" not in flags:
        flags.append("cross_day_range_normalized")
    elif not applied and _CROSS_DAY_RANGE_PATTERN.search(normalize(original_message)):
        if "ambiguous_cross_day_range" not in flags:
            flags.append("ambiguous_cross_day_range")

    return extraction.model_copy(
        update={
            "participants": participants,
            "removals": removals,
            "implied": implied,
            "quality_flags": flags,
        }
    )


def _canonical_day_token(token: str) -> str | None:
    normalized = normalize(token).strip().rstrip(".")
    for day, aliases in DAY_ALIASES.items():
        if normalized in {alias.rstrip(".") for alias in aliases}:
            return day
    return None


def _cross_day_boundary_hour(
    text: str,
    is_end: bool,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> int | None:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if not match:
        return workday_end if is_end else workday_start

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None

    normalized = normalize(text)
    suffix = match.group(3)
    if suffix == "pm" or "tarde" in normalized or "noche" in normalized:
        if hour < 12:
            hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif hour < 8:
        hour += 12

    if is_end and minute:
        hour += 1
    return max(workday_start, min(hour, workday_end))


def _days_in_cross_day_range(day_range: CrossDayRange) -> list[str]:
    start = WEEKDAYS.index(day_range.start_day)
    end = WEEKDAYS.index(day_range.end_day)
    return WEEKDAYS[start : end + 1]


def _slots_for_cross_day_range(
    day_range: CrossDayRange,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> list[TimeSlot]:
    days = _days_in_cross_day_range(day_range)
    if not days or day_range.start_hour >= workday_end or day_range.end_hour <= workday_start:
        return []

    slots: list[TimeSlot] = []
    for index, day in enumerate(days):
        start = day_range.start_hour if index == 0 else workday_start
        end = day_range.end_hour if index == len(days) - 1 else workday_end
        if end <= start:
            continue
        slots.append(
            TimeSlot(
                day=day,  # type: ignore[arg-type]
                start=f"{start:02d}:00",
                end=f"{end:02d}:00",
                week_offset=day_range.week_offset,
            )
        )
    return slots


def _deduplicate_slots(slots: list[TimeSlot]) -> list[TimeSlot]:
    result: list[TimeSlot] = []
    seen: set[tuple[int, str, str, str]] = set()
    for slot in slots:
        key = (slot.week_offset, slot.day, slot.start, slot.end)
        if key not in seen:
            seen.add(key)
            result.append(slot)
    return result


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


def build_cache_key(
    message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
    memory_fingerprint: str = "none",
) -> str:
    provider_model = {
        "local": settings.local_llm_model,
        "gemini": settings.gemini_model,
        "ollama": settings.ollama_model,
    }.get(settings.llm_provider, "mock")
    normalized_message = " ".join(message.strip().lower().split())
    # La fecha entra en la clave: "manana" cambia de dia real cada dia, cachear
    # sin fecha reutilizaria una interpretacion relativa desactualizada.
    today = _timezone_now().strftime("%Y-%m-%d")
    fingerprint = (memory_fingerprint or "none").strip() or "none"
    return (
        f"{settings.llm_provider}:{provider_model}:{today}:"
        f"{workday_start:02d}-{workday_end:02d}:{fingerprint}:{normalized_message}"
    )


def validate_workday_window(workday_start: int, workday_end: int) -> None:
    if not 0 <= workday_start < workday_end <= 23:
        raise ValueError("Ventana horaria invalida")


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


def normalize_llm_extraction(
    extraction: ExtractedAvailability,
    original_message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
    active_round_context: str | None = None,
    active_round_days: list[str] | None = None,
) -> ExtractedAvailability:
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

    extraction.replacements = [normalize_person_name(name) for name in extraction.replacements]

    extraction = discard_invalid_names(extraction, invalid_names)
    extraction = apply_grounded_availability_overrides(extraction, original_message, workday_start, workday_end)
    extraction = apply_exclusive_availability_overrides(extraction, original_message, workday_start, workday_end)
    extraction = apply_cross_day_availability_overrides(extraction, original_message, workday_start, workday_end)
    extraction = discard_ungrounded_days(
        extraction,
        original_message,
        active_round_context=active_round_context,
        active_round_days=active_round_days,
    )
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
    replacements = [name for name in extraction.replacements if name.strip().lower() not in invalid_names]
    return extraction.model_copy(
        update={
            "participants": participants,
            "removals": removals,
            "implied": implied,
            "replacements": replacements,
        }
    )


def discard_ungrounded_days(
    extraction: ExtractedAvailability,
    original_message: str,
    active_round_context: str | None = None,
    active_round_days: list[str] | None = None,
) -> ExtractedAvailability:
    """Reject day-bound slots that are not grounded in message or active round.

    Historical RAG decisions are never used as temporal grounding.
    """
    from app.services.temporal_grounding import validate_extracted_temporal_grounding

    result = validate_extracted_temporal_grounding(
        extraction,
        original_message,
        active_round_context=active_round_context,
        active_round_days=active_round_days,
    )
    if result.rejected_days:
        logger.info(
            "temporal_grounding source=%s rejected_days=%s allowed_days=%s flags=%s",
            result.grounding_source,
            result.rejected_days,
            sorted(result.allowed_days),
            result.flags,
        )
    return result.extraction


_CHANNEL_MENTION_PATTERN = re.compile(r"(?<![\w@])@([^\s,;:!?()\[\]{}]+)", re.UNICODE)


def channel_identity_values(values: list[str | None] | tuple[str, ...]) -> list[str]:
    identities: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip().lower() if value else ""
        if cleaned and cleaned not in seen:
            identities.append(cleaned)
            seen.add(cleaned)
    return identities


def canonical_channel_identity(values: list[str] | tuple[str, ...]) -> str | None:
    identities = channel_identity_values(tuple(values))
    if not identities:
        return None
    return next((value for value in identities if value.endswith("@s.whatsapp.net")), identities[0])


def channel_sender_aliases(messages: list[ChannelMessage]) -> dict[str, str]:
    """Tokeniza cada remitente identificado por mensaje.

    El token por mensaje conserva procedencia: un texto plano de Nicolas sobre
    Gabriel no puede aprovechar que Gabriel haya escrito otro mensaje dentro
    del mismo lote. Los mensajes del simulador sin JID mantienen su nombre para
    compatibilidad, pero el gateway real siempre aporta identidad.
    """
    aliases: dict[str, str] = {}
    for index, message in enumerate(messages):
        identities = channel_identity_values([message.sender_id, *message.sender_aliases])
        if identities:
            digest = hashlib.sha256("|".join(sorted(identities)).encode("utf-8")).hexdigest()[:8]
            aliases[message.id] = f"Remitente{index}x{digest}"
        else:
            aliases[message.id] = message.sender.strip()
    return aliases


def is_usable_channel_display_name(name: str | None) -> bool:
    """True si el texto puede mostrarse como nombre de persona en el grupo.

    Rechaza placeholders internos, JIDs, telefonos y etiquetas solo numericas.
    WhatsApp a veces inserta el LID (`@119048071307283`) en vez del nombre
    visible; eso no debe filtrarse al mensaje del bot.
    """

    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered in {"participante", "agente", "contacto mencionado", "yo"}:
        return False
    if cleaned.endswith("@s.whatsapp.net") or cleaned.endswith("@lid") or cleaned.endswith("@c.us"):
        return False
    if re.fullmatch(r"\d{5,}(?:@\S+)?", cleaned):
        return False
    if re.fullmatch(r"Contacto [A-F0-9]{4}", cleaned, flags=re.IGNORECASE):
        return False
    return bool(re.search(r"[^\W\d_]", cleaned, flags=re.UNICODE))


def build_channel_identity_display_names(messages: list[ChannelMessage]) -> dict[str, str]:
    """Indice identidad WhatsApp -> pushName humano visto en el canal.

    Usa el nombre publico (`sender` / pushName) de cada mensaje humano y lo
    asocia a todos sus aliases (PN y LID). Asi una mencion numerica posterior
    del mismo JID puede mostrarse como Gabriel en vez de "Contacto mencionado".
    """

    names: dict[str, str] = {}
    for message in messages:
        if message.kind != "human":
            continue
        if not is_usable_channel_display_name(message.sender):
            continue
        display_name = normalize_person_name(message.sender.strip())
        for identity in channel_identity_values([message.sender_id, *message.sender_aliases]):
            existing = names.get(identity)
            if existing is None or not is_usable_channel_display_name(existing):
                names[identity] = display_name
    return names


def resolve_display_name_for_identities(
    identities: list[str] | tuple[str, ...],
    display_names: dict[str, str],
    *,
    fallback: str = "Contacto mencionado",
) -> str:
    for identity in channel_identity_values(tuple(identities)):
        candidate = display_names.get(identity)
        if candidate and is_usable_channel_display_name(candidate):
            return candidate
    return fallback


def prepare_channel_messages(messages: list[ChannelMessage]) -> list[PreparedChannelMessage]:
    sender_tokens = channel_sender_aliases(messages)
    display_names = build_channel_identity_display_names(messages)
    prepared_messages: list[PreparedChannelMessage] = []
    for index, message in enumerate(messages):
        if message.kind != "human":
            continue

        sender_ids = channel_identity_values([message.sender_id, *message.sender_aliases])
        sender_display = normalize_person_name(message.sender.strip())
        if not is_usable_channel_display_name(sender_display):
            sender_display = resolve_display_name_for_identities(
                sender_ids,
                display_names,
                fallback=sender_display or "Participante",
            )
        sender = ChannelIdentityBinding(
            token=sender_tokens.get(message.id, message.sender.strip()),
            display_name=sender_display,
            external_ids=tuple(sender_ids),
        )
        text = remove_invocation_tokens(message.text)
        text, mention, ambiguous = prepare_verified_mention(
            message,
            index,
            text,
            display_names=display_names,
        )
        rewritten = rewrite_first_person_availability(sender.token, text)
        is_self_report = (
            normalize(rewritten) != normalize(text)
            and has_extractable_scheduling_signal(rewritten)
        )
        authorized_names = {
            *( [normalize(sender.token)] if is_self_report else [] ),
            *( [normalize(mention.token)] if mention and has_extractable_scheduling_signal(text) else [] ),
        }
        detected_names = {normalize(name) for name in detect_participant_names(rewritten)}
        requires_mention = any(name not in authorized_names for name in detected_names)
        prepared_messages.append(
            PreparedChannelMessage(
                message=message,
                text=text,
                sender=sender,
                mention=mention,
                ambiguous_mention=ambiguous,
                requires_mention=requires_mention,
                has_authorized_subject=bool(authorized_names),
            )
        )
    return prepared_messages


def prepare_verified_mention(
    message: ChannelMessage,
    message_index: int,
    text: str,
    display_names: dict[str, str] | None = None,
) -> tuple[str, ChannelIdentityBinding | None, bool]:
    mentioned_ids = channel_identity_values(tuple(message.mentioned_jids))
    if not mentioned_ids:
        return text, None, False

    mention_tokens = list(_CHANNEL_MENTION_PATTERN.finditer(text))
    scheduling_signal = has_extractable_scheduling_signal(text)
    # Sin una correspondencia uno-a-uno no se asigna por posición: el orden de
    # mentionedJid no es una prueba suficiente para distinguir dos contactos.
    if len(mentioned_ids) != 1 or len(mention_tokens) != 1:
        return text, None, scheduling_signal

    match = mention_tokens[0]
    external_id = mentioned_ids[0]
    raw_label = match.group(1).strip()
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:8]
    token = f"Mencionado{message_index}x{digest}"
    if is_usable_channel_display_name(raw_label):
        display_name = normalize_person_name(raw_label)
    else:
        # WhatsApp a menudo deja el LID numerico en el texto (`@1190...`).
        # Si ya vimos el pushName de ese JID en el canal, usarlo; si no, el
        # placeholder generico. La identidad tecnica sigue siendo el JID.
        display_name = resolve_display_name_for_identities(
            [external_id],
            display_names or {},
            fallback="Contacto mencionado",
        )
    replaced = f"{text[:match.start()]}{token}{text[match.end():]}"
    return (
        replaced,
        ChannelIdentityBinding(
            token=token,
            display_name=display_name,
            external_ids=(external_id,),
        ),
        False,
    )


def apply_exclusive_availability_overrides(
    extraction: ExtractedAvailability,
    original_message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> ExtractedAvailability:
    overrides = find_exclusive_availability_overrides(original_message, workday_start, workday_end)
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
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> ExtractedAvailability:
    overrides = find_grounded_availability_overrides(original_message, workday_start, workday_end)
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


def find_grounded_availability_overrides(
    original_message: str,
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> list[tuple[str, list[TimeSlot]]]:
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
                slots_in_workday(days, hour, workday_end, workday_start, workday_end),
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
    workday_start: int = WORKDAY_START_HOUR,
    workday_end: int = WORKDAY_END_HOUR,
) -> list[tuple[str, list[TimeSlot], list[TimeSlot]]]:
    overrides: list[tuple[str, list[TimeSlot], list[TimeSlot]]] = []
    for fragment in split_scheduling_fragments(original_message):
        if not is_exclusive_availability_fragment(fragment):
            continue

        participant_name = detect_name(fragment)
        if not participant_name:
            continue

        availability = detect_slots(fragment, workday_start, workday_end)
        if not availability:
            continue

        week = detect_week_offset(fragment)
        if week:
            availability = [slot.model_copy(update={"week_offset": week}) for slot in availability]

        available_days = {slot.day for slot in availability}
        removal_slots = [
            TimeSlot(
                day=day,
                start=f"{workday_start:02d}:00",
                end=f"{workday_end:02d}:00",
                week_offset=week,
            )
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


def is_replacement_availability_fragment(fragment: str) -> bool:
    """Detecta correcciones explicitas; una ampliacion con "tambien" no reemplaza."""
    normalized = normalize(fragment)
    if "tambien" in normalized:
        return False
    correction = re.search(
        r"\b(?:en\s+realidad|en\s+verdad|me\s+corrijo|corrijo|correccion|"
        r"quise\s+decir|actualizo(?:\s+mi)?\s+disponibilidad|ahora\s+solo)\b",
        normalized,
    )
    positive = re.search(
        r"\b(?:puedo|puede|pueden|podre|disponible|libre|sirve|acomoda|tinca)\b",
        normalized,
    )
    return bool(correction and positive)


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
        match = re.match(r"(\d+(?:\.\d+)?)s", retry_delay)
        if match:
            return max(1, math.ceil(float(match.group(1))))

    return None


def is_invalid_gemini_key_error(error: GeminiApiError) -> bool:
    if error.status_code in {401, 403}:
        return True
    normalized = normalize(error.detail)
    return error.status_code == 400 and any(
        marker in normalized
        for marker in (
            "api_key_invalid",
            "api key not valid",
            "invalid api key",
            "api key was reported as leaked",
        )
    )


def is_incompatible_gemini_model_error(error: GeminiApiError) -> bool:
    # generateContent con 404: modelo no usable para esa key (p. ej. "no longer
    # available to new users"), recurso no encontrado o parametro invalido.
    # No implica por si solo que el modelo no exista en el catalogo global.
    return error.status_code == 404


def gemini_models_for_request() -> list[str]:
    """Primary Gemini model first, then optional fallback (e.g. flash-lite-latest)."""
    models: list[str] = []
    primary = (settings.gemini_model or "").strip()
    if primary:
        models.append(primary)
    fallback = (settings.gemini_model_fallback or "").strip()
    if fallback and fallback not in models:
        models.append(fallback)
    return models


def gemini_quota_cooldown_seconds(error: GeminiApiError, credential: dict) -> int:
    if error.retry_after_seconds:
        return error.retry_after_seconds
    if is_daily_gemini_quota_error(error.detail):
        pacific = _load_zoneinfo("America/Los_Angeles") or dt_timezone.utc
        now = datetime.now(pacific)
        tomorrow = (now + timedelta(days=1)).date()
        reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=pacific)
        return max(60, math.ceil((reset - now).total_seconds()))
    failures = int(credential.get("failure_count", 0))
    return min(
        3600,
        max(1, settings.gemini_cooldown_seconds) * (2 ** min(failures, 5)),
    )


def is_daily_gemini_quota_error(detail: str) -> bool:
    normalized = normalize(detail)
    return any(
        marker in normalized
        for marker in (
            "per day",
            "per_day",
            "perday",
            "requests per day",
            "requestsdaily",
            "rpd",
        )
    )


def redact_sensitive_text(value: str) -> str:
    sanitized = re.sub(r"AIza[0-9A-Za-z_-]{12,}", "[REDACTED_GEMINI_KEY]", value)
    sanitized = re.sub(
        r"(?i)(x-goog-api-key\s*[:=]\s*)[^\s,;\"']+",
        r"\1[REDACTED]",
        sanitized,
    )
    return sanitized


def extract_json(content: str) -> str:
    """Extract a single top-level JSON object from model text.

    Accepts optional markdown fences. Does not repair truncated/invalid JSON.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("LLM response does not contain JSON")

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        json.loads(candidate)  # validate
        return candidate

    # Prefer balanced object starting at first '{'
    start = text.find("{")
    if start < 0:
        raise ValueError("LLM response does not contain JSON")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                json.loads(candidate)
                return candidate
    raise ValueError("LLM response does not contain JSON")


def build_gemini_stream_url(url: str) -> str:
    stream_url = url.replace(":generateContent", ":streamGenerateContent")
    if "alt=sse" in stream_url:
        return stream_url
    separator = "&" if "?" in stream_url else "?"
    return f"{stream_url}{separator}alt=sse"


def read_gemini_sse(response, request_started: float) -> tuple[dict, int]:
    text_chunks: list[str] = []
    usage_metadata: dict = {}
    event_data: list[str] = []
    first_token_ms: int | None = None

    def consume_event() -> None:
        nonlocal first_token_ms, usage_metadata
        if not event_data:
            return
        payload = "\n".join(event_data).strip()
        event_data.clear()
        if not payload or payload == "[DONE]":
            return
        chunk = json.loads(payload)
        if chunk.get("error"):
            error = chunk["error"]
            raise GeminiApiError(
                int(error.get("code", 500)),
                json.dumps(error, ensure_ascii=False),
                parse_retry_delay_seconds(json.dumps(error, ensure_ascii=False)),
            )
        for text in gemini_text_parts(chunk):
            if first_token_ms is None:
                first_token_ms = max(0, round((time.perf_counter() - request_started) * 1000))
            text_chunks.append(text)
        if chunk.get("usageMetadata"):
            usage_metadata = dict(chunk["usageMetadata"])

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            consume_event()
            continue
        if line.startswith("data:"):
            event_data.append(line[5:].lstrip())
    consume_event()

    if first_token_ms is None or not text_chunks:
        raise ValueError("Gemini streaming response does not contain text")

    return (
        {
            "candidates": [{"content": {"parts": [{"text": "".join(text_chunks)}]}}],
            "usageMetadata": usage_metadata,
        },
        first_token_ms,
    )


def gemini_text_parts(raw: dict) -> list[str]:
    candidates = raw.get("candidates") or []
    if not candidates:
        return []
    parts = candidates[0].get("content", {}).get("parts") or []
    return [part.get("text", "") for part in parts if part.get("text")]


def extract_gemini_text(raw: dict) -> str:
    texts = gemini_text_parts(raw)
    if not raw.get("candidates"):
        raise ValueError("Gemini response does not contain candidates")
    if not texts:
        raise ValueError("Gemini response does not contain text")

    return "\n".join(texts)


llm_service = LlmService()
