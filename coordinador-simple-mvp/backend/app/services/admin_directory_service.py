from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock

from app.settings import settings

if settings.db_backend == "json":
    from app.storage.json_admin_directory_repository import repository
else:
    from app.storage.postgres_admin_directory_repository import repository

TOUCH_THROTTLE = timedelta(minutes=5)


class AdminDirectoryService:
    def __init__(self) -> None:
        self._lock = RLock()

    def touch(self, actor: str) -> None:
        """Registra actividad de un admin autenticado. Nuevo -> crea el
        registro; conocido -> solo actualiza last_seen_at, y no mas seguido
        que TOUCH_THROTTLE para no escribir en cada request."""
        with self._lock:
            now = _now_iso()
            record = repository.get(actor)
            if record is None:
                repository.upsert({
                    "actor": actor,
                    "is_superadmin": False,
                    "first_seen_at": now,
                    "last_seen_at": now,
                })
                return
            last_seen = _parse(record.get("last_seen_at"))
            if last_seen and datetime.now(timezone.utc) - last_seen < TOUCH_THROTTLE:
                return
            repository.upsert({**record, "last_seen_at": now})

    def list_all(self) -> list[dict]:
        with self._lock:
            return repository.list_all()

    def get(self, actor: str) -> dict | None:
        with self._lock:
            return repository.get(actor)

    def set_superadmin(self, actor: str, is_superadmin: bool) -> dict:
        with self._lock:
            record = repository.get(actor) or {
                "actor": actor,
                "is_superadmin": False,
                "first_seen_at": None,
                "last_seen_at": None,
            }
            record = {**record, "is_superadmin": is_superadmin}
            return repository.upsert(record)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


admin_directory_service = AdminDirectoryService()
