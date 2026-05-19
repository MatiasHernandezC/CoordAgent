from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import json

from app.schemas import Session
from app.settings import settings


class PostgresRepository:
    def __init__(self, database_url: str | None = None):
        url = database_url or settings.database_url
        self.engine = create_engine(url, pool_pre_ping=True)
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Crea la tabla si no existe. Sin migraciones externas para mantenerlo simple."""
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id          TEXT PRIMARY KEY,
                    data        JSONB NOT NULL,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))

    def save(self, session: Session) -> Session:
        payload = json.dumps(session.model_dump(), ensure_ascii=False, default=str)
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO sessions (id, data, updated_at)
                    VALUES (:id, CAST(:data AS JSONB), NOW())
                    ON CONFLICT (id) DO UPDATE
                        SET data       = EXCLUDED.data,
                            updated_at = NOW()
                """),
                {"id": session.id, "data": payload},
            )
        return session

    def get(self, session_id: str) -> Session | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM sessions WHERE id = :id"),
                {"id": session_id},
            ).fetchone()

        if row is None:
            return None

        raw = row[0]  # psycopg2 deserializa JSONB automáticamente a dict
        if isinstance(raw, str):
            raw = json.loads(raw)

        return Session.model_validate(raw)

    def list_all(self) -> list[Session]:
        """Útil para admin/debug — no lo tenías en JSON pero es gratis aquí."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT data FROM sessions ORDER BY updated_at DESC")
            ).fetchall()
        return [Session.model_validate(row[0]) for row in rows]

    def delete(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
        return result.rowcount > 0


repository = PostgresRepository()