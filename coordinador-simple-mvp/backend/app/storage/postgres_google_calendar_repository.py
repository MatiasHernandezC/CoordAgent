import json

from sqlalchemy import create_engine, text

from app.settings import settings


class PostgresGoogleCalendarRepository:
    """Almacena una unica credencial (OAuth a nivel organizacion, no por usuario)."""

    def __init__(self, database_url: str | None = None) -> None:
        self.engine = create_engine(database_url or settings.database_url, pool_pre_ping=True)
        self._tables_ready = False

    def _ensure_tables(self) -> None:
        if self._tables_ready:
            return
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS google_calendar_credential (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        self._tables_ready = True

    def get(self) -> dict | None:
        self._ensure_tables()
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT data FROM google_calendar_credential WHERE id = 'default'")
            ).fetchone()
        return _as_dict(row[0]) if row else None

    def save(self, record: dict) -> dict:
        self._ensure_tables()
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO google_calendar_credential (id, data, updated_at)
                    VALUES ('default', CAST(:data AS JSONB), NOW())
                    ON CONFLICT (id) DO UPDATE SET data = CAST(:data AS JSONB), updated_at = NOW()
                """),
                {"data": json.dumps(record, ensure_ascii=False)},
            )
        return dict(record)

    def clear(self) -> None:
        self._ensure_tables()
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM google_calendar_credential WHERE id = 'default'"))


def _as_dict(value) -> dict:
    return json.loads(value) if isinstance(value, str) else dict(value)


repository = PostgresGoogleCalendarRepository()
