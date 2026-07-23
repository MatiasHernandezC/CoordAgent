import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

import app.services.llm_key_service as key_module
from app.main import app
from app.schemas import ExtractedAvailability, Participant, TimeSlot, TokenUsage
from app.services.credential_crypto import (
    CredentialEncryptionError,
    decrypt_secret,
    encrypt_secret,
)
from app.services.llm_key_service import LlmKeyService
from app.services.llm_service import GeminiApiError, LlmService, is_daily_gemini_quota_error
from app.settings import settings
from app.storage.json_llm_key_repository import JsonLlmKeyRepository


def master_key(seed: int = 0) -> str:
    return base64.urlsafe_b64encode(bytes((seed + index) % 256 for index in range(32))).decode("ascii")


@pytest.fixture()
def key_service(tmp_path, monkeypatch):
    repository = JsonLlmKeyRepository(tmp_path / "llm_keys.json")
    monkeypatch.setattr(key_module, "repository", repository)
    monkeypatch.setattr(settings, "llm_keys_master_key", master_key())
    monkeypatch.setattr(settings, "gemini_api_key", "")
    return LlmKeyService(), repository


def successful_result(ttft_ms: int = 750):
    return (
        ExtractedAvailability(
            participants=[
                Participant(
                    name="Ana",
                    availability=[TimeSlot(day="lunes", start="09:00", end="10:00")],
                )
            ]
        ),
        TokenUsage(
            provider="gemini",
            prompt_tokens=11,
            completion_tokens=4,
            total_tokens=15,
            estimated_cost_usd=0.000123,
            time_to_first_token_ms=ttft_ms,
        ),
    )


def test_aes_gcm_roundtrip_and_wrong_master_key():
    encrypted = encrypt_secret("gemini-secret-value", master_key(), "credential-1")

    assert "gemini-secret-value" not in encrypted
    assert decrypt_secret(encrypted, master_key(), "credential-1") == "gemini-secret-value"
    with pytest.raises(CredentialEncryptionError):
        decrypt_secret(encrypted, master_key(7), "credential-1")


def test_crud_never_exposes_or_audits_secret(key_service):
    service, repository = key_service
    secret = "AIza-production-secret-123456"
    created = service.add("Proyecto principal", secret, 10, "nico")

    assert "encrypted_secret" not in created
    assert "fingerprint" not in created
    assert secret not in repository.path.read_text(encoding="utf-8")
    state = service.list_state()
    assert state["keys"][0]["name"] == "Proyecto principal"
    assert state["audit"][0]["actor"] == "nico"
    assert secret not in str(state)

    updated = service.update(
        created["id"],
        name="Proyecto respaldo",
        priority=20,
        enabled=False,
        actor="nico",
    )
    assert updated["status"] == "disabled"
    with pytest.raises(Exception) as mismatch:
        service.delete(created["id"], "nombre incorrecto", "nico")
    assert getattr(mismatch.value, "status_code", None) == 409

    service.delete(created["id"], "Proyecto respaldo", "nico")
    assert service.list_state()["keys"] == []
    assert any(item["action"] == "deleted" for item in repository.list_audit())


def test_duplicate_name_and_secret_are_rejected(key_service):
    service, _ = key_service
    service.add("Primaria", "AIza-secret-one-123456", 1, "admin")
    with pytest.raises(Exception) as duplicate_name:
        service.add("primaria", "AIza-secret-two-123456", 2, "admin")
    assert getattr(duplicate_name.value, "status_code", None) == 409
    with pytest.raises(Exception) as duplicate_secret:
        service.add("Secundaria", "AIza-secret-one-123456", 2, "admin")
    assert getattr(duplicate_secret.value, "status_code", None) == 409


