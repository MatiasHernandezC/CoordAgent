from pathlib import Path

from app.schemas import Session
from app.storage.json_repository import JsonRepository


def test_repository_reads_utf8_bom_file(tmp_path: Path):
    data_file = tmp_path / "sessions.json"
    data_file.write_text("\ufeff{}", encoding="utf-8")

    repository = JsonRepository(data_file)
    session = repository.save(Session(title="Reunion con BOM"))

    assert repository.get(session.id) is not None
