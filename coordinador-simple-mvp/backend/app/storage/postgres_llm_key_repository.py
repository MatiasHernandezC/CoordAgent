import json

from sqlalchemy import create_engine, text

from app.settings import settings


class PostgresLlmKeyRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.engine = create_engine(database_url or settings.database_url, pool_pre_ping=True)
        self._tables_ready = False

    def _ensure_tables(self) -> None:
        if self._tables_ready:
            return
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS llm_keys (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS llm_key_audit (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        self._tables_ready = True

    def list_keys(self) -> list[dict]:
        self._ensure_tables()
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT data FROM llm_keys ORDER BY (data->>'priority')::INT, updated_at")
            ).fetchall()
        return [_as_dict(row[0]) for row in rows]

    def get_key(self, credential_id: str) -> dict | None:
        self._ensure_tables()
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT data FROM llm_keys WHERE id = :id"),
                {"id": credential_id},
            ).fetchone()
        return _as_dict(row[0]) if row else None

    def insert_key(self, record: dict) -> dict:
        self._ensure_tables()
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO llm_keys (id, data, updated_at)
                    VALUES (:id, CAST(:data AS JSONB), NOW())
                """),
                {"id": record["id"], "data": json.dumps(record, ensure_ascii=False)},
            )
        return dict(record)

    def update_key(self, credential_id: str, updates: dict) -> dict | None:
        self._ensure_tables()
        with self.engine.begin() as connection:
            row = connection.execute(
                text("SELECT data FROM llm_keys WHERE id = :id FOR UPDATE"),
                {"id": credential_id},
            ).fetchone()
            if row is None:
                return None
            updated = {**_as_dict(row[0]), **updates}
            connection.execute(
                text("UPDATE llm_keys SET data = CAST(:data AS JSONB), updated_at = NOW() WHERE id = :id"),
                {"id": credential_id, "data": json.dumps(updated, ensure_ascii=False)},
            )
        return updated

    def delete_key(self, credential_id: str) -> bool:
        self._ensure_tables()
        with self.engine.begin() as connection:
            result = connection.execute(
                text("DELETE FROM llm_keys WHERE id = :id"),
                {"id": credential_id},
            )
        return result.rowcount > 0

    def append_audit(self, record: dict) -> None:
        self._ensure_tables()
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO llm_key_audit (id, data, created_at)
                    VALUES (:id, CAST(:data AS JSONB), NOW())
                """),
                {"id": record["id"], "data": json.dumps(record, ensure_ascii=False)},
            )

    def list_audit(self, limit: int = 100) -> list[dict]:
        self._ensure_tables()
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT data FROM llm_key_audit ORDER BY created_at DESC LIMIT :limit"),
                {"limit": limit},
            ).fetchall()
        return [_as_dict(row[0]) for row in rows]


def _as_dict(value) -> dict:
    return json.loads(value) if isinstance(value, str) else dict(value)


repository = PostgresLlmKeyRepository()