def test_order_and_observed_usage_are_automatic(key_service, monkeypatch):
    service, _ = key_service
    first = service.add("Proyecto A", "AIza-secret-one-123456", None, "admin")
    second = service.add("Proyecto B", "AIza-secret-two-123456", None, "admin")
    assert first["priority"] == 10
    assert second["priority"] == 20

    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)
    llm = LlmService()
    observed_results = iter([successful_result(500), successful_result(1500)])
    monkeypatch.setattr(llm, "_extract_with_gemini", lambda *args, **kwargs: next(observed_results))

    llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)
    llm._extract_with_gemini_pool("Ana puede martes", 9, 18)
    usage = service.public_record(first["id"])

    assert usage["request_count"] == 2
    assert usage["prompt_tokens"] == 22
    assert usage["completion_tokens"] == 8
    assert usage["total_tokens"] == 30
    assert usage["estimated_cost_usd"] == 0.000246
    assert usage["ttft_sample_count"] == 2
    assert usage["last_ttft_ms"] == 1500
    assert usage["average_ttft_ms"] == 1000
    assert usage["best_ttft_ms"] == 500
    assert usage["worst_ttft_ms"] == 1500
    assert usage["last_generation_at"] is not None

    # Probar una llave valida sus permisos, pero no cuenta como una generacion.
    service.mark_success(first["id"])
    assert service.public_record(first["id"])["request_count"] == 2

    service.mark_quota(first["id"], 30)
    quota = service.public_record(first["id"])
    assert quota["quota_exhaustion_count"] == 1
    assert quota["error_count"] == 1


def test_429_rotates_to_next_key_and_sets_cooldown(key_service, monkeypatch):
    service, _ = key_service
    first = service.add("Primaria", "AIza-first-key-123456", 1, "admin")
    second = service.add("Respaldo", "AIza-second-key-123456", 2, "admin")
    monkeypatch.setattr(key_module, "llm_key_service", service, raising=False)
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)
    llm = LlmService()
    attempts = []

    def fake_extract(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
        attempts.append(api_key)
        if api_key == "AIza-first-key-123456":
            raise GeminiApiError(429, '{"error":{"status":"RESOURCE_EXHAUSTED"}}', 30)
        return successful_result()

    monkeypatch.setattr(llm, "_extract_with_gemini", fake_extract)
    extraction, _ = llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)

    assert extraction.participants[0].name == "Ana"
    assert attempts == ["AIza-first-key-123456", "AIza-second-key-123456"]
    assert service.public_record(first["id"])["status"] == "cooldown"
    assert service.public_record(second["id"])["status"] == "ready"


def test_404_model_incompatible_rotates_and_only_business_success_counts_usage(
    key_service,
    monkeypatch,
):
    service, _ = key_service
    first = service.add("Proyecto sin modelo", "AIza-first-key-123456", 1, "admin")
    second = service.add("Proyecto compatible", "AIza-second-key-123456", 2, "admin")
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)
    llm = LlmService()
    attempts = []

    def model_missing_then_success(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
        attempts.append(api_key)
        if api_key == "AIza-first-key-123456":
            raise GeminiApiError(
                404,
                '{"error":{"code":404,"status":"NOT_FOUND",'
                '"message":"models/gemini-test is not found for API version v1beta, '
                'or is not supported for generateContent"}}',
            )
        return successful_result(ttft_ms=640)

    monkeypatch.setattr(llm, "_extract_with_gemini", model_missing_then_success)
    extraction, _ = llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)

    assert extraction.participants[0].name == "Ana"
    assert attempts == ["AIza-first-key-123456", "AIza-second-key-123456"]
    incompatible = service.public_record(first["id"])
    successful = service.public_record(second["id"])
    assert incompatible["status"] == "incompatible"
    assert incompatible["last_error_code"] == 404
    assert incompatible["error_count"] == 1
    assert incompatible["request_count"] == 0
    assert successful["status"] == "ready"
    assert successful["request_count"] == 1
    assert successful["total_tokens"] == 15
    assert successful["ttft_sample_count"] == 1
    # Con GEMINI_MODEL_FALLBACK activo, las llaves incompatible siguen en el pool
    # para reintentar con el modelo alternativo.
    candidate_ids = [item["id"] for item in service.candidate_records()]
    assert second["id"] in candidate_ids
    assert first["id"] in candidate_ids


