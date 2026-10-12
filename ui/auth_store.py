"""Durable accounts, opaque sessions and resource ownership for the web API."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

SESSION_SECONDS = 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000
COOKIE_NAME = "ship_agent_session"


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (TypeError, ValueError):
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, *, backend: str, sqlite_path: str | Path | None = None,
                 database_url: str | None = None):
        self.backend = backend
        self.sqlite_path = Path(sqlite_path or os.getenv("SHIP_AUTH_SQLITE_PATH", "var/auth.sqlite3"))
        self.database_url = database_url or os.getenv("SHIP_AUTH_DATABASE_URL") or os.getenv("SHIP_CHECKPOINT_DATABASE_URL")
        if backend not in ("sqlite", "postgres"):
            raise ValueError("auth storage requires sqlite or postgres")

    @contextmanager
    def _connect(self):
        if self.backend == "sqlite":
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.sqlite_path, timeout=10)
            connection.execute("PRAGMA foreign_keys=ON")
        else:
            if not self.database_url:
                raise RuntimeError("SHIP_AUTH_DATABASE_URL or SHIP_CHECKPOINT_DATABASE_URL is required")
            import psycopg
            connection = psycopg.connect(self.database_url, connect_timeout=5)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _sql(self, query: str) -> str:
        return query if self.backend == "sqlite" else query.replace("?", "%s")

    def _execute(self, connection, query: str, args=()):
        return connection.execute(self._sql(query), args)

    def ensure_schema(self) -> None:
        with self._connect() as db:
            self._execute(db, """CREATE TABLE IF NOT EXISTS auth_users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, role TEXT NOT NULL,
                password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""")
            self._execute(db, """CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                csrf_token TEXT NOT NULL, expires_at INTEGER NOT NULL)""")
            self._execute(db, """CREATE TABLE IF NOT EXISTS auth_failures (
                login_key TEXT PRIMARY KEY, count INTEGER NOT NULL, window_started INTEGER NOT NULL)""")
            self._execute(db, """CREATE TABLE IF NOT EXISTS auth_workflows (
                thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL)""")
            self._execute(db, """CREATE TABLE IF NOT EXISTS auth_voice_sessions (
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at INTEGER NOT NULL)""")

    def create_user(self, username: str, display_name: str, role: str, password: str) -> str:
        if not (1 <= len(username) <= 64 and username.isascii() and
                all(ch.isalnum() or ch in "._-" for ch in username) and
                1 <= len(display_name) <= 100 and role in ("user", "admin") and
                12 <= len(password) <= 1024):
            raise ValueError("invalid account fields or password (minimum 12 characters)")
        user_id = str(uuid4())
        with self._connect() as db:
            self._execute(db, "INSERT INTO auth_users VALUES (?, ?, ?, ?, ?, 1)",
                          (user_id, username.lower(), display_name, role, password_hash(password)))
        return user_id

    def authenticate(self, username: str, password: str, login_key: str):
        """Return (user, throttled) with a uniform password path for missing users."""
        now = int(time.time())
        with self._connect() as db:
            row = self._execute(db, "SELECT count, window_started FROM auth_failures WHERE login_key=?",
                                (login_key,)).fetchone()
            if row and row[1] > now - 900 and row[0] >= 5:
                return None, True
            user = self._execute(db, """SELECT id, username, display_name, role, password_hash
                FROM auth_users WHERE username=? AND active=1""", (username.lower(),)).fetchone()
            # A real PBKDF2 operation also runs when the username is unknown.
            valid = verify_password(password, user[4] if user else _DUMMY_HASH)
            if user and valid:
                self._execute(db, "DELETE FROM auth_failures WHERE login_key=?", (login_key,))
                return dict(zip(("id", "username", "display_name", "role"), user[:4])), False
            self._execute(db, """INSERT INTO auth_failures VALUES (?, 1, ?)
                ON CONFLICT(login_key) DO UPDATE SET
                count=CASE WHEN auth_failures.window_started > ?
                    THEN auth_failures.count + 1 ELSE 1 END,
                window_started=CASE WHEN auth_failures.window_started > ?
                    THEN auth_failures.window_started ELSE excluded.window_started END""",
                (login_key, now, now - 900, now - 900))
            attempts = self._execute(db, "SELECT count FROM auth_failures WHERE login_key=?",
                                     (login_key,)).fetchone()[0]
            return None, attempts > 5

    def create_session(self, user_id: str, old_token: str | None = None):
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        expires = int(time.time()) + SESSION_SECONDS
        with self._connect() as db:
            if old_token:
                self._execute(db, "DELETE FROM auth_sessions WHERE token_hash=?", (_digest(old_token),))
            self._execute(db, "INSERT INTO auth_sessions VALUES (?, ?, ?, ?)",
                          (_digest(token), user_id, csrf, expires))
        return token, csrf, expires

    def get_session(self, token: str | None):
        if not token:
            return None
        with self._connect() as db:
            row = self._execute(db, """SELECT u.id, u.username, u.display_name, u.role,
                s.csrf_token, s.expires_at FROM auth_sessions s
                JOIN auth_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
                (_digest(token), int(time.time()))).fetchone()
        if not row:
            return None
        return {**dict(zip(("id", "username", "display_name", "role"), row[:4])),
                "csrf_token": row[4], "expires_at": row[5]}

    def revoke_session(self, token: str | None) -> None:
        if token:
            with self._connect() as db:
                self._execute(db, "DELETE FROM auth_sessions WHERE token_hash=?", (_digest(token),))

    def bind_workflow(self, thread_id: str, user_id: str) -> None:
        with self._connect() as db:
            self._execute(db, "INSERT INTO auth_workflows VALUES (?, ?)", (thread_id, user_id))

    def owns_workflow(self, thread_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = self._execute(db, "SELECT 1 FROM auth_workflows WHERE thread_id=? AND user_id=?",
                                (thread_id, user_id)).fetchone()
        return row is not None

    def bind_voice(self, session_id: str, user_id: str, ttl_s: int = 90) -> None:
        with self._connect() as db:
            self._execute(db, "INSERT INTO auth_voice_sessions VALUES (?, ?, ?)",
                          (session_id, user_id, int(time.time()) + ttl_s))

    def owns_voice(self, session_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = self._execute(db, """SELECT 1 FROM auth_voice_sessions
                WHERE session_id=? AND user_id=? AND expires_at>?""",
                (session_id, user_id, int(time.time()))).fetchone()
            if row:
                self._execute(db, "UPDATE auth_voice_sessions SET expires_at=? WHERE session_id=?",
                              (int(time.time()) + 90, session_id))
        return row is not None

    def remove_voice(self, session_id: str) -> None:
        with self._connect() as db:
            self._execute(db, "DELETE FROM auth_voice_sessions WHERE session_id=?", (session_id,))


_DUMMY_HASH = password_hash("not-a-real-account-password")


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a Ship Agent account")
    parser.add_argument("create-user", choices=["create-user"])
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--role", choices=["user", "admin"], required=True)
    parser.add_argument("--backend", choices=["sqlite", "postgres"], default=os.getenv("SHIP_AUTH_BACKEND", os.getenv("SHIP_CHECKPOINT_BACKEND", "sqlite")))
    args = parser.parse_args()
    password = getpass.getpass("Password: ")
    store = AuthStore(backend=args.backend)
    store.ensure_schema()
    print(store.create_user(args.username, args.display_name, args.role, password))


if __name__ == "__main__":
    main()
