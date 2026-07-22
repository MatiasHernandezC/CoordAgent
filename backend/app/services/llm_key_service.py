from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException

from app.schemas import TokenUsage
from app.services.credential_crypto import (
    CredentialEncryptionError,
    decode_master_key,
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
)
from app.settings import settings

if settings.db_backend == "json":
    from app.storage.json_llm_key_repository import repository
else:
    from app.storage.postgres_llm_key_repository import repository


LEGACY_ENV_ID = "env:GEMINI_API_KEY"


class LlmKeyService:
    def __init__(self) -> None:
        self._lock = RLock()

    def management_enabled(self) -> bool:
        try:
            decode_master_key(settings.llm_keys_master_key)
        except CredentialEncryptionError:
            return False
        return True

    def list_state(self, *, audit_limit: int = 30) -> dict:
        with self._lock:
            self._ensure_legacy_import()
            records = [
                self._refresh_expired_cooldown(item)
                for item in self._records_with_virtual_legacy()
            ]
            public_keys = [self._public_record(item) for item in self._ordered(records)]
            candidates = [item for item in records if self._is_available(item)]
            ordered_candidates = self._ordered(candidates)
            return {
                "keys": public_keys,
                "audit": repository.list_audit(audit_limit),
                "active_key_id": ordered_candidates[0]["id"] if ordered_candidates else None,
                "available_count": len(ordered_candidates),
                "management_enabled": self.management_enabled(),
            }

    def add(self, name: str, secret: str, priority: int | None, actor: str) -> dict:
        with self._lock:
            self._require_management()
            clean_name = self._clean_name(name)
            clean_secret = secret.strip()
            if not 8 <= len(clean_secret) <= 512:
                raise HTTPException(
                    status_code=422,
                    detail="La llave Gemini no tiene una longitud valida.",
                )
            records = repository.list_keys()
            self._assert_unique_name(clean_name, records)
            fingerprint = secret_fingerprint(clean_secret, settings.llm_keys_master_key)
            if any(item.get("fingerprint") == fingerprint for item in records):
                raise HTTPException(status_code=409, detail="Esta llave Gemini ya esta registrada.")

            now = _now_iso()
            credential_id = str(uuid4())
            record = {
                "id": credential_id,
                "name": clean_name,
                "encrypted_secret": encrypt_secret(
                    clean_secret,
                    settings.llm_keys_master_key,
                    credential_id,
                ),
                "fingerprint": fingerprint,
                "priority": priority if priority is not None else self._next_priority(records),
                "enabled": True,
                "status": "unverified",
                "source": "panel",
                "success_count": 0,
                "failure_count": 0,
                "request_count": 0,
                "error_count": 0,
                "quota_exhaustion_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "ttft_sample_count": 0,
                "ttft_total_ms": 0,
                "last_ttft_ms": None,
                "best_ttft_ms": None,
                "worst_ttft_ms": None,
                "last_generation_at": None,
                "last_used_at": None,
                "cooldown_until": None,
                "last_error_code": None,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            repository.insert_key(record)
            self._audit(record, "created", actor, "ok")
            return self._public_record(record)

    def update(
        self,
        credential_id: str,
        *,
        name: str | None,
        priority: int | None,
        enabled: bool | None,
        actor: str,
    ) -> dict:
        with self._lock:
            self._require_management()
            record = self._require_record(credential_id)
            updates: dict = {"updated_at": _now_iso()}
            if name is not None:
                clean_name = self._clean_name(name)
                self._assert_unique_name(
                    clean_name,
                    repository.list_keys(),
                    exclude_id=credential_id,
                )
                updates["name"] = clean_name
            if priority is not None:
                updates["priority"] = priority
            if enabled is not None:
                updates["enabled"] = enabled
                updates["status"] = "unverified" if enabled else "disabled"
                updates["cooldown_until"] = None
                updates["last_error"] = None if enabled else record.get("last_error")
            updated = repository.update_key(credential_id, updates)
            if updated is None:
                raise HTTPException(status_code=404, detail="Llave Gemini no encontrada.")
            self._audit(updated, "updated", actor, "ok")
            return self._public_record(updated)

    def delete(self, credential_id: str, confirm_name: str, actor: str) -> None:
        with self._lock:
            self._require_management()
            record = self._require_record(credential_id)
            if confirm_name.strip() != record["name"]:
                raise HTTPException(
                    status_code=409,
                    detail="El nombre de confirmacion no coincide.",
                )
            if not repository.delete_key(credential_id):
                raise HTTPException(status_code=404, detail="Llave Gemini no encontrada.")
            self._audit(record, "deleted", actor, "ok")

    def candidate_records(self, excluded_ids: set[str] | None = None) -> list[dict]:
        with self._lock:
            self._ensure_legacy_import()
            excluded = excluded_ids or set()
            candidates: list[dict] = []
            for record in self._records_with_virtual_legacy():
                if record["id"] in excluded:
                    continue
                refreshed = self._refresh_expired_cooldown(record)
                if self._is_available(refreshed):
                    candidates.append(refreshed)
            return self._ordered(candidates)

    def reveal_secret(self, record: dict) -> str:
        if record.get("id") == LEGACY_ENV_ID:
            return settings.gemini_api_key.strip()
        return decrypt_secret(
            record["encrypted_secret"],
            settings.llm_keys_master_key,
            record["id"],
        )

    def get_record(self, credential_id: str) -> dict:
        with self._lock:
            if credential_id == LEGACY_ENV_ID and self._virtual_legacy_available():
                return self._virtual_legacy_record()
            return self._require_record(credential_id)

    def public_record(self, credential_id: str) -> dict:
        with self._lock:
            return self._public_record(self.get_record(credential_id))

    def next_retry_seconds(self) -> int | None:
        with self._lock:
            waits = []
            now = datetime.now(timezone.utc)
            for record in repository.list_keys():
                if not record.get("enabled") or record.get("status") != "cooldown":
                    continue
                cooldown_until = record.get("cooldown_until")
                if cooldown_until:
                    waits.append(max(1, int((_parse_datetime(cooldown_until) - now).total_seconds())))
            return min(waits) if waits else None

    def mark_success(self, credential_id: str, token_usage: TokenUsage | None = None) -> None:
        with self._lock:
            record = repository.get_key(credential_id)
            if record is None:
                return
            now = _now_iso()
            updates = {
                "status": "ready",
                "success_count": int(record.get("success_count", 0)) + 1,
                "failure_count": 0,
                "last_used_at": now,
                "cooldown_until": None,
                "last_error_code": None,
                "last_error": None,
                "updated_at": now,
            }
            if token_usage is not None:
                updates.update({
                    "request_count": int(record.get("request_count", 0)) + 1,
                    "prompt_tokens": int(record.get("prompt_tokens", 0)) + token_usage.prompt_tokens,
                    "completion_tokens": int(record.get("completion_tokens", 0)) + token_usage.completion_tokens,
                    "total_tokens": int(record.get("total_tokens", 0)) + token_usage.total_tokens,
                    "estimated_cost_usd": round(
                        float(record.get("estimated_cost_usd", 0.0)) + token_usage.estimated_cost_usd,
                        8,
                    ),
                    "last_generation_at": now,
                })
                if token_usage.time_to_first_token_ms is not None:
                    ttft_ms = max(0, int(token_usage.time_to_first_token_ms))
                    sample_count = int(record.get("ttft_sample_count", 0)) + 1
                    ttft_total_ms = int(record.get("ttft_total_ms", 0)) + ttft_ms
                    previous_best = record.get("best_ttft_ms")
                    previous_worst = record.get("worst_ttft_ms")
                    updates.update({
                        "ttft_sample_count": sample_count,
                        "ttft_total_ms": ttft_total_ms,
                        "last_ttft_ms": ttft_ms,
                        "best_ttft_ms": ttft_ms if previous_best is None else min(int(previous_best), ttft_ms),
                        "worst_ttft_ms": ttft_ms if previous_worst is None else max(int(previous_worst), ttft_ms),
                    })
            repository.update_key(
                credential_id,
                updates,
            )

    def mark_quota(self, credential_id: str, seconds: int) -> None:
        self._mark_failure(
            credential_id,
            status="cooldown",
            code=429,
            detail="Cuota o limite de uso agotado.",
            cooldown_until=(datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat(),
            quota_exhausted=True,
        )

    def mark_invalid(self, credential_id: str, code: int) -> None:
        self._mark_failure(
            credential_id,
            status="invalid",
            code=code,
            detail="Llave invalida, vencida o sin permisos.",
            cooldown_until=None,
            quota_exhausted=False,
        )

    def mark_incompatible(self, credential_id: str, code: int = 404) -> None:
        self._mark_failure(
            credential_id,
            status="incompatible",
            code=code,
            detail="El modelo Gemini configurado no esta disponible para este proyecto.",
            cooldown_until=None,
            quota_exhausted=False,
        )

    def mark_transient_error(self, credential_id: str, code: int | None) -> None:
        self._mark_failure(
            credential_id,
            status=None,
            code=code,
            detail="Gemini no estuvo disponible temporalmente.",
            cooldown_until=None,
            quota_exhausted=False,
        )

    def audit_test(self, record: dict, actor: str, result: str) -> None:
        with self._lock:
            self._audit(record, "tested", actor, result)

    def _mark_failure(
        self,
        credential_id: str,
        *,
        status: str | None,
        code: int | None,
        detail: str,
        cooldown_until: str | None,
        quota_exhausted: bool,
    ) -> None:
        with self._lock:
            record = repository.get_key(credential_id)
            if record is None:
                return
            updates = {
                "failure_count": int(record.get("failure_count", 0)) + 1,
                "error_count": int(record.get("error_count", 0)) + 1,
                "quota_exhaustion_count": int(record.get("quota_exhaustion_count", 0))
                + (1 if quota_exhausted else 0),
                "last_used_at": _now_iso(),
                "last_error_code": code,
                "last_error": detail,
                "cooldown_until": cooldown_until,
                "updated_at": _now_iso(),
            }
            if status is not None:
                updates["status"] = status
            repository.update_key(credential_id, updates)

    def _ensure_legacy_import(self) -> None:
        if not settings.gemini_api_key.strip() or not self.management_enabled():
            return
        audit = repository.list_audit(500)
        if any(item.get("action") == "legacy_imported" for item in audit):
            return
        records = repository.list_keys()
        fingerprint = secret_fingerprint(
            settings.gemini_api_key,
            settings.llm_keys_master_key,
        )
        existing = next(
            (item for item in records if item.get("fingerprint") == fingerprint),
            None,
        )
        if existing is not None:
            self._audit(existing, "legacy_imported", "system", "already_registered")
            return
        now = _now_iso()
        credential_id = str(uuid4())
        base_name = "Produccion heredada"
        name = base_name
        suffix = 2
        existing_names = {item.get("name", "").casefold() for item in records}
        while name.casefold() in existing_names:
            name = f"{base_name} {suffix}"
            suffix += 1
        record = {
            "id": credential_id,
            "name": name,
            "encrypted_secret": encrypt_secret(
                settings.gemini_api_key,
                settings.llm_keys_master_key,
                credential_id,
            ),
            "fingerprint": fingerprint,
            "priority": 0,
            "enabled": True,
            "status": "unverified",
            "source": "legacy_env",
            "success_count": 0,
            "failure_count": 0,
            "request_count": 0,
            "error_count": 0,
            "quota_exhaustion_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "ttft_sample_count": 0,
            "ttft_total_ms": 0,
            "last_ttft_ms": None,
            "best_ttft_ms": None,
            "worst_ttft_ms": None,
            "last_generation_at": None,
            "last_used_at": None,
            "cooldown_until": None,
            "last_error_code": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        repository.insert_key(record)
        self._audit(record, "legacy_imported", "system", "ok")

    def _records_with_virtual_legacy(self) -> list[dict]:
        records = repository.list_keys()
        if self._virtual_legacy_available():
            records.append(self._virtual_legacy_record())
        return records

    def _virtual_legacy_available(self) -> bool:
        if not settings.gemini_api_key.strip():
            return False
        if self.management_enabled():
            audit = repository.list_audit(500)
            if any(item.get("action") == "legacy_imported" for item in audit):
                return False
        return not any(item.get("source") == "legacy_env" for item in repository.list_keys())

    def _virtual_legacy_record(self) -> dict:
        now = _now_iso()
        return {
            "id": LEGACY_ENV_ID,
            "name": "Llave de entorno",
            "priority": 0,
            "enabled": True,
            "status": "ready",
            "source": "legacy_env",
            "success_count": 0,
            "failure_count": 0,
            "request_count": 0,
            "error_count": 0,
            "quota_exhaustion_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "ttft_sample_count": 0,
            "ttft_total_ms": 0,
            "last_ttft_ms": None,
            "best_ttft_ms": None,
            "worst_ttft_ms": None,
            "last_generation_at": None,
            "last_used_at": None,
            "cooldown_until": None,
            "last_error_code": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
            "managed": False,
        }

    def _refresh_expired_cooldown(self, record: dict) -> dict:
        if record.get("status") != "cooldown" or not record.get("cooldown_until"):
            return record
        if _parse_datetime(record["cooldown_until"]) > datetime.now(timezone.utc):
            return record
        if record["id"] == LEGACY_ENV_ID:
            return {**record, "status": "ready", "cooldown_until": None}
        updated = repository.update_key(
            record["id"],
            {
                "status": "ready",
                "cooldown_until": None,
                "updated_at": _now_iso(),
            },
        )
        return updated or record

    def _is_available(self, record: dict) -> bool:
        if not record.get("enabled", False):
            return False
        if record.get("status") not in {"ready", "unverified"}:
            return False
        return True

    def _public_record(self, record: dict) -> dict:
        # Lista explicita: encrypted_secret y fingerprint nunca pueden filtrarse.
        ttft_sample_count = int(record.get("ttft_sample_count", 0))
        ttft_total_ms = int(record.get("ttft_total_ms", 0))
        return {
            "id": record["id"],
            "name": record["name"],
            "priority": int(record.get("priority", 0)),
            "enabled": bool(record.get("enabled", False)),
            "status": record.get("status", "unverified"),
            "source": record.get("source", "panel"),
            "managed": bool(record.get("managed", True)),
            "success_count": int(record.get("success_count", 0)),
            "failure_count": int(record.get("failure_count", 0)),
            "request_count": int(record.get("request_count", 0)),
            "error_count": int(record.get("error_count", 0)),
            "quota_exhaustion_count": int(record.get("quota_exhaustion_count", 0)),
            "prompt_tokens": int(record.get("prompt_tokens", 0)),
            "completion_tokens": int(record.get("completion_tokens", 0)),
            "total_tokens": int(record.get("total_tokens", 0)),
            "estimated_cost_usd": float(record.get("estimated_cost_usd", 0.0)),
            "ttft_sample_count": ttft_sample_count,
            "last_ttft_ms": _optional_int(record.get("last_ttft_ms")),
            "average_ttft_ms": round(ttft_total_ms / ttft_sample_count) if ttft_sample_count else None,
            "best_ttft_ms": _optional_int(record.get("best_ttft_ms")),
            "worst_ttft_ms": _optional_int(record.get("worst_ttft_ms")),
            "last_generation_at": record.get("last_generation_at"),
            "last_used_at": record.get("last_used_at"),
            "cooldown_until": record.get("cooldown_until"),
            "last_error_code": record.get("last_error_code"),
            "last_error": record.get("last_error"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def _audit(self, record: dict, action: str, actor: str, result: str) -> None:
        repository.append_audit(
            {
                "id": str(uuid4()),
                "credential_id": record.get("id"),
                "credential_name": record.get("name", "Llave eliminada"),
                "action": action,
                "actor": actor.strip() or "admin",
                "result": result,
                "created_at": _now_iso(),
            }
        )

    def _require_record(self, credential_id: str) -> dict:
        record = repository.get_key(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Llave Gemini no encontrada.")
        return record

    def _require_management(self) -> None:
        if not self.management_enabled():
            raise HTTPException(
                status_code=503,
                detail="Configura LLM_KEYS_MASTER_KEY antes de administrar llaves Gemini.",
            )

    @staticmethod
    def _clean_name(name: str) -> str:
        value = " ".join(name.split())
        if len(value) < 2:
            raise HTTPException(status_code=422, detail="El nombre de la llave es demasiado corto.")
        return value

    @staticmethod
    def _assert_unique_name(name: str, records: list[dict], exclude_id: str | None = None) -> None:
        if any(
            item.get("id") != exclude_id and item.get("name", "").casefold() == name.casefold()
            for item in records
        ):
            raise HTTPException(status_code=409, detail="Ya existe una llave con ese nombre.")

    @staticmethod
    def _next_priority(records: list[dict]) -> int:
        return max((int(item.get("priority", 0)) for item in records), default=0) + 10

    @staticmethod
    def _ordered(records: list[dict]) -> list[dict]:
        return sorted(
            records,
            key=lambda item: (
                int(item.get("priority", 0)),
                item.get("created_at", ""),
                item.get("name", "").casefold(),
            ),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


llm_key_service = LlmKeyService()
