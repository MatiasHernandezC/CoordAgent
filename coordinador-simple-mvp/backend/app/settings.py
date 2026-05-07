import os
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"

load_dotenv(dotenv_path=env_path, override=True)

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    data_file: Path = Path(os.getenv("DATA_FILE", "data/sessions.json"))
    llm_provider: str = os.getenv("LLM_PROVIDER", "local")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    local_llm_python: str = os.getenv(
        "LOCAL_LLM_PYTHON",
        str(WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"),
    )
    local_llm_script: Path = Path(os.getenv("LOCAL_LLM_SCRIPT", str(WORKSPACE_ROOT / "local_llm.py")))
    local_llm_model: str = os.getenv("LOCAL_LLM_MODEL", "qwen")
    local_llm_timeout_seconds: int = env_int("LOCAL_LLM_TIMEOUT_SECONDS", 180)
    local_llm_max_tokens: int = env_int("LOCAL_LLM_MAX_TOKENS", 700)
    llm_cache_enabled: bool = os.getenv("LLM_CACHE_ENABLED", "true").lower() == "true"
    llm_cache_max_items: int = env_int("LLM_CACHE_MAX_ITEMS", 64)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_url: str = os.getenv(
        "GEMINI_URL",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )
    gemini_timeout_seconds: int = env_int("GEMINI_TIMEOUT_SECONDS", 45)
    
    gemini_input_price_per_million: float = float(
        os.getenv("GEMINI_INPUT_PRICE_PER_MILLION", "0.10")
    )

    gemini_output_price_per_million: float = float(
        os.getenv("GEMINI_OUTPUT_PRICE_PER_MILLION", "0.40")
    )
    @property
    def cors_origins(self) -> list[str]:
        raw = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


settings = Settings()
