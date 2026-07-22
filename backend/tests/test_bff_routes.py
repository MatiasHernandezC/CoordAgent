import pytest
from fastapi import HTTPException

from app.bff.routes import raise_llm_http_error
from app.services.llm_service import LlmUnavailableError


def test_llm_unavailable_error_is_mapped_to_http_error():
    with pytest.raises(HTTPException) as error:
        raise_llm_http_error(
            LlmUnavailableError(
                "Gemini rechazo la solicitud por cuota.",
                status_code=429,
                retry_after_seconds=31,
            )
        )

    assert error.value.status_code == 429
    assert error.value.detail == "Gemini rechazo la solicitud por cuota."
    assert error.value.headers == {"Retry-After": "31"}
