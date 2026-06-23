from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from librarry.db import Database, utcnow


PBKDF2_ROUNDS = 260_000


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    auth_type TEXT NOT NULL,
    issuer TEXT,
    subject TEXT,
    username TEXT,
    email TEXT,
    display_name TEXT,
    preferred_username TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_admin INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT,
    password_salt TEXT,
    database_path TEXT NOT NULL,
    setup_complete INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(issuer, subject),
    UNIQUE(username)
);

CREATE TABLE IF NOT EXISTS user_kindle_settings (
    user_id TEXT PRIMARY KEY,
    kindle_to TEXT NOT NULL DEFAULT '',
    send_kindle INTEGER NOT NULL DEFAULT 0,
    setup_complete INTEGER NOT NULL DEFAULT 0,
    last_test_status TEXT,
    last_test_at TEXT,
    last_send_status TEXT,
    last_send_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


@dataclass(frozen=True)
class User:
    id: str
    auth_type: str
    issuer: str | None
    subject: str | None
    username: str | None
    email: str
    display_name: str
    preferred_username: str
    enabled: bool
    is_admin: bool
    database_path: Path
    setup_complete: bool


@dataclass(frozen=True)
class KindleSettings:
    user_id: str
    kindle_to: str
    send_kindle: bool
    setup_complete: bool
    last_test_status: str
    last_test_at: str
    last_send_status: str
    last_send_at: str


class UserStore:
    def __init__(self, path: Path, users_dir: Path):
        self.path = path
        self.users_dir = users_dir
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.users_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _database_path(self, user_id: str) -> Path:
        return self.users_dir / user_id / "librarry.db"

    def _ensure_user_db(self, path: Path) -> None:
        db = Database(path)
        db.init()

    def _ensure_kindle_settings(self, conn: sqlite3.Connection, user_id: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO user_kindle_settings (
                user_id, kindle_to, send_kindle, setup_complete, updated_at
            ) VALUES (?, '', 0, 0, ?)
            """,
            (user_id, utcnow()),
        )

    def upsert_oidc_user(
        self,
        *,
        issuer: str,
        subject: str,
        email: str = "",
        display_name: str = "",
        preferred_username: str = "",
    ) -> User:
        issuer = issuer.strip().rstrip("/")
        subject = subject.strip()
        if not issuer or not subject:
            raise ValueError("issuer and subject are required")
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE issuer=? AND subject=?",
                (issuer, subject),
            ).fetchone()
            if row:
                user_id = row["id"]
                db_path = Path(row["database_path"])
                conn.execute(
                    """
                    UPDATE users SET
                        email=?, display_name=?, preferred_username=?, updated_at=?
                    WHERE id=?
                    """,
                    (email, display_name or preferred_username or email, preferred_username, now, user_id),
                )
            else:
                user_id = uuid.uuid4().hex
                db_path = self._database_path(user_id)
                conn.execute(
                    """
                    INSERT INTO users (
                        id, auth_type, issuer, subject, username, email, display_name,
                        preferred_username, enabled, is_admin, database_path,
                        setup_complete, created_at, updated_at
                    ) VALUES (?, 'oidc', ?, ?, NULL, ?, ?, ?, 1, 0, ?, 0, ?, ?)
                    """,
                    (
                        user_id,
                        issuer,
                        subject,
                        email,
                        display_name or preferred_username or email,
                        preferred_username,
                        str(db_path),
                        now,
                        now,
                    ),
                )
            self._ensure_kindle_settings(conn, user_id)
        self._ensure_user_db(db_path)
        user = self.get_user(user_id)
        if not user:
            raise RuntimeError("failed to create user")
        return user

    def upsert_local_admin(self, username: str, password: str) -> User:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        if not password:
            raise ValueError("password is required")
        salt = os.urandom(16).hex()
        digest = _hash_password(password, salt)
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if row:
                user_id = row["id"]
                db_path = Path(row["database_path"])
                conn.execute(
                    """
                    UPDATE users SET password_hash=?, password_salt=?,
                        enabled=1, is_admin=1, updated_at=?
                    WHERE id=?
                    """,
                    (digest, salt, now, user_id),
                )
            else:
                user_id = uuid.uuid4().hex
                db_path = self._database_path(user_id)
                conn.execute(
                    """
                    INSERT INTO users (
                        id, auth_type, issuer, subject, username, email, display_name,
                        preferred_username, enabled, is_admin, password_hash,
                        password_salt, database_path, setup_complete, created_at, updated_at
                    ) VALUES (?, 'local', NULL, NULL, ?, '', ?, '', 1, 1, ?, ?, ?, 1, ?, ?)
                    """,
                    (user_id, username, username, digest, salt, str(db_path), now, now),
                )
            self._ensure_kindle_settings(conn, user_id)
        self._ensure_user_db(db_path)
        user = self.get_user(user_id)
        if not user:
            raise RuntimeError("failed to create local admin")
        return user

    def verify_local_admin(self, username: str, password: str) -> User | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM users
                WHERE auth_type='local' AND username=? AND enabled=1 AND is_admin=1
                """,
                (username.strip(),),
            ).fetchone()
        if not row or not row["password_hash"] or not row["password_salt"]:
            return None
        digest = _hash_password(password, row["password_salt"])
        if not hmac.compare_digest(digest, row["password_hash"]):
            return None
        return _row_to_user(row)

    def get_user(self, user_id: str) -> User | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [_row_to_user(row) for row in rows]

    def set_user_enabled(self, user_id: str, enabled: bool) -> User:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, utcnow(), user_id),
            )
        user = self.get_user(user_id)
        if not user:
            raise KeyError(user_id)
        return user

    def get_kindle_settings(self, user_id: str) -> KindleSettings:
        with self.connect() as conn:
            self._ensure_kindle_settings(conn, user_id)
            row = conn.execute(
                "SELECT * FROM user_kindle_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return _row_to_kindle(row)

    def set_kindle_settings(
        self,
        user_id: str,
        *,
        kindle_to: str | None = None,
        send_kindle: bool | None = None,
        setup_complete: bool | None = None,
    ) -> KindleSettings:
        current = self.get_kindle_settings(user_id)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE user_kindle_settings SET
                    kindle_to=?, send_kindle=?, setup_complete=?, updated_at=?
                WHERE user_id=?
                """,
                (
                    current.kindle_to if kindle_to is None else kindle_to.strip(),
                    1 if (current.send_kindle if send_kindle is None else send_kindle) else 0,
                    1 if (current.setup_complete if setup_complete is None else setup_complete) else 0,
                    utcnow(),
                    user_id,
                ),
            )
            if setup_complete is not None:
                conn.execute(
                    "UPDATE users SET setup_complete=?, updated_at=? WHERE id=?",
                    (1 if setup_complete else 0, utcnow(), user_id),
            )
        return self.get_kindle_settings(user_id)

    def set_kindle_test_status(self, user_id: str, status: str) -> KindleSettings:
        with self.connect() as conn:
            self._ensure_kindle_settings(conn, user_id)
            conn.execute(
                """
                UPDATE user_kindle_settings
                SET last_test_status=?, last_test_at=?, updated_at=?
                WHERE user_id=?
                """,
                (status[:500], utcnow(), utcnow(), user_id),
            )
        return self.get_kindle_settings(user_id)


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        PBKDF2_ROUNDS,
    ).hex()


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        auth_type=row["auth_type"],
        issuer=row["issuer"],
        subject=row["subject"],
        username=row["username"],
        email=row["email"] or "",
        display_name=row["display_name"] or "",
        preferred_username=row["preferred_username"] or "",
        enabled=bool(row["enabled"]),
        is_admin=bool(row["is_admin"]),
        database_path=Path(row["database_path"]),
        setup_complete=bool(row["setup_complete"]),
    )


def _row_to_kindle(row: sqlite3.Row) -> KindleSettings:
    return KindleSettings(
        user_id=row["user_id"],
        kindle_to=row["kindle_to"] or "",
        send_kindle=bool(row["send_kindle"]),
        setup_complete=bool(row["setup_complete"]),
        last_test_status=row["last_test_status"] or "",
        last_test_at=row["last_test_at"] or "",
        last_send_status=row["last_send_status"] or "",
        last_send_at=row["last_send_at"] or "",
    )