def test_probe_uses_real_generation_and_does_not_accept_metadata_false_positive(
    key_service,
    monkeypatch,
):
    service, repository = key_service
    created = service.add("Proyecto sin modelo", "AIza-probe-key-123456", None, "admin")
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)
    monkeypatch.setattr(
        settings,
        "gemini_url",
        "https://generativelanguage.googleapis.test/v1beta/models/{model}:generateContent",
    )
    methods = []
    payloads = []

    class MetadataResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def metadata_would_pass_but_generation_fails(request, timeout):
        methods.append(request.get_method())
        if request.data:
            payloads.append(json.loads(request.data.decode("utf-8")))
        if request.get_method() == "GET":
            return MetadataResponse()
        detail = (
            b'{"error":{"code":404,"status":"NOT_FOUND",'
            b'"message":"model is not supported for generateContent"}}'
        )
        raise HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=BytesIO(detail))

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", metadata_would_pass_but_generation_fails)
    result = LlmService().test_gemini_credential(created["id"], "admin")

    assert result["ok"] is False
    # Un POST por modelo de la cadena (principal + fallback).
    assert methods == ["POST", "POST"]
    assert payloads[0]["contents"][0]["parts"][0]["text"]
    tested = result["key"]
    assert tested["status"] == "incompatible"
    assert tested["last_error_code"] == 404
    assert tested["request_count"] == 0
    assert tested["total_tokens"] == 0
    assert tested["ttft_sample_count"] == 0
    assert repository.list_audit(1)[0]["result"] == "incompatible"


def test_successful_probe_generates_content_without_counting_business_usage(
    key_service,
    monkeypatch,
):
    service, _ = key_service
    created = service.add("Proyecto compatible", "AIza-probe-ok-123456", None, "admin")
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)

    class GenerationResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
            ).encode("utf-8")

    requested_methods = []

    def successful_generation(request, timeout):
        requested_methods.append(request.get_method())
        return GenerationResponse()

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", successful_generation)
    result = LlmService().test_gemini_credential(created["id"], "admin")

    assert result["ok"] is True
    assert requested_methods == ["POST"]
    tested = result["key"]
    assert tested["status"] == "ready"
    assert tested["request_count"] == 0
    assert tested["total_tokens"] == 0
    assert tested["ttft_sample_count"] == 0


def test_invalid_key_rotates_but_503_does_not_burn_next_key(key_service, monkeypatch):
    service, _ = key_service
    first = service.add("Primaria", "AIza-first-key-123456", 1, "admin")
    service.add("Respaldo", "AIza-second-key-123456", 2, "admin")
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)
    llm = LlmService()
    attempts = []

    def invalid_then_success(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
        attempts.append(api_key)
        if api_key == "AIza-first-key-123456":
            raise GeminiApiError(403, "PERMISSION_DENIED")
        return successful_result()

    monkeypatch.setattr(llm, "_extract_with_gemini", invalid_then_success)
    llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)
    assert service.public_record(first["id"])["status"] == "invalid"
    assert len(attempts) == 2

    service.update(first["id"], name=None, priority=None, enabled=True, actor="admin")
    attempts.clear()

    def unavailable(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
        attempts.append(api_key)
        raise GeminiApiError(503, "UNAVAILABLE")

    monkeypatch.setattr(llm, "_extract_with_gemini", unavailable)
    with pytest.raises(GeminiApiError) as error:
        llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)
    assert error.value.status_code == 503
    assert attempts == ["AIza-first-key-123456"]


def test_invalid_request_does_not_rotate_and_all_429_exhaust_pool(key_service, monkeypatch):
    service, _ = key_service
    first = service.add("Primaria", "AIza-first-key-123456", 1, "admin")
    second = service.add("Respaldo", "AIza-second-key-123456", 2, "admin")
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)
    llm = LlmService()
    attempts = []

    def invalid_request(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
        attempts.append(api_key)
        raise GeminiApiError(400, "INVALID_ARGUMENT")

    monkeypatch.setattr(llm, "_extract_with_gemini", invalid_request)
    with pytest.raises(GeminiApiError) as invalid:
        llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)
    assert invalid.value.status_code == 400
    assert attempts == ["AIza-first-key-123456"]

    def quota(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
        raise GeminiApiError(429, "RESOURCE_EXHAUSTED", 30)

    monkeypatch.setattr(llm, "_extract_with_gemini", quota)
    with pytest.raises(GeminiApiError) as exhausted:
        llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)
    assert exhausted.value.status_code == 429
    assert service.public_record(first["id"])["status"] == "cooldown"
    assert service.public_record(second["id"])["status"] == "cooldown"


