import json
from json import JSONDecodeError
from pathlib import Path

from app.schemas import Session


class JsonRepository:
    def __init__(self, path: Path | None = None):
        self.path = path or Path("data/sessions.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self, session: Session) -> Session:
        data = self._read()
        data[session.id] = session.model_dump()
        self._write(data)
        return session

    def get(self, session_id: str) -> Session | None:
        raw = self._read().get(session_id)
        if raw is None:
            return None
        return Session.model_validate(raw)
