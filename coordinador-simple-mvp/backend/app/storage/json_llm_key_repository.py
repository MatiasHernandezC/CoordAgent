import json
import os
import threading
from json import JSONDecodeError
from pathlib import Path

from app.settings import settings


class JsonLlmKeyRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.llm_keys_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self.path.write_text('{"keys": [], "audit": []}', encoding="utf-8")

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (JSONDecodeError, OSError):
            data = {}
        return {
            "keys": list(data.get("keys") or []),
            "audit": list(data.get("audit") or []),
        }

    def _write(self, data: dict) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def list_keys(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._read()["keys"]]

    def get_key(self, credential_id: str) -> dict | None:
        with self._lock:
            return next(
                (dict(item) for item in self._read()["keys"] if item.get("id") == credential_id),
                None,
            )

    def insert_key(self, record: dict) -> dict:
        with self._lock:
            data = self._read()
            data["keys"].append(dict(record))
            self._write(data)
        return dict(record)

    def update_key(self, credential_id: str, updates: dict) -> dict | None:
        with self._lock:
            data = self._read()
            updated = None
            for index, item in enumerate(data["keys"]):
                if item.get("id") != credential_id:
                    continue
                updated = {**item, **updates}
                data["keys"][index] = updated
                break
            if updated is not None:
                self._write(data)
            return dict(updated) if updated is not None else None

    def delete_key(self, credential_id: str) -> bool:
        with self._lock:
            data = self._read()
            remaining = [item for item in data["keys"] if item.get("id") != credential_id]
            if len(remaining) == len(data["keys"]):
                return False
            data["keys"] = remaining
            self._write(data)
            return True

    def append_audit(self, record: dict) -> None:
        with self._lock:
            data = self._read()
            data["audit"].append(dict(record))
            data["audit"] = data["audit"][-500:]
            self._write(data)

    def list_audit(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(item) for item in reversed(self._read()["audit"][-limit:])]


repository = JsonLlmKeyRepository()