def test_legacy_environment_key_is_imported_once_and_can_be_deleted(key_service, monkeypatch):
    service, repository = key_service
    legacy_secret = "AIza-legacy-production-123456"
    monkeypatch.setattr(settings, "gemini_api_key", legacy_secret)

    state = service.list_state()
    assert len(state["keys"]) == 1
    key = state["keys"][0]
    assert key["source"] == "legacy_env"
    assert legacy_secret not in repository.path.read_text(encoding="utf-8")

    service.delete(key["id"], key["name"], "admin")
    assert service.list_state()["keys"] == []


def test_error_details_are_redacted_and_daily_quota_is_detected():
    secret = "AIza-super-sensitive-production-key-123456789"
    error = GeminiApiError(403, f'{{"message":"invalid {secret}"}}')

    assert secret not in error.detail
    assert secret not in str(error)
    assert is_daily_gemini_quota_error('{"quotaId":"GenerateRequestsPerDayPerProject"}')


def test_concurrent_quota_rotation_remains_usable(key_service, monkeypatch):
    service, _ = key_service
    service.add("Primaria", "AIza-first-key-123456", 1, "admin")
    service.add("Respaldo", "AIza-second-key-123456", 2, "admin")
    import app.services.llm_service as llm_module

    monkeypatch.setattr(llm_module, "llm_key_service", service)

    def run_once(_):
        llm = LlmService()

        def fake_extract(message, start, end, api_key=None, group_memory=None, active_round_context=None, active_round_days=None):
            if api_key == "AIza-first-key-123456":
                raise GeminiApiError(429, "RESOURCE_EXHAUSTED", 30)
            return successful_result()

        llm._extract_with_gemini = fake_extract
        return llm._extract_with_gemini_pool("Ana puede lunes", 9, 18)[0]

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run_once, range(8)))
    assert all(item.participants[0].name == "Ana" for item in results)
    state = service.list_state()
    assert sum(item["request_count"] for item in state["keys"]) == 8
    assert sum(item["ttft_sample_count"] for item in state["keys"]) == 8
    assert sum(item["quota_exhaustion_count"] for item in state["keys"]) >= 1


def test_admin_api_requires_proxy_identity_and_never_returns_secret(tmp_path, monkeypatch):
    repository = JsonLlmKeyRepository(tmp_path / "api-keys.json")
    monkeypatch.setattr(key_module, "repository", repository)
    monkeypatch.setattr(settings, "llm_keys_master_key", master_key())
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "admin_proxy_header_required", True)
    client = TestClient(app)
    secret = "AIza-api-secret-123456789"

    assert client.get("/api/admin/llm-keys").status_code == 403
    created_response = client.post(
        "/api/admin/llm-keys",
        headers={"X-Coordina-Admin": "nicolas"},
        json={"name": "Produccion B", "secret": secret},
    )
    assert created_response.status_code == 200
    created = created_response.json()["key"]
    assert secret not in created_response.text
    assert created["priority"] == 10
    assert created["request_count"] == 0
    assert created["total_tokens"] == 0
    assert created["ttft_sample_count"] == 0
    assert created["last_ttft_ms"] is None

    listed = client.get(
        "/api/admin/llm-keys",
        headers={"X-Coordina-Admin": "nicolas"},
    )
    assert listed.status_code == 200
    assert secret not in listed.text
    assert "encrypted_secret" not in listed.text

    wrong_delete = client.request(
        "DELETE",
        f"/api/admin/llm-keys/{created['id']}",
        headers={"X-Coordina-Admin": "nicolas"},
        json={"confirm_name": "otra"},
    )
    assert wrong_delete.status_code == 409
    deleted = client.request(
        "DELETE",
        f"/api/admin/llm-keys/{created['id']}",
        headers={"X-Coordina-Admin": "nicolas"},
        json={"confirm_name": "Produccion B"},
    )
    assert deleted.status_code == 200
    assert secret not in repository.path.read_text(encoding="utf-8")
