import json

from sqlalchemy import create_engine, text

from app.settings import settings


class PostgresAdminDirectoryRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.engine = create_engine(database_url or settings.database_url, pool_pre_ping=True)
        self._tables_ready = False

    def _ensure_tables(self) -> None:
        if self._tables_ready:
            return
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS admin_directory (
                    actor TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        self._tables_ready = True

    def list_all(self) -> list[dict]:
        self._ensure_tables()
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT data FROM admin_directory ORDER BY actor")).fetchall()
        return [_as_dict(row[0]) for row in rows]

    def get(self, actor: str) -> dict | None:
        self._ensure_tables()
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT data FROM admin_directory WHERE actor = :actor"),
                {"actor": actor},
            ).fetchone()
        return _as_dict(row[0]) if row else None

    def upsert(self, record: dict) -> dict:
        self._ensure_tables()
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO admin_directory (actor, data, updated_at)
                    VALUES (:actor, CAST(:data AS JSONB), NOW())
                    ON CONFLICT (actor) DO UPDATE SET data = CAST(:data AS JSONB), updated_at = NOW()
                """),
                {"actor": record["actor"], "data": json.dumps(record, ensure_ascii=False)},
            )
        return dict(record)


def _as_dict(value) -> dict:
    return json.loads(value) if isinstance(value, str) else dict(value)


repository = PostgresAdminDirectoryRepository()
