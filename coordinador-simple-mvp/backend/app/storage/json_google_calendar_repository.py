import json
import os
import threading
from json import JSONDecodeError
from pathlib import Path

from app.settings import settings


class JsonGoogleCalendarRepository:
    """Almacena una unica credencial (OAuth a nivel organizacion, no por usuario)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.google_calendar_credential_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def get(self) -> dict | None:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (JSONDecodeError, OSError):
                return None
            return dict(data) if data else None

    def save(self, record: dict) -> dict:
        with self._lock:
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            return dict(record)

    def clear(self) -> None:
        with self._lock:
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text("{}", encoding="utf-8")
            os.replace(temporary, self.path)


repository = JsonGoogleCalendarRepository()
