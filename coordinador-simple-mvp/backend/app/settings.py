import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)


def find_workspace_root(settings_file: Path) -> Path:
    for parent in settings_file.parents:
        if (parent / "local_llm.py").exists() and (parent / "models").exists():
            return parent
    return settings_file.parents[4]


WORKSPACE_ROOT = find_workspace_root(Path(__file__).resolve())


# ── Helpers de conversión ─────────────────────────────────────────────────────

def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# ── Settings ──────────────────────────────────────────────────────────────────

class Settings:
    # Entorno
    app_env: str = os.getenv("APP_ENV", "development")

    # Base de datos
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://user:password@localhost:5432/meetingdb",
    )

    # Almacenamiento legacy (conservado por si necesitas rollback)
    data_file: Path = Path(os.getenv("DATA_FILE", "data/sessions.json"))

    # LLM provider
    llm_provider: str = os.getenv("LLM_PROVIDER", "local")

    # Ollama
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")

    # LLM local
    local_llm_python: str = os.getenv(
        "LOCAL_LLM_PYTHON",
        str(WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"),
    )
    local_llm_script: Path = Path(
        os.getenv("LOCAL_LLM_SCRIPT", str(WORKSPACE_ROOT / "local_llm.py"))
    )
    local_llm_model: str = os.getenv("LOCAL_LLM_MODEL", "qwen")
    local_llm_timeout_seconds: int = env_int("LOCAL_LLM_TIMEOUT_SECONDS", 180)
    local_llm_max_tokens: int = env_int("LOCAL_LLM_MAX_TOKENS", 700)

    # Cache
    llm_cache_enabled: bool = env_bool("LLM_CACHE_ENABLED", True)
    llm_cache_max_items: int = env_int("LLM_CACHE_MAX_ITEMS", 64)

    # Fallback
    llm_fallback_enabled: bool = env_bool("LLM_FALLBACK_ENABLED", True)

    # Gemini
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    gemini_url: str = os.getenv(
        "GEMINI_URL",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )
    gemini_timeout_seconds: int = env_int("GEMINI_TIMEOUT_SECONDS", 45)
    gemini_cooldown_seconds: int = env_int("GEMINI_COOLDOWN_SECONDS", 60)
    gemini_input_price_per_million: float = env_float("GEMINI_INPUT_PRICE_PER_MILLION", 0.10)
    gemini_output_price_per_million: float = env_float("GEMINI_OUTPUT_PRICE_PER_MILLION", 0.40)

    @property
    def cors_origins(self) -> list[str]:
        raw = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def runtime_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.llm_provider == "gemini":
            if not self.gemini_api_key:
                warnings.append("Gemini esta seleccionado, pero falta GEMINI_API_KEY.")
            if self.gemini_model == "gemini-3.0-flash":
                warnings.append(
                    "gemini-3.0-flash no esta disponible en v1beta/generateContent; "
                    "usa gemini-2.5-flash-lite."
                )
            if not self.llm_fallback_enabled:
                warnings.append(
                    "Fallback desactivado: si Gemini falla, la app mostrara error en vez de usar mock."
                )
        return warnings


settings = Settings()