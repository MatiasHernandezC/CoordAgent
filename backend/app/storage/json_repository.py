import json
import os
import threading
from json import JSONDecodeError
from pathlib import Path

from app.schemas import Session
from app.settings import settings


class JsonRepository:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.data_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serializa el read-modify-write: FastAPI ejecuta los endpoints sync en un
        # threadpool, asi que dos requests concurrentes podrian pisarse sin este lock.
        self._lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _read(self) -> dict:
        raw = self.path.read_text(encoding="utf-8-sig").strip()
        if not raw:
            return {}

        try:
            data = json.loads(raw)
        except JSONDecodeError:
            self._write({})
            return {}

        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        # Escritura atomica: se escribe un temporal y se reemplaza de una sola vez,
        # asi un corte a mitad de escritura no deja el JSON corrupto.
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, session: Session) -> Session:
        with self._lock:
            data = self._read()
            data[session.id] = session.model_dump()
            self._write(data)
        return session

    def get(self, session_id: str) -> Session | None:
        raw = self._read().get(session_id)
        if raw is None:
            return None
        return Session.model_validate(raw)

    def list_all(self) -> list[Session]:
        data = self._read()
        return [Session.model_validate(raw) for raw in reversed(list(data.values()))]

    def healthcheck(self) -> None:
        """Valida que el archivo local siga siendo legible."""
        self._read()


repository = JsonRepository()
