import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)


def find_workspace_root(settings_file: Path) -> Path:
    for parent in settings_file.parents:
        if (parent / "local_llm.py").exists() and (parent / "models").exists():
            return parent
    # Fallback tolerante a la profundidad de la ruta: en un contenedor (/app/app/...)
    # no hay 5 niveles y parents[4] reventaria. Solo importa cuando LLM_PROVIDER=local.
    parents = settings_file.parents
    return parents[4] if len(parents) > 4 else parents[-1]


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
    app_timezone: str = os.getenv("APP_TIMEZONE", "America/Santiago")

    # Base de datos
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://user:password@localhost:5432/meetingdb",
    )

    # Selector de almacenamiento: "postgres" (default) | "json".
    # Con "json" la app corre sin Postgres usando data/sessions.json.
    db_backend: str = os.getenv("DB_BACKEND", "postgres").strip().lower()

    # Estado interno del puente WhatsApp (solo red Docker; no se publica).
    gateway_status_url: str = os.getenv("GATEWAY_STATUS_URL", "http://gateway:8080/status")
    gateway_status_timeout_seconds: float = env_float("GATEWAY_STATUS_TIMEOUT_SECONDS", 2.0)

    # Almacenamiento legacy (conservado por si necesitas rollback)
    data_file: Path = Path(os.getenv("DATA_FILE", "data/sessions.json"))

    # Acceso al panel. En produccion no existe ninguna clave por defecto: el
    # primer administrador se crea desde estas variables y la contrasena solo
    # se conserva como hash. En desarrollo el sembrado demo es opt-in.
    panel_auth_required: bool = env_bool("PANEL_AUTH_REQUIRED", app_env == "production")
    panel_admin_username: str = os.getenv("PANEL_ADMIN_USERNAME", "admin").strip()
    panel_admin_display_name: str = os.getenv("PANEL_ADMIN_DISPLAY_NAME", "Administrador").strip()
    panel_admin_password: str = os.getenv("PANEL_ADMIN_PASSWORD", "")
    panel_seed_demo_users: bool = env_bool("PANEL_SEED_DEMO_USERS", False)
    panel_self_registration_enabled: bool = env_bool("PANEL_SELF_REGISTRATION_ENABLED", True)
    users_file: Path = Path(os.getenv("USERS_FILE", "data/users.json"))

    # Proteccion basica para una instancia publica de demostracion. Se aplica
    # por proceso; Cloudflare sigue siendo la primera barrera ante trafico hostil.
    rate_limit_enabled: bool = env_bool("RATE_LIMIT_ENABLED", app_env == "production")
    rate_limit_per_minute: int = env_int("RATE_LIMIT_PER_MINUTE", 120)
    rate_limit_global_per_minute: int = env_int("RATE_LIMIT_GLOBAL_PER_MINUTE", 600)
    rate_limit_auth_per_minute: int = env_int("RATE_LIMIT_AUTH_PER_MINUTE", 20)
    rate_limit_register_per_hour: int = env_int("RATE_LIMIT_REGISTER_PER_HOUR", 5)
    max_request_body_bytes: int = env_int("MAX_REQUEST_BODY_BYTES", 1_000_000)

    # Secreto compartido exclusivamente dentro de la red Docker para que el
    # gateway de WhatsApp pueda sincronizar grupos sin usar una cuenta humana.
    gateway_api_token: str = os.getenv("GATEWAY_API_TOKEN", "")

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

    # Horario habitual ("a la hora de siempre"): minimo de decisiones confirmadas
    # necesarias para que la sesion tenga un horario habitual computable.
    habitual_min_decisions: int = env_int("HABITUAL_MIN_DECISIONS", 1)

    # Fallback
    llm_fallback_enabled: bool = env_bool("LLM_FALLBACK_ENABLED", True)

    # Gemini
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    # Si generateContent del modelo principal devuelve 404 (p. ej. "no longer
    # available to new users"), se reintenta una vez con este modelo en la
    # misma llave. Vacio desactiva el fallback de modelo.
    gemini_model_fallback: str = os.getenv(
        "GEMINI_MODEL_FALLBACK",
        "gemini-flash-lite-latest",
    )
    gemini_url: str = os.getenv(
        "GEMINI_URL",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )
    gemini_streaming_enabled: bool = env_bool("GEMINI_STREAMING_ENABLED", True)
    gemini_timeout_seconds: int = env_int("GEMINI_TIMEOUT_SECONDS", 45)
    gemini_cooldown_seconds: int = env_int("GEMINI_COOLDOWN_SECONDS", 60)
    gemini_input_price_per_million: float = env_float("GEMINI_INPUT_PRICE_PER_MILLION", 0.10)
    gemini_output_price_per_million: float = env_float("GEMINI_OUTPUT_PRICE_PER_MILLION", 0.40)

    # Llaves Gemini administradas desde el panel. La llave maestra nunca se
    # persiste junto a los secretos cifrados.
    llm_keys_master_key: str = os.getenv("LLM_KEYS_MASTER_KEY", "")
    llm_keys_file: Path = Path(os.getenv("LLM_KEYS_FILE", "data/llm_keys.json"))
    admin_proxy_header_required: bool = env_bool(
        "ADMIN_PROXY_HEADER_REQUIRED",
        app_env == "production",
    )
    # Canal Slack (opcional, ademas de WhatsApp). Sin bot token el canal queda
    # desactivado: el endpoint responde 503 en vez de fallar silenciosamente.
    slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
    slack_signing_secret: str = os.getenv("SLACK_SIGNING_SECRET", "")
    slack_trigger_word: str = os.getenv("SLACK_TRIGGER_WORD", "@coordina")
    slack_request_max_age_seconds: int = env_int("SLACK_REQUEST_MAX_AGE_SECONDS", 300)

    # Integracion opcional con Google Calendar: OAuth a nivel administrador
    # (una sola cuenta conectada desde el panel), no por participante. Usa la
    # misma llave maestra que las llaves Gemini para cifrar el refresh token.
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_oauth_redirect_uri: str = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "")
    google_calendar_credential_file: Path = Path(
        os.getenv("GOOGLE_CALENDAR_CREDENTIAL_FILE", "data/google_calendar_credential.json")
    )

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_bot_token and self.slack_signing_secret)

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_oauth_redirect_uri)

    @property
    def cors_origins(self) -> list[str]:
        raw = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def runtime_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.llm_provider == "gemini":
            if not self.gemini_api_key and not self.llm_keys_master_key:
                warnings.append("Gemini esta seleccionado, pero no hay llaves configuradas.")
            if not self.llm_keys_master_key:
                warnings.append(
                    "La administracion de llaves esta desactivada hasta definir LLM_KEYS_MASTER_KEY."
                )
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
