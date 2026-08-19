import json
import os
import threading
from json import JSONDecodeError
from pathlib import Path

from app.settings import settings


class JsonAdminDirectoryRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.admin_directory_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self.path.write_text('{"admins": []}', encoding="utf-8")

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (JSONDecodeError, OSError):
            data = {}
        return {"admins": list(data.get("admins") or [])}

    def _write(self, data: dict) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def list_all(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._read()["admins"]]

    def get(self, actor: str) -> dict | None:
        with self._lock:
            return next(
                (dict(item) for item in self._read()["admins"] if item.get("actor") == actor),
                None,
            )

    def upsert(self, record: dict) -> dict:
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["admins"]):
                if item.get("actor") == record["actor"]:
                    data["admins"][index] = record
                    self._write(data)
                    return dict(record)
            data["admins"].append(record)
            self._write(data)
            return dict(record)


repository = JsonAdminDirectoryRepository()
