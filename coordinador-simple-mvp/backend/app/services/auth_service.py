from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.settings import settings


@dataclass(frozen=True)
class AuthUser:
    username: str
    display_name: str
    is_admin: bool = False
    role: str = "group_admin"
    active: bool = True
    created_at: str | None = None

    def __post_init__(self) -> None:
        if self.is_admin and self.role != "platform_admin":
            object.__setattr__(self, "role", "platform_admin")


class AuthService:
    iterations = 600_000

    def __init__(self, *, path: Path | None = None, database_url: str | None = None):
        self.path = path
        self.database_url = database_url
        self._lock = threading.RLock()
        self._engine = None
        self._ready = False

    @staticmethod
    def normalize_username(value: str) -> str:
        normalized = unicodedata.normalize("NFD", value.strip().lower())
        normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
        return re.sub(r"[^a-z0-9._-]+", ".", normalized).strip(".-_")

    def _using_postgres(self) -> bool:
        return self.path is None and (self.database_url is not None or settings.db_backend == "postgres")

    def _get_engine(self):
        if self._engine is None:
            self._engine = create_engine(self.database_url or settings.database_url, pool_pre_ping=True)
        return self._engine

    def _ensure_ready(self) -> None:
        with self._lock:
            if self._ready:
                return
            if self._using_postgres():
                with self._get_engine().begin() as connection:
                    connection.execute(text("""
                        CREATE TABLE IF NOT EXISTS app_users (
                            username TEXT PRIMARY KEY,
                            display_name TEXT NOT NULL,
                            is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                            role TEXT NOT NULL DEFAULT 'group_admin',
                            active BOOLEAN NOT NULL DEFAULT TRUE,
                            salt TEXT NOT NULL,
                            password_hash TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """))
                    connection.execute(text(
                        "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'group_admin'"
                    ))
                    connection.execute(text(
                        "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE"
                    ))
                    connection.execute(text(
                        "UPDATE app_users SET role = 'platform_admin' WHERE is_admin = TRUE AND role <> 'platform_admin'"
                    ))
            else:
                target = self.path or settings.users_file
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    self._write_json({})
            self._ready = True
            self._bootstrap_users()

    def _read_json(self) -> dict:
        target = self.path or settings.users_file
        try:
            value = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_json(self, value: dict) -> None:
        target = self.path or settings.users_file
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def _hash_password(self, password: str, salt: bytes | None = None) -> tuple[str, str]:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, self.iterations)
        return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")

    def _verify_password(self, password: str, record: dict) -> bool:
        try:
            salt = base64.b64decode(record["salt"], validate=True)
            expected = base64.b64decode(record["password_hash"], validate=True)
        except (KeyError, TypeError, ValueError):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, self.iterations)
        return hmac.compare_digest(actual, expected)

    def _bootstrap_users(self) -> None:
        if self.list_users(_skip_ready=True):
            return
        password = settings.panel_admin_password
        if password:
            if settings.app_env == "production" and len(password) < 12:
                raise RuntimeError("PANEL_ADMIN_PASSWORD debe tener al menos 12 caracteres en produccion.")
            self.create_user(
                settings.panel_admin_username,
                settings.panel_admin_display_name,
                password,
                is_admin=True,
                _skip_ready=True,
            )
        if settings.app_env != "production" and settings.panel_seed_demo_users:
            if not self.get_user("admin", _skip_ready=True):
                self.create_user("admin", "Administrador", "admin-local", is_admin=True, _skip_ready=True)
            if not self.get_user("usuario", _skip_ready=True):
                self.create_user("usuario", "Usuario de prueba", "usuario-local", _skip_ready=True)

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        *,
        is_admin: bool = False,
        role: str = "group_admin",
        _skip_ready: bool = False,
    ) -> AuthUser:
        if not _skip_ready:
            self._ensure_ready()
        clean_username = self.normalize_username(username)
        clean_name = display_name.strip()
        if len(clean_username) < 2:
            raise HTTPException(status_code=400, detail="El usuario debe contener al menos dos caracteres validos.")
        if len(clean_name) < 2:
            raise HTTPException(status_code=400, detail="El nombre visible es demasiado corto.")
        if len(password) < 10:
            raise HTTPException(status_code=400, detail="La contrasena debe tener al menos 10 caracteres.")
        clean_role = "platform_admin" if is_admin else role.strip().lower()
        if clean_role not in {"platform_admin", "group_admin"}:
            raise HTTPException(status_code=400, detail="Rol de usuario invalido.")
        is_admin = clean_role == "platform_admin"
        salt, password_hash = self._hash_password(password)
        created_at = datetime.now(timezone.utc).isoformat()
        if self._using_postgres():
            try:
                with self._get_engine().begin() as connection:
                    connection.execute(
                        text("""
                            INSERT INTO app_users (username, display_name, is_admin, role, salt, password_hash)
                            VALUES (:username, :display_name, :is_admin, :role, :salt, :password_hash)
                        """),
                        {
                            "username": clean_username,
                            "display_name": clean_name,
                            "is_admin": is_admin,
                            "role": clean_role,
                            "salt": salt,
                            "password_hash": password_hash,
                        },
                    )
            except IntegrityError as exc:
                raise HTTPException(status_code=409, detail="Ese usuario ya existe.") from exc
        else:
            with self._lock:
                users = self._read_json()
                if clean_username in users:
                    raise HTTPException(status_code=409, detail="Ese usuario ya existe.")
                users[clean_username] = {
                    "display_name": clean_name,
                    "is_admin": is_admin,
                    "role": clean_role,
                    "salt": salt,
                    "password_hash": password_hash,
                    "active": True,
                    "created_at": created_at,
                }
                self._write_json(users)
        return self.get_user(clean_username, _skip_ready=True) or AuthUser(
            clean_username,
            clean_name,
            is_admin,
            clean_role,
            True,
            created_at,
        )

    def _record(self, username: str) -> dict | None:
        key = self.normalize_username(username)
        if self._using_postgres():
            with self._get_engine().connect() as connection:
                row = connection.execute(
                    text("SELECT username, display_name, is_admin, role, active, salt, password_hash, created_at FROM app_users WHERE username = :username"),
                    {"username": key},
                ).mappings().first()
            return dict(row) if row else None
        record = self._read_json().get(key)
        return {"username": key, **record} if isinstance(record, dict) else None

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        self._ensure_ready()
        record = self._record(username)
        if not record or not bool(record.get("active", True)) or not self._verify_password(password, record):
            return None
        return self._user_from_record(record)

    def get_user(self, username: str, *, _skip_ready: bool = False) -> AuthUser | None:
        if not _skip_ready:
            self._ensure_ready()
        record = self._record(username)
        if not record:
            return None
        return self._user_from_record(record)

    def list_users(self, *, _skip_ready: bool = False) -> list[AuthUser]:
        if not _skip_ready:
            self._ensure_ready()
        if self._using_postgres():
            with self._get_engine().connect() as connection:
                rows = connection.execute(
                    text("SELECT username, display_name, is_admin, role, active, created_at FROM app_users ORDER BY username")
                ).mappings().all()
            return [self._user_from_record(dict(row)) for row in rows]
        return [
            self._user_from_record({"username": username, **record})
            for username, record in sorted(self._read_json().items())
            if isinstance(record, dict)
        ]

    @staticmethod
    def _user_from_record(record: dict) -> AuthUser:
        is_admin = bool(record.get("is_admin"))
        role = str(record.get("role") or ("platform_admin" if is_admin else "group_admin"))
        created_at = record.get("created_at")
        if created_at is not None and hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        return AuthUser(
            str(record["username"]),
            str(record.get("display_name") or record["username"]),
            is_admin,
            role,
            bool(record.get("active", True)),
            str(created_at) if created_at else None,
        )

    def update_user(
        self,
        username: str,
        *,
        display_name: str | None = None,
        active: bool | None = None,
        actor_username: str | None = None,
    ) -> AuthUser:
        self._ensure_ready()
        key = self.normalize_username(username)
        record = self._record(key)
        if not record:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        clean_name = display_name.strip() if display_name is not None else None
        if clean_name is not None and len(clean_name) < 2:
            raise HTTPException(status_code=400, detail="El nombre visible es demasiado corto.")
        if active is False and key == self.normalize_username(actor_username or ""):
            raise HTTPException(status_code=400, detail="No puedes desactivar tu propia cuenta.")

        if self._using_postgres():
            with self._get_engine().begin() as connection:
                if active is False and bool(record.get("is_admin")) and bool(record.get("active", True)):
                    active_admins = connection.execute(text(
                        "SELECT COUNT(*) FROM app_users WHERE is_admin = TRUE AND active = TRUE"
                    )).scalar_one()
                    if active_admins <= 1:
                        raise HTTPException(status_code=409, detail="Debe quedar al menos un administrador activo.")
                if clean_name is not None:
                    connection.execute(
                        text("UPDATE app_users SET display_name = :display_name WHERE username = :username"),
                        {"username": key, "display_name": clean_name},
                    )
                if active is not None:
                    connection.execute(
                        text("UPDATE app_users SET active = :active WHERE username = :username"),
                        {"username": key, "active": active},
                    )
        else:
            with self._lock:
                users = self._read_json()
                stored = users.get(key)
                if not isinstance(stored, dict):
                    raise HTTPException(status_code=404, detail="Usuario no encontrado.")
                if active is False and bool(stored.get("is_admin")) and bool(stored.get("active", True)):
                    active_admins = sum(
                        1
                        for value in users.values()
                        if isinstance(value, dict) and value.get("is_admin") and value.get("active", True)
                    )
                    if active_admins <= 1:
                        raise HTTPException(status_code=409, detail="Debe quedar al menos un administrador activo.")
                if clean_name is not None:
                    stored["display_name"] = clean_name
                if active is not None:
                    stored["active"] = active
                users[key] = stored
                self._write_json(users)
        return self.get_user(key)  # type: ignore[return-value]

    def reset_password(self, username: str, password: str) -> AuthUser:
        self._ensure_ready()
        key = self.normalize_username(username)
        if len(password) < 10:
            raise HTTPException(status_code=400, detail="La contrasena debe tener al menos 10 caracteres.")
        if not self._record(key):
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")
        salt, password_hash = self._hash_password(password)
        if self._using_postgres():
            with self._get_engine().begin() as connection:
                connection.execute(
                    text("UPDATE app_users SET salt = :salt, password_hash = :password_hash WHERE username = :username"),
                    {"username": key, "salt": salt, "password_hash": password_hash},
                )
        else:
            with self._lock:
                users = self._read_json()
                stored = users.get(key)
                if not isinstance(stored, dict):
                    raise HTTPException(status_code=404, detail="Usuario no encontrado.")
                stored["salt"] = salt
                stored["password_hash"] = password_hash
                users[key] = stored
                self._write_json(users)
        return self.get_user(key)  # type: ignore[return-value]


auth_service = AuthService()


def authenticate_request(request: Request) -> AuthUser | None:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    return auth_service.authenticate(username, password)


def current_user(request: Request) -> AuthUser:
    user = getattr(request.state, "auth_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Debes iniciar sesion.")
    return user


def require_admin_user(request: Request) -> AuthUser:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Esta funcion requiere una cuenta administradora.")
    return user
